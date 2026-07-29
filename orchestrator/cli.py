"""smart-mcps-orchestrate CLI.

U4 ships the `group` command with `--dry-run` as the human checkpoint before any
execution; U9 adds `run` / `status` / `resume` on top. Config resolution is
CLI flags > `.orchestrator/config.toml` in the target repo > defaults (plan U9).
`group` writes a named grouping directory (`.orchestrator/groupings/<name>/`,
plan U10) holding `groups.json` + `base-context.md`; `run` selects one, snapshots
it into the run directory, and wires the Phase B execution engine: one base
session per run, a dependency-aware scheduler, per-group review loops, and
integration-branch merges.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from orchestrator.config import OrchestratorConfig, load_config
from orchestrator.execution.escalation import (
    EscalationBroker,
    EscalationPolicy,
    pending_escalations,
)
from orchestrator.execution.manifest import (
    GroupingNameError,
    GroupingSelectionError,
    ManifestStore,
    RunPaths,
    atomic_write_text,
    describe_groupings,
    grouping_dir,
    log_event,
    snapshot_grouping,
    validate_grouping_name,
)
from orchestrator.execution.merge import IntegrationMerger, MergeError, commits_ahead
from orchestrator.execution.review import MergeConflict, ReviewDeps, SurpriseBoard, make_executor
from orchestrator.execution.scheduler import (
    Executor,
    GroupState,
    ResolveConflict,
    ResolveDeps,
    RunAbort,
    RunState,
    Scheduler,
    SchedulerError,
)
from orchestrator.execution.sessions import SessionError, SessionRunner
from orchestrator.execution.worktrees import (
    WorktreeError,
    _git_ok,
    commit_all,
    create_worktree,
    group_branch,
    provision_env,
    worktree_path,
)
from orchestrator.grouping.graphing import CodegraphClient, GraphBuildError
from orchestrator.grouping.llm import JsonRunner, LlmError, claude_json_runner
from orchestrator.grouping.partition import GroupCycleError
from orchestrator.grouping.pipeline import (
    SELF_MODIFICATION_FLAG,
    GrouperError,
    compute_partition,
    group_label,
    run_grouping,
    serialize_grouping,
)
from orchestrator.grouping.plan_reader import strip_task_map
from orchestrator.grouping.speccer import write_specs
from orchestrator.grouping.trace import GroupingTrace, TraceRecorder, serialize_trace
from orchestrator.model import (
    EscalationResponse,
    Group,
    GroupingResult,
    HumanAction,
    ReviewIntensity,
    RunManifest,
    Surprise,
)


def main(
    argv: list[str] | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
) -> int:
    """Entry point. ``llm_runner`` and ``client`` are injectable for offline tests."""
    parser = argparse.ArgumentParser(prog="smart-mcps-orchestrate")
    subparsers = parser.add_subparsers(dest="command", required=True)

    group_cmd = subparsers.add_parser("group", help="compute groups from a plan document")
    group_cmd.add_argument("plan", type=Path, help="path to the plan document")
    group_cmd.add_argument(
        "--name",
        default=None,
        help="grouping name (default: the plan's filename stem); written to "
        ".orchestrator/groupings/<name>/",
    )
    group_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="print groups, DAG, and estimates without writing artifacts",
    )
    group_cmd.add_argument(
        "--token-budget", type=int, default=None, help="override estimator token budget per group"
    )
    group_cmd.add_argument(
        "--no-spec",
        action="store_true",
        help=(
            "print the partition-only report (groups, DAG, node work, budget cap, "
            "hub roles, slice atoms, last-modifying stage) with zero LLM calls; "
            "never writes artifacts"
        ),
    )
    group_cmd.add_argument(
        "--allow-unknown-symbols",
        action="store_true",
        help=(
            "task-map symbols not found in the codegraph index are dropped with a "
            "flag instead of failing the run (default: hard error)"
        ),
    )
    group_cmd.add_argument(
        "--allow-oversized-slice",
        action="store_true",
        help=(
            "keep a slice whose summed work exceeds the budget cap as one flagged "
            "group instead of failing (default: hard error, R5); equivalent to "
            "[partition] allow_oversized_slice = true in config.toml"
        ),
    )
    group_cmd.add_argument(
        "--allow-degenerate-partition",
        action="store_true",
        help=(
            "accept a partition whose cycle repair left a group over the budget cap "
            "instead of failing (default: hard error); equivalent to "
            "[partition] allow_degenerate_partition = true in config.toml"
        ),
    )
    _add_common_args(group_cmd)

    run_cmd = subparsers.add_parser("run", help="execute the groups computed by `group`")
    run_cmd.add_argument("--run-id", default=None, help="run identifier (default: r<timestamp>)")
    run_cmd.add_argument(
        "--grouping",
        default=None,
        help="named grouping to run (default: auto-select if exactly one exists)",
    )
    _add_execution_args(run_cmd)
    _add_common_args(run_cmd)

    resume_cmd = subparsers.add_parser("resume", help="resume a crashed or interrupted run")
    resume_cmd.add_argument("run_id", help="the run to resume (see `status`)")
    _add_execution_args(resume_cmd)
    _add_common_args(resume_cmd)

    groupings_cmd = subparsers.add_parser("groupings", help="list named groupings")
    groupings_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    status_cmd = subparsers.add_parser("status", help="show run state and sessions")
    status_cmd.add_argument("run_id", nargs="?", default=None, help="run to show (default: list)")
    status_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    answer_cmd = subparsers.add_parser("answer", help="answer a pending escalation (HITL)")
    answer_cmd.add_argument("run_id", help="the run holding the escalation")
    answer_cmd.add_argument("esc_id", help="the escalation id (see `status`)")
    answer_cmd.add_argument(
        "--action",
        default="answer",
        choices=[action.value for action in HumanAction],
        help="answer (resume/guide) | skip (fail the group) | abort (stop the run)",
    )
    answer_cmd.add_argument("--text", default="", help="free-text guidance for --action answer")
    answer_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    args = parser.parse_args(argv)
    if args.command == "group":
        return _cmd_group(args, llm_runner, client)
    if args.command == "run":
        return _cmd_run(args, llm_runner, resume=False)
    if args.command == "resume":
        return _cmd_run(args, llm_runner, resume=True)
    if args.command == "groupings":
        return _cmd_groupings(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "answer":
        return _cmd_answer(args)
    parser.error(f"unknown command {args.command!r}")
    return 2


def _add_common_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")
    cmd.add_argument("--config", type=Path, default=None, help="config TOML path")


def _add_execution_args(cmd: argparse.ArgumentParser) -> None:
    cmd.add_argument(
        "--sequential", action="store_true", help="run groups one at a time (debug mode)"
    )
    cmd.add_argument("--concurrency", type=int, default=None, help="max parallel groups")
    cmd.add_argument(
        "--permission-mode", default=None, help="claude CLI permission mode for workers"
    )
    cmd.add_argument(
        "--review-intensity",
        default=None,
        choices=[intensity.value for intensity in ReviewIntensity],
        help="override the computed review intensity for every group",
    )
    cmd.add_argument(
        "--hitl",
        action="store_true",
        help="enable human-in-the-loop escalation (default tier: on_stuck)",
    )
    cmd.add_argument(
        "--intensity",
        default=None,
        choices=["autonomous", "on_failure", "on_stuck", "interactive"],
        help="escalation tier; a non-autonomous tier implies --hitl",
    )
    cmd.add_argument(
        "--escalation-source",
        default=None,
        choices=["orchestrator_only", "workers_via_orchestrator"],
        help="who may request escalation (default: workers_via_orchestrator)",
    )
    cmd.add_argument(
        "--escalation-timeout",
        type=float,
        default=None,
        help="seconds to wait for an answer before the on_timeout fallback (default: block)",
    )


def apply_overrides(config: OrchestratorConfig, args: argparse.Namespace) -> OrchestratorConfig:
    """Layer CLI flags over the loaded config: flag > config-file > default (plan U9)."""
    execution_updates: dict = {}
    if getattr(args, "sequential", False):
        execution_updates["sequential"] = True
    if getattr(args, "concurrency", None) is not None:
        execution_updates["concurrency"] = args.concurrency
    if getattr(args, "permission_mode", None):
        execution_updates["permission_mode"] = args.permission_mode
    estimator_updates: dict = {}
    if getattr(args, "token_budget", None) is not None:
        estimator_updates["token_budget"] = args.token_budget
    partition_updates: dict = {}
    if getattr(args, "allow_oversized_slice", False):
        partition_updates["allow_oversized_slice"] = True
    if getattr(args, "allow_degenerate_partition", False):
        partition_updates["allow_degenerate_partition"] = True
    escalation_updates: dict = {}
    intensity = getattr(args, "intensity", None)
    if intensity:
        escalation_updates["intensity"] = intensity
    # --hitl enables; a non-autonomous --intensity implies it; --intensity
    # autonomous forces it off (an explicit "run unattended" even over a config file).
    if getattr(args, "hitl", False) or (intensity and intensity != "autonomous"):
        escalation_updates["enabled"] = True
    if intensity == "autonomous":
        escalation_updates["enabled"] = False
    if getattr(args, "escalation_source", None):
        escalation_updates["source"] = args.escalation_source
    if getattr(args, "escalation_timeout", None) is not None:
        escalation_updates["timeout_s"] = args.escalation_timeout
    updates: dict = {}
    if execution_updates:
        updates["execution"] = config.execution.model_copy(update=execution_updates)
    if estimator_updates:
        updates["estimator"] = config.estimator.model_copy(update=estimator_updates)
    if partition_updates:
        updates["partition"] = config.partition.model_copy(update=partition_updates)
    if escalation_updates:
        updates["escalation"] = config.escalation.model_copy(update=escalation_updates)
    return config.model_copy(update=updates) if updates else config


def _load_config(args: argparse.Namespace, repo_root: Path) -> OrchestratorConfig | None:
    config_path = args.config or repo_root / ".orchestrator" / "config.toml"
    try:
        return apply_overrides(load_config(config_path), args)
    except (ValidationError, tomllib.TOMLDecodeError) as exc:
        print(f"error: invalid config {config_path}: {exc}", file=sys.stderr)
        return None


# --------------------------------------------------------------------- group


def _cmd_group(
    args: argparse.Namespace,
    llm_runner: JsonRunner | None,
    client: CodegraphClient | None,
) -> int:
    repo_root = args.repo.resolve()
    name = getattr(args, "name", None) or args.plan.stem
    try:
        validate_grouping_name(name)
    except GroupingNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    config = _load_config(args, repo_root)
    if config is None:
        return 1
    allow_unknown_symbols = getattr(args, "allow_unknown_symbols", False)
    out_dir = grouping_dir(repo_root, name)
    trace_path = out_dir / "grouping-trace.json"
    recorder = TraceRecorder()

    if getattr(args, "no_spec", False):
        try:
            outcome = compute_partition(
                plan_path=args.plan,
                repo_root=repo_root,
                config=config,
                llm_runner=llm_runner,
                client=client,
                allow_unknown_symbols=allow_unknown_symbols,
                recorder=recorder,
            )
        except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
            _write_failure_trace(out_dir, recorder, exc, trace_path)
            return 1
        _write_trace(out_dir, recorder)
        _warn_self_modification(outcome.mapper_out.flags)
        _print_partition_report(recorder.trace)
        return 0

    try:
        result, base_context = run_grouping(
            plan_path=args.plan,
            repo_root=repo_root,
            config=config,
            llm_runner=llm_runner,
            client=client,
            allow_unknown_symbols=allow_unknown_symbols,
            recorder=recorder,
        )
    except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
        _write_failure_trace(out_dir, recorder, exc, trace_path)
        return 1
    _warn_self_modification(result.flags)

    if args.dry_run:
        _write_trace(out_dir, recorder)
        _print_report(result)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "groups.json").write_text(serialize_grouping(result))
    (out_dir / "base-context.md").write_text(base_context)
    _write_trace(out_dir, recorder)
    print(f"wrote {out_dir / 'groups.json'} and {out_dir / 'base-context.md'}")
    _print_report(result)
    return 0


def _write_trace(out_dir: Path, recorder: TraceRecorder) -> None:
    """Every ``group`` mode writes the trace (plan U9), including modes that
    write nothing else — explaining a partition or a failure is the point."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grouping-trace.json").write_text(serialize_trace(recorder.trace))


def _write_failure_trace(
    out_dir: Path, recorder: TraceRecorder, exc: Exception, trace_path: Path
) -> None:
    """A grouping that raises still leaves a trace: whatever stages ran before
    the failure, plus a ``failure`` section naming it — the CLI message points
    at the file instead of losing that partial context."""
    recorder.record_failure(exc)
    _write_trace(out_dir, recorder)
    print(f"error: {exc}", file=sys.stderr)
    print(f"see {trace_path} for the partial trace", file=sys.stderr)


def _warn_self_modification(flags: list[str]) -> None:
    """R15: echo the self-modification flag to stderr at grouping time, so a plan
    that edits orchestrator/ is caught before the run starts, not mid-run."""
    if SELF_MODIFICATION_FLAG in flags:
        print(f"warning: {SELF_MODIFICATION_FLAG}", file=sys.stderr)


def _print_partition_report(trace: GroupingTrace) -> None:
    """R18: the zero-LLM, sub-second answer to "how would this plan group?" —
    rendered from the trace (plan U9) rather than ``PartitionOutcome`` fields,
    so what is printed is exactly what ``grouping-trace.json`` also carries.
    """
    partition = trace.stages[-1].partition if trace.stages else {}
    node_work = {entry.node: entry.total for entry in trace.node_work}
    budget_cap = trace.budget.budget_cap if trace.budget else 0.0

    members_by_gid: dict[int, list[str]] = {}
    for node, gid in partition.items():
        members_by_gid.setdefault(gid, []).append(node)

    print(f"groups: {len(members_by_gid)} (partition-only — no specs, no LLM calls)")
    for gid, members in sorted(members_by_gid.items()):
        gid_str = group_label(gid)
        work = sum(node_work.get(node, 0.0) for node in members)
        downstream = sorted(group_label(down) for down in trace.dag.get(gid, ()))
        print(f"\n{gid_str}:")
        print(f"  tasks: {', '.join(sorted(members))}")
        print(f"  node work: {work:.1f} / budget cap {budget_cap:.1f}")
        print(f"  depends on (downstream): {', '.join(downstream) if downstream else 'none'}")

    hub_roles = {entry.node: entry.role for entry in trace.hub_roles if entry.role != "core"}
    print("\nhub roles:")
    if hub_roles:
        for node, role in sorted(hub_roles.items()):
            print(f"  {node}: {role}")
    else:
        print("  none")

    print("\nslice atoms:")
    if trace.slice_atoms:
        for entry in sorted(trace.slice_atoms, key=lambda e: e.label):
            print(f"  {entry.label}: {', '.join(entry.members)}")
    else:
        print("  none")

    print(f"\nlast partition-modifying stage: {trace.last_stage}")
    print(f"budget cap: {budget_cap:.1f}")

    if trace.mapper_flags:
        print("\nflags:")
        for flag in trace.mapper_flags:
            print(f"  - {flag}")


def _print_report(result: GroupingResult) -> None:
    print(f"plan: {result.plan_path}")
    print(f"groups: {len(result.groups)}")
    for group in result.groups:
        deps = ", ".join(group.dependencies) if group.dependencies else "none"
        print(f"\n{group.id}: {group.name}")
        print(f"  summary: {group.summary}")
        print(f"  tasks: {', '.join(group.tasks)}")
        print(f"  files: {', '.join(group.files) if group.files else 'none'}")
        print(f"  est. tokens: {group.estimated_tokens}")
        print(f"  difficulty: {group.difficulty:.2f} → {group.intensity.value}")
        print(f"  depends on: {deps}")
        print(f"  verification: {len(group.verification)} item(s)")
    if result.flags:
        print("\nflags:")
        for flag in result.flags:
            print(f"  - {flag}")


# ------------------------------------------------------------------ groupings


def _select_grouping(repo_root: Path, name: str | None) -> tuple[str, Path]:
    """`run`'s grouping selection (plan U10): an explicit ``--grouping`` wins;
    with none, auto-select only when exactly one grouping exists. Ambiguity and
    legacy top-level state are reported by name, never guessed — that
    implicitness is the failure ADR 0002 records."""
    if name:
        source_dir = grouping_dir(repo_root, name)
        if not (source_dir / "groups.json").is_file():
            raise GroupingSelectionError(
                f"no grouping named {name!r} at {source_dir} — run "
                f"`smart-mcps-orchestrate group <plan> --name {name}` first"
            )
        return name, source_dir

    infos = describe_groupings(repo_root)
    if len(infos) == 1:
        info = infos[0]
        return info.name, grouping_dir(repo_root, info.name)
    if len(infos) > 1:
        listing = "; ".join(f"{info.name} ({info.plan_path})" for info in infos)
        raise GroupingSelectionError(
            f"multiple groupings present — pick one with --grouping <name>: {listing}"
        )

    legacy = repo_root / ".orchestrator" / "groups.json"
    if legacy.is_file():
        raise GroupingSelectionError(
            f"found legacy grouping artifact {legacy} from before named groupings — "
            "it is not used automatically; re-group with "
            "`smart-mcps-orchestrate group <plan> --name <name>`"
        )
    raise GroupingSelectionError(
        "no groupings found — run `smart-mcps-orchestrate group <plan>` first"
    )


def _cmd_groupings(args: argparse.Namespace) -> int:
    repo_root = args.repo.resolve()
    infos = describe_groupings(repo_root)
    if not infos:
        print("no groupings found")
        return 0
    for info in infos:
        print(f"{info.name}: {info.plan_path} ({info.group_count} group(s))")
    return 0


# ----------------------------------------------------------------- run/resume


def _cmd_run(args: argparse.Namespace, llm_runner: JsonRunner | None, *, resume: bool) -> int:
    repo_root = args.repo.resolve()
    config = _load_config(args, repo_root)
    if config is None:
        return 1

    run_id = args.run_id if resume else (args.run_id or _default_run_id())
    paths = RunPaths(repo_root, run_id)
    orch_dir = repo_root / ".orchestrator"

    source_grouping_dir: Path | None = None
    grouping_name: str | None = None
    if resume:
        if not paths.state_path.is_file():
            print(
                f"error: no run state at {paths.state_path} — check `status` for known runs",
                file=sys.stderr,
            )
            return 1
        groups_path = paths.run_dir / "groups.json"
        base_context_path = paths.run_dir / "base-context.md"
    else:
        if paths.state_path.is_file():
            print(
                f"error: run {run_id} already exists — `resume {run_id}` to continue it, "
                "or pick another --run-id",
                file=sys.stderr,
            )
            return 1
        try:
            grouping_name, source_grouping_dir = _select_grouping(
                repo_root, getattr(args, "grouping", None)
            )
        except (GroupingNameError, GroupingSelectionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        groups_path = source_grouping_dir / "groups.json"
        base_context_path = source_grouping_dir / "base-context.md"

    if not groups_path.is_file() or not base_context_path.is_file():
        print(
            f"error: {groups_path} or {base_context_path} missing — "
            "run `smart-mcps-orchestrate group <plan>` first",
            file=sys.stderr,
        )
        return 1
    grouping = GroupingResult.model_validate_json(groups_path.read_text())
    groups = grouping.groups
    if getattr(args, "review_intensity", None):
        intensity = ReviewIntensity(args.review_intensity)
        groups = [group.model_copy(update={"intensity": intensity}) for group in groups]

    plan_path = Path(grouping.plan_path)
    if not plan_path.is_absolute():
        plan_path = repo_root / plan_path
    if not plan_path.is_file():
        print(
            f"error: plan document {plan_path} (referenced by the grouping) not found — "
            "re-run `group` against the current plan",
            file=sys.stderr,
        )
        return 1
    # Stripped before it ever reaches an LLM context (R27) — the rewrite provider
    # is the only consumer of plan_text in this command.
    plan_text = strip_task_map(plan_path.read_text())

    # R8: the effective execution config prints before any session spawns —
    # obs1's operator trap was a config file silently beating flag expectations,
    # discovered only after the base session was already paid for.
    mode = (
        "sequential"
        if config.execution.sequential
        else f"concurrency {config.execution.concurrency}"
    )
    hitl = (
        f"HITL on (intensity={config.escalation.intensity}, source={config.escalation.source})"
        if config.escalation.enabled
        else "HITL off"
    )
    print(
        f"run {run_id}: {len(groups)} group(s), {mode}, {hitl}, "
        f"permission-mode {config.execution.permission_mode}"
    )

    session = config.session
    runner = SessionRunner(
        claude_bin=session.claude_bin,
        model=session.model,
        permission_mode=config.execution.permission_mode,
        allowed_tools=session.allowed_tools or None,
        transcript_root=(
            Path(session.transcript_root).expanduser() if session.transcript_root else None
        ),
    )
    try:
        runner.preflight()
    except SessionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    # The run keeps its own frozen copy of the grouping it started with (plan
    # U10): a later `group --name <same>` against a different plan must not be
    # able to rewrite a finished run's history. Done only after preflight
    # succeeds, so a dead worker CLI never leaves a run directory behind.
    if not resume:
        snapshot_grouping(source_grouping_dir, paths.run_dir)

    merger = IntegrationMerger(repo_root, run_id)
    try:
        merger.ensure()
    except WorktreeError as exc:
        print(f"error: cannot create integration worktree: {exc}", file=sys.stderr)
        return 1

    # The lifecycle log is always on (R10): the run-start line lands in every
    # mode; only the escalation channel itself is HITL-gated. Built before the
    # Scheduler (plan U2): a FAILED group's resolve routine needs the same
    # broker/policy the review loop's escalations already use.
    if config.escalation.enabled:
        broker: EscalationBroker | None = EscalationBroker(paths, config.escalation)
        policy: EscalationPolicy | None = EscalationPolicy(
            config.escalation.intensity, config.escalation.source
        )
        log_event(
            paths,
            f"run {run_id} started with HITL: intensity={config.escalation.intensity}, "
            f"source={config.escalation.source}, "
            f"timeout={config.escalation.timeout_s}",
        )
    else:
        broker = None
        policy = None
        log_event(paths, f"run {run_id} started (autonomous)")

    resolve_deps = _resolve_deps(repo_root, run_id, merger)

    # Construction is circular on paper (scheduler → executor → deps → runner →
    # scheduler.tracker); the executor closes over a slot assigned once deps exist —
    # it is only invoked inside scheduler.run().
    executor_slot: list[Executor] = []

    async def executor(ctx):  # noqa: ANN001 — GroupContext, kept light for the closure
        return await executor_slot[0](ctx)

    try:
        scheduler = Scheduler(
            groups=groups,
            paths=paths,
            executor=executor,
            config=config.execution,
            resume=resume,
            broker=broker,
            policy=policy,
            resolve=resolve_deps,
        )
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    runner.tracker = scheduler.tracker

    store = ManifestStore(paths)
    if resume:
        if not store.exists():
            print(f"error: no manifest at {paths.manifest_path}", file=sys.stderr)
            return 1
        manifest = store.load()
        if not manifest.base_session_id:
            print("error: manifest has no base session — start a fresh run", file=sys.stderr)
            return 1
        base_session_id = manifest.base_session_id
    else:
        try:
            base = runner.start_base(
                run_id=run_id, base_context=base_context_path.read_text(), cwd=repo_root
            )
        except SessionError as exc:
            print(f"error: base session failed: {exc}", file=sys.stderr)
            return 1
        base_session_id = base.session_id
        manifest = RunManifest(
            run_id=run_id,
            plan_path=grouping.plan_path,
            base_session_id=base_session_id,
            grouping=grouping_name,
        )
        store.save(manifest)

    workspace_for, base_ref_for = _workspace_seams(repo_root, run_id, merger, paths)
    deps = ReviewDeps(
        run_id=run_id,
        runner=runner,
        store=store,
        manifest=manifest,
        base_session_id=base_session_id,
        breaker=config.breaker,
        execution=config.execution,
        board=SurpriseBoard(),
        workspace_for=workspace_for,
        merge_group=merger.merge_group,
        rewrite_spec=_rewrite_provider(
            plan_text, llm_runner or claude_json_runner, orch_dir / "failures"
        ),
        base_ref_for=base_ref_for,
        broker=broker,
        policy=policy,
    )
    executor_slot.append(make_executor(deps))

    try:
        asyncio.run(scheduler.run())
    except RunAbort as exc:
        # The operator stopped the run; state stays resumable (mid-flight groups
        # restart from ready on `resume`).
        print(f"run aborted by operator: {exc}", file=sys.stderr)
        log_event(paths, f"run {run_id} aborted by operator: {exc}")
        _print_outcomes(scheduler.state)
        print(f"resume with: smart-mcps-orchestrate resume {run_id}", file=sys.stderr)
        return 2
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_outcomes(scheduler.state)
        return 1
    return _print_outcomes(scheduler.state)


def _default_run_id() -> str:
    """Short, filesystem- and ref-safe; lands in branch names and session names."""
    return datetime.now(UTC).strftime("r%Y%m%d-%H%M%S")


def _workspace_seams(repo_root: Path, run_id: str, merger: IntegrationMerger, paths: RunPaths):
    """The workspace_for / base_ref_for pair, sharing one tip capture per group.

    The integration tip is read once per group at its ready→running transition —
    an interleaved sibling merge must not move a group's diff base between the
    reviewer's diff and the handoff's diff_stat. On resume the branch already
    exists, so the diff base is its original fork point (merge-base), not today's
    tip.
    """
    tips: dict[str, str] = {}

    def workspace_for(group: Group) -> Path:
        branch = group_branch(run_id, group.id)
        tip = merger.tip()
        path = create_worktree(
            repo_root, group_id=group.id, name=group.name, branch=branch, start_point=tip
        )
        # U6/R16: the worktree owns its environment — provision after creation,
        # non-fatally (a failed sync logs and lets the worker re-sync itself).
        provision_env(path, log=lambda message: log_event(paths, message))
        tips[group.id] = _git_ok(repo_root, "merge-base", tip, branch).strip()
        return path

    def base_ref_for(group: Group) -> str:
        return tips[group.id]

    return workspace_for, base_ref_for


def _resolve_deps(repo_root: Path, run_id: str, merger: IntegrationMerger) -> ResolveDeps:
    """Wires the scheduler's resolve routine (plan U2) to real git, translating
    ``MergeConflict`` into the scheduler's own ``ResolveConflict`` so scheduler.py
    never has to import merge/review machinery (review.py already imports
    scheduler.py — a reverse import there would cycle).
    """

    def branch_for(group: Group) -> str:
        return group_branch(run_id, group.id)

    def worktree_for(group: Group) -> Path:
        return worktree_path(repo_root, group.id, group.name)

    def commit_stranded(group: Group) -> bool:
        return commit_all(worktree_for(group), f"resolve({run_id}): {group.id} stranded work")

    def commits_ahead_fn(group: Group) -> int:
        return commits_ahead(merger.ensure(), merger.branch, branch_for(group))

    def merge_for_resolve(group: Group) -> None:
        try:
            merger.merge_group(group, worktree_for(group))
        except MergeConflict as exc:
            raise ResolveConflict(f"resolving group {group.id}: {exc}") from exc
        except MergeError:
            pass  # commits_ahead already gated this — defensive no-op

    return ResolveDeps(
        commit_stranded=commit_stranded,
        commits_ahead=commits_ahead_fn,
        merge_group=merge_for_resolve,
    )


def _rewrite_provider(plan_text: str, llm_runner: JsonRunner, failure_dir: Path):
    """rewrite_spec seam: one-group skeleton through the Phase A speccer, with the
    surprises folded in as rewrite context (they are never empty on escalation
    paths — Phase B synthesizes a context surprise for blocked/too_hard/etc.)."""

    def rewrite_spec(group: Group, surprises: list[Surprise]) -> Group:
        skeleton = {
            group.id: {
                "tasks": group.tasks,
                "files": group.files,
                "previous_spec": group.spec,
                "rewrite_context": [
                    f"[{surprise.kind}] {surprise.description}" for surprise in surprises
                ],
            }
        }
        spec = write_specs(plan_text, skeleton, llm_runner, failure_dir=failure_dir)[group.id]
        return group.model_copy(
            update={
                "name": spec.name,
                "summary": spec.summary,
                "spec": spec.spec,
                "verification": spec.verification,
            }
        )

    return rewrite_spec


def _print_outcomes(state: RunState) -> int:
    print(f"\nrun {state.run_id}:")
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        line = f"  {gid}: {entry.state.value}"
        if entry.generation > 1:
            line += f" (generation {entry.generation})"
        if entry.failure:
            line += f" — {entry.failure}"
        print(line)
    completed = all(entry.state == GroupState.COMPLETED for entry in state.groups.values())
    if completed:
        print("all groups completed; merge the integration branch when ready")
        return 0
    interrupted = sorted(
        gid for gid, entry in state.groups.items() if entry.state == GroupState.INTERRUPTED
    )
    if interrupted:
        # Envelope failures are stopped-but-resumable: exit 2 mirrors the
        # operator-abort path, distinct from needs-inspection work failures.
        print(
            f"run interrupted — group(s) {', '.join(interrupted)} stopped by envelope "
            f"failure; resume with: smart-mcps-orchestrate resume {state.run_id}",
            file=sys.stderr,
        )
        return 2
    print("run did not complete — inspect `status`, fix, then `resume`", file=sys.stderr)
    return 1


# --------------------------------------------------------------------- status


def _cmd_status(args: argparse.Namespace) -> int:
    repo_root = args.repo.resolve()
    runs_dir = repo_root / ".orchestrator" / "runs"
    if args.run_id is None:
        runs = sorted(p.name for p in runs_dir.iterdir() if p.is_dir()) if runs_dir.is_dir() else []
        if not runs:
            print("no runs found")
            return 0
        for run_id in runs:
            print(run_id)
        return 0

    paths = RunPaths(repo_root, args.run_id)
    if not paths.state_path.is_file():
        print(f"error: no run state at {paths.state_path}", file=sys.stderr)
        return 1
    state = RunState.model_validate_json(paths.state_path.read_text())
    store = ManifestStore(paths)
    manifest = store.load() if store.exists() else None

    print(f"run {state.run_id}")
    if manifest is not None:
        print(f"plan: {manifest.plan_path}")
        print(f"base session: {manifest.base_session_id}")
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        line = f"\n{gid}: {entry.state.value} (generation {entry.generation})"
        if entry.failure:
            line += f"\n  failure: {entry.failure}"
        print(line)
        if manifest is not None and gid in manifest.groups:
            group_entry = manifest.groups[gid]
            print(f"  {group_entry.group_name}: {group_entry.summary}")
            for session in group_entry.sessions:
                retired = (
                    f" (retired: {session.retirement_reason})" if session.retirement_reason else ""
                )
                print(f"  session {session.name} [{session.role.value}]{retired}")

    pending = pending_escalations(paths)
    if pending:
        print("\npending escalations (answer with `answer <run_id> <esc_id> ...`):")
        for request in pending:
            print(f"  {request.id} [{request.kind.value}] {request.group_id}: {request.prompt}")
    return 0


# --------------------------------------------------------------------- answer


def _cmd_answer(args: argparse.Namespace) -> int:
    """Write a response file the run's blocked coroutine picks up by correlation id.

    A clean, testable channel for the main session (or a foreground operator) to
    resolve an escalation without touching the running process."""
    paths = RunPaths(args.repo.resolve(), args.run_id)
    request_path = paths.escalations_dir / f"request-{args.esc_id}.json"
    if not request_path.is_file():
        print(
            f"error: no escalation {args.esc_id} for run {args.run_id} "
            f"(check `status {args.run_id}`)",
            file=sys.stderr,
        )
        return 1
    response = EscalationResponse(id=args.esc_id, action=HumanAction(args.action), answer=args.text)
    response_path = paths.escalations_dir / f"response-{args.esc_id}.json"
    atomic_write_text(response_path, response.model_dump_json(indent=2) + "\n")
    print(f"answered {args.esc_id}: {args.action}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
