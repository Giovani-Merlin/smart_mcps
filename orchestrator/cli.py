"""smart-mcps-orchestrate CLI.

U4 ships the `group` command with `--dry-run` as the human checkpoint before any
execution; U9 adds `run` / `status` / `resume` on top. Config resolution is
CLI flags > `.orchestrator/config.toml` in the target repo > defaults (plan U9).
`run` consumes the artifacts `group` wrote (`groups.json`, `base-context.md`) and
wires the Phase B execution engine: one base session per run, a dependency-aware
scheduler, per-group review loops, and integration-branch merges.
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
from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.execution.merge import IntegrationMerger
from orchestrator.execution.review import ReviewDeps, SurpriseBoard, make_executor
from orchestrator.execution.scheduler import (
    Executor,
    GroupState,
    RunState,
    Scheduler,
    SchedulerError,
)
from orchestrator.execution.sessions import SessionError, SessionRunner
from orchestrator.execution.worktrees import (
    WorktreeError,
    _git_ok,
    create_worktree,
    group_branch,
)
from orchestrator.grouping.graphing import CodegraphClient, GraphBuildError
from orchestrator.grouping.llm import JsonRunner, LlmError, claude_json_runner
from orchestrator.grouping.partition import GroupCycleError
from orchestrator.grouping.pipeline import GrouperError, run_grouping, serialize_grouping
from orchestrator.grouping.speccer import write_specs
from orchestrator.model import Group, GroupingResult, ReviewIntensity, RunManifest, Surprise


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
        "--dry-run",
        action="store_true",
        help="print groups, DAG, and estimates without writing artifacts",
    )
    group_cmd.add_argument(
        "--token-budget", type=int, default=None, help="override estimator token budget per group"
    )
    _add_common_args(group_cmd)

    run_cmd = subparsers.add_parser("run", help="execute the groups computed by `group`")
    run_cmd.add_argument("--run-id", default=None, help="run identifier (default: r<timestamp>)")
    _add_execution_args(run_cmd)
    _add_common_args(run_cmd)

    resume_cmd = subparsers.add_parser("resume", help="resume a crashed or interrupted run")
    resume_cmd.add_argument("run_id", help="the run to resume (see `status`)")
    _add_execution_args(resume_cmd)
    _add_common_args(resume_cmd)

    status_cmd = subparsers.add_parser("status", help="show run state and sessions")
    status_cmd.add_argument("run_id", nargs="?", default=None, help="run to show (default: list)")
    status_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    args = parser.parse_args(argv)
    if args.command == "group":
        return _cmd_group(args, llm_runner, client)
    if args.command == "run":
        return _cmd_run(args, llm_runner, resume=False)
    if args.command == "resume":
        return _cmd_run(args, llm_runner, resume=True)
    if args.command == "status":
        return _cmd_status(args)
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
    updates: dict = {}
    if execution_updates:
        updates["execution"] = config.execution.model_copy(update=execution_updates)
    if estimator_updates:
        updates["estimator"] = config.estimator.model_copy(update=estimator_updates)
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
    config = _load_config(args, repo_root)
    if config is None:
        return 1
    try:
        result, base_context = run_grouping(
            plan_path=args.plan,
            repo_root=repo_root,
            config=config,
            llm_runner=llm_runner,
            client=client,
        )
    except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.dry_run:
        _print_report(result)
        return 0

    out_dir = repo_root / ".orchestrator"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "groups.json").write_text(serialize_grouping(result))
    (out_dir / "base-context.md").write_text(base_context)
    print(f"wrote {out_dir / 'groups.json'} and {out_dir / 'base-context.md'}")
    _print_report(result)
    return 0


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


# ----------------------------------------------------------------- run/resume


def _cmd_run(args: argparse.Namespace, llm_runner: JsonRunner | None, *, resume: bool) -> int:
    repo_root = args.repo.resolve()
    config = _load_config(args, repo_root)
    if config is None:
        return 1

    orch_dir = repo_root / ".orchestrator"
    groups_path = orch_dir / "groups.json"
    base_context_path = orch_dir / "base-context.md"
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
            f"error: plan document {plan_path} (referenced by groups.json) not found — "
            "re-run `group` against the current plan",
            file=sys.stderr,
        )
        return 1
    plan_text = plan_path.read_text()

    run_id = args.run_id if resume else (args.run_id or _default_run_id())
    paths = RunPaths(repo_root, run_id)
    if resume and not paths.state_path.is_file():
        print(
            f"error: no run state at {paths.state_path} — check `status` for known runs",
            file=sys.stderr,
        )
        return 1

    session = config.session
    runner = SessionRunner(
        claude_bin=session.claude_bin,
        timeout_s=session.timeout_s,
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
        )
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    runner.tracker = scheduler.tracker

    merger = IntegrationMerger(repo_root, run_id)
    try:
        merger.ensure()
    except WorktreeError as exc:
        print(f"error: cannot create integration worktree: {exc}", file=sys.stderr)
        return 1

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
            run_id=run_id, plan_path=grouping.plan_path, base_session_id=base_session_id
        )
        store.save(manifest)

    workspace_for, base_ref_for = _workspace_seams(repo_root, run_id, merger)
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
    )
    executor_slot.append(make_executor(deps))

    print(
        f"run {run_id}: {len(groups)} group(s), concurrency "
        f"{1 if config.execution.sequential else config.execution.concurrency}"
    )
    try:
        asyncio.run(scheduler.run())
    except SchedulerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        _print_outcomes(scheduler.state)
        return 1
    return _print_outcomes(scheduler.state)


def _default_run_id() -> str:
    """Short, filesystem- and ref-safe; lands in branch names and session names."""
    return datetime.now(UTC).strftime("r%Y%m%d-%H%M%S")


def _workspace_seams(repo_root: Path, run_id: str, merger: IntegrationMerger):
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
        tips[group.id] = _git_ok(repo_root, "merge-base", tip, branch).strip()
        return path

    def base_ref_for(group: Group) -> str:
        return tips[group.id]

    return workspace_for, base_ref_for


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
    return 0


if __name__ == "__main__":
    sys.exit(main())
