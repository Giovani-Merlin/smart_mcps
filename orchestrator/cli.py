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
import functools
import json
import os
import signal
import sys
import tomllib
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from orchestrator.config import (
    AuthConfig,
    EscalationConfig,
    ExecutionConfig,
    OrchestratorConfig,
    SessionConfig,
    UsageLimitConfig,
    load_config,
)
from orchestrator.execution.auth import AuthLadder
from orchestrator.execution.confinement import (
    default_cache_root,
    landlock_abi_version,
    worker_cache_env,
)
from orchestrator.execution.escalation import (
    EscalationBroker,
    EscalationError,
    EscalationPolicy,
    answer_escalation,
    pending_escalations,
)
from orchestrator.execution.driver import DriverAlreadyRunning, DriverLock, driver_status_line
from orchestrator.execution.heartbeat import RoundHeartbeat
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
from orchestrator.execution.calibrate import calibrate_run, format_calibration
from orchestrator.execution.finish import FinishError, finish_run, run_is_finishable
from orchestrator.execution.merge import IntegrationMerger, MergeError, commits_ahead
from orchestrator.execution.preflight import (
    PreflightFailure,
    capture_preflight_baseline,
    load_baseline,
    save_baseline,
)
from orchestrator.execution.retry import RetryConflictError, RetryError, retry_group
from orchestrator.execution.prompting import render_conflict_resolve_prompt
from orchestrator.execution.ratelimit import UsageLimitGate, UsageLimitState
from orchestrator.execution.review import (
    MergeConflict,
    ReviewDeps,
    SurpriseBoard,
    format_residue_report,
    make_executor,
    surprise_residue,
)
from orchestrator.execution.scheduler import (
    Executor,
    GroupState,
    HoldReason,
    ResolveConflict,
    ResolveDeps,
    ResolvePreflightFailed,
    RunAbort,
    RunState,
    RunStateVersionError,
    Scheduler,
    SchedulerError,
)
from orchestrator.execution.sessions import (
    ReportError,
    SessionError,
    SessionRunner,
    nudge_until_report,
)
from orchestrator.execution.worktrees import (
    WorktreeError,
    _git,
    _git_ok,
    commit_all,
    create_worktree,
    group_branch,
    provision_env,
    worktree_path,
    write_provisioning_record,
)
from orchestrator.grouping.graphing import CodegraphClient, GraphBuildError
from orchestrator.grouping.llm import (
    JsonRunner,
    LlmError,
    claude_json_runner,
    with_usage_limit_retry,
)
from orchestrator.grouping.partition import GroupCycleError
from orchestrator.grouping.pipeline import (
    SELF_MODIFICATION_FLAG,
    GrouperError,
    IndexFingerprintMismatch,
    compute_partition,
    group_label,
    run_grouping,
    verify_index_fingerprint,
    EdgeProvenanceRecorder,
    serialize_edge_provenance,
    serialize_grouping,
)
from orchestrator.grouping.plan_reader import strip_task_map
from orchestrator.grouping.speccer import write_specs
from orchestrator.grouping.llm_record import JsonlCallRecorder
from orchestrator.grouping.trace import GroupingTrace, TraceRecorder, serialize_trace
from orchestrator.model import (
    CoderReport,
    Group,
    GroupingResult,
    HumanAction,
    ReviewIntensity,
    RunManifest,
    SessionRole,
    Surprise,
)

# Reviewer sessions one group at a given intensity spawns (plan U8's
# --review-intensity warning): self-verify skips the reviewer entirely,
# paired-plus adds one mandatory extra verification pass (origin R15).
_REVIEWER_SESSIONS: dict[ReviewIntensity, int] = {
    ReviewIntensity.SELF_VERIFY: 0,
    ReviewIntensity.PAIRED: 1,
    ReviewIntensity.PAIRED_PLUS: 2,
}


DEFAULT_REGISTRY_PATH = "~/.orchestrator-ui.yaml"
DEFAULT_UI_PORT = 8765
OBSERVATORY_HOST = "127.0.0.1"


def main(
    argv: list[str] | None = None,
    llm_runner: JsonRunner | None = None,
    client: CodegraphClient | None = None,
) -> int:
    """Entry point. ``llm_runner`` and ``client`` are injectable for offline tests."""
    # A run is normally started as `… > run.log 2>&1 &`, and Python block-buffers
    # stdout at 8KB when it is not a tty — so `run.log` stayed empty for the life
    # of the run and a healthy run was indistinguishable from a hang, with the
    # confinement header invisible. `-u` is not available to us (console-script
    # entry points take no interpreter flags), and one reconfigure here fixes
    # every print at once rather than needing `flush=True` on each.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):  # a replaced/closed stdout in tests
        pass
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
    group_cmd.add_argument(
        "--granularity",
        default=None,
        choices=["independent", "balanced", "monolithic"],
        help=(
            "how eagerly merge_small_groups folds small groups together: "
            "'independent' (default) enforces chain-compatibility and the makespan "
            "no-regression check (today's behaviour); 'balanced' drops "
            "chain-compatibility but still rejects a merge that regresses the "
            "simulated makespan; 'monolithic' also drops the makespan check. The "
            "budget cap, slice must-link and cycle checks stay hard at every level. "
            "Equivalent "
            "to [partition] granularity in config.toml; this flag wins when both "
            "are set."
        ),
    )
    _add_auto_resume_arg(group_cmd)
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

    retry_cmd = subparsers.add_parser(
        "retry", help="release a terminally failed or quarantined group (operator override)"
    )
    retry_cmd.add_argument("run_id", help="the run holding the group")
    retry_cmd.add_argument("group_id", help="the group to retry")
    retry_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    finish_cmd = subparsers.add_parser(
        "finish",
        help="push the integration branch, open a PR, and tear down merged groups",
    )
    finish_cmd.add_argument("run_id", help="the run to finish")
    finish_cmd.add_argument("--repo", type=Path, default=Path.cwd(), help="target repo root")

    calibrate_cmd = subparsers.add_parser(
        "calibrate", help="compare a finished run's token estimates against what it actually cost"
    )
    calibrate_cmd.add_argument("run_id", help="the run to calibrate against")
    _add_common_args(calibrate_cmd)

    ui_cmd = subparsers.add_parser("ui", help="serve the Observatory web UI (local, no auth)")
    ui_cmd.add_argument(
        "--registry",
        type=Path,
        default=None,
        help=f"project registry YAML (default: {DEFAULT_REGISTRY_PATH})",
    )
    ui_cmd.add_argument(
        "--port", type=int, default=DEFAULT_UI_PORT, help=f"port (default: {DEFAULT_UI_PORT})"
    )
    ui_cmd.add_argument(
        "--repo",
        type=Path,
        default=Path.cwd(),
        help="target repo root; served as a fallback project when no registry file exists",
    )

    args = parser.parse_args(argv)
    if args.command == "group":
        return _cmd_group(args, llm_runner, client)
    if args.command == "run":
        return _cmd_run(args, llm_runner, client, resume=False)
    if args.command == "resume":
        return _cmd_run(args, llm_runner, client, resume=True)
    if args.command == "groupings":
        return _cmd_groupings(args)
    if args.command == "status":
        return _cmd_status(args)
    if args.command == "answer":
        return _cmd_answer(args)
    if args.command == "retry":
        return _cmd_retry(args)
    if args.command == "finish":
        return _cmd_finish(args)
    if args.command == "calibrate":
        return _cmd_calibrate(args)
    if args.command == "ui":
        return _cmd_ui(args)
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
    cmd.add_argument(
        "--on-failure",
        default=None,
        choices=["halt", "overlap"],
        help=(
            "admission policy once a group ends FAILED or INTERRUPTED (plan U3/R41): "
            "'halt' (default) admits no further group; 'overlap' keeps only the "
            "file-overlap gate"
        ),
    )
    cmd.add_argument(
        "--allow-index-drift",
        action="store_true",
        help=(
            "when the grouping being run was built against a codegraph index that "
            "no longer matches the current one (plan U7), warn and force a "
            "re-partition instead of failing (default: hard error naming both "
            "fingerprints); the re-partition is index-stable, not reproducible — "
            "the mapper is an unseeded LLM call"
        ),
    )
    _add_auto_resume_arg(cmd)


def _add_auto_resume_arg(cmd: argparse.ArgumentParser) -> None:
    """``--auto-resume`` / ``--no-auto-resume``, on both the execution commands
    and ``group`` — the one-shot grouping path meets the same account limit."""
    cmd.add_argument(
        "--auto-resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "on a usage limit, pause until it resets and retry the same call "
            "instead of stopping the run (default: on; --no-auto-resume restores "
            "the stop-and-resume-by-hand behaviour)"
        ),
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
    if getattr(args, "on_failure", None) is not None:
        execution_updates["on_group_failure"] = args.on_failure
    estimator_updates: dict = {}
    if getattr(args, "token_budget", None) is not None:
        estimator_updates["token_budget"] = args.token_budget
    partition_updates: dict = {}
    if getattr(args, "allow_oversized_slice", False):
        partition_updates["allow_oversized_slice"] = True
    if getattr(args, "allow_degenerate_partition", False):
        partition_updates["allow_degenerate_partition"] = True
    if getattr(args, "granularity", None) is not None:
        partition_updates["granularity"] = args.granularity
    escalation_updates: dict = {}
    intensity = getattr(args, "intensity", None)
    if intensity:
        escalation_updates["intensity"] = intensity
    # --hitl enables; a non-autonomous --intensity implies it; --intensity
    # autonomous forces it off (an explicit "run unattended" even over a config file).
    if getattr(args, "hitl", False) or (intensity and intensity != "autonomous"):
        escalation_updates["enabled"] = True
    # --hitl alone has to supply a tier as well. The library default is now
    # `autonomous`, and enabled=True beside intensity=autonomous escalates
    # nothing — --hitl would be a silent no-op. Only fill in when the operator
    # named no tier and the resolved tier is autonomous, so a config file's own
    # non-autonomous tier still wins.
    if (
        getattr(args, "hitl", False)
        and not intensity
        and config.escalation.intensity == "autonomous"
    ):
        escalation_updates["intensity"] = "on_stuck"
    if intensity == "autonomous":
        escalation_updates["enabled"] = False
    if getattr(args, "escalation_source", None):
        escalation_updates["source"] = args.escalation_source
    if getattr(args, "escalation_timeout", None) is not None:
        escalation_updates["timeout_s"] = args.escalation_timeout
    session_updates: dict = {}
    auto_resume = getattr(args, "auto_resume", None)
    if auto_resume is not None:
        session_updates["usage_limit"] = config.session.usage_limit.model_copy(
            update={"auto_resume": auto_resume}
        )
    updates: dict = {}
    if session_updates:
        updates["session"] = config.session.model_copy(update=session_updates)
    if execution_updates:
        updates["execution"] = config.execution.model_copy(update=execution_updates)
    if estimator_updates:
        updates["estimator"] = config.estimator.model_copy(update=estimator_updates)
    if partition_updates:
        updates["partition"] = config.partition.model_copy(update=partition_updates)
    if escalation_updates:
        updates["escalation"] = config.escalation.model_copy(update=escalation_updates)
    return config.model_copy(update=updates) if updates else config


def _load_config(
    args: argparse.Namespace,
    repo_root: Path,
    *,
    persisted_escalation: EscalationConfig | None = None,
    persisted_usage_limit: UsageLimitConfig | None = None,
) -> OrchestratorConfig | None:
    """Load config.toml, then layer CLI flags on top (flag > config-file > default).

    ``persisted_escalation`` — a resumed run's own recorded escalation tier (plan
    U2) — slots in as a fourth rung *under* the config file and *above* the
    library default, so an omitted flag on resume restores the run's original
    tier instead of resetting to ``EscalationConfig()``'s autonomous/HITL-off
    default; an explicit flag on resume still wins via ``apply_overrides``.

    ``persisted_usage_limit`` slots in at the same rung for the same reason: a
    run started with ``--no-auto-resume`` must not silently regain auto-resume
    when it is resumed without the flag.
    """
    config_path = args.config or repo_root / ".orchestrator" / "config.toml"
    try:
        loaded = load_config(config_path)
        if persisted_escalation is not None:
            loaded = loaded.model_copy(update={"escalation": persisted_escalation})
        if persisted_usage_limit is not None:
            loaded = loaded.model_copy(
                update={
                    "session": loaded.session.model_copy(
                        update={"usage_limit": persisted_usage_limit}
                    )
                }
            )
        return apply_overrides(loaded, args)
    except (ValidationError, tomllib.TOMLDecodeError) as exc:
        print(f"error: invalid config {config_path}: {exc}", file=sys.stderr)
        return None


def _config_banner_source(config_path: Path) -> str:
    """F11: naming a config file that was never read misleads the reader into
    thinking one exists — only name the path when `load_config` actually
    found and parsed a file there (mirrors `load_config`'s own is_file check).
    """
    return str(config_path) if config_path.is_file() else "defaults (no config file)"


# --------------------------------------------------------------------- group


def _anchor_plan_path(plan: Path, repo_root: Path) -> Path:
    """Resolve a plan argument the way `run`/`resume` already resolve theirs.

    `group` passed `args.plan` through verbatim, and `Path.is_file()` downstream
    resolves a relative path against the *process* cwd — so `group docs/plan.md
    --repo <elsewhere>` from any other directory failed with a plan that plainly
    exists. `run`/`resume` re-anchor against the repo; the asymmetry was only
    here.

    cwd still wins when it resolves, so an operator standing inside one repo and
    pointing `--repo` at another keeps the path they typed.
    """
    if plan.is_absolute() or plan.is_file():
        return plan
    anchored = repo_root / plan
    return anchored if anchored.is_file() else plan


def _speccer_json_runner(llm_runner: JsonRunner | None, config: OrchestratorConfig) -> JsonRunner:
    """The default one-shot runner for the mapper/speccer path, bound to the
    speccer model (plan U17).

    A caller-supplied ``llm_runner`` (tests, mainly) is returned untouched —
    only the production default gets the model bound, via ``functools.partial``
    so ``claude_json_runner`` itself stays a plain two-arg ``JsonRunner``.
    """
    if llm_runner is not None:
        return llm_runner
    return functools.partial(claude_json_runner, model=config.session.speccer_model)


def _cmd_group(
    args: argparse.Namespace,
    llm_runner: JsonRunner | None,
    client: CodegraphClient | None,
) -> int:
    repo_root = args.repo.resolve()
    plan_path = _anchor_plan_path(args.plan, repo_root)
    name = getattr(args, "name", None) or plan_path.stem
    try:
        validate_grouping_name(name)
    except GroupingNameError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    config = _load_config(args, repo_root)
    if config is None:
        return 1
    # `group` has no run directory, so its gate logs to stdout and writes no
    # state file — but it waits out a limit exactly as a run does. Grouping is a
    # handful of expensive calls; losing the last one to a reset that clears in
    # twenty minutes is the same waste here as mid-run.
    llm_runner = with_usage_limit_retry(
        _speccer_json_runner(llm_runner, config), build_usage_limit_gate(config)
    )
    allow_unknown_symbols = getattr(args, "allow_unknown_symbols", False)
    out_dir = grouping_dir(repo_root, name)
    trace_path = out_dir / "grouping-trace.json"
    # Plan U8: --no-spec and --dry-run never describe the partition committed to
    # groups.json, so their trace/edge-provenance go in a preview subdirectory
    # rather than beside (and possibly overwriting) a real grouping's sibling
    # trace. A directory holding only a preview never gets a groups.json, so it
    # stays invisible to describe_groupings and shows up in the launch-page
    # preview as exactly that — a preview, never a failed grouping.
    preview_dir = out_dir / "preview"
    preview_trace_path = preview_dir / "grouping-trace.json"
    recorder = TraceRecorder()
    # Written on every mode including --no-spec and --dry-run: --no-spec is the
    # debugging mode, so it is exactly when the mapper's reasoning is wanted most.
    llm_recorder = JsonlCallRecorder(out_dir, grouping_run_id=uuid.uuid4().hex)
    # Same rationale as the trace and the LLM records: written on every mode,
    # --no-spec included, because --no-spec is the debugging mode.
    provenance_recorder = EdgeProvenanceRecorder()

    def _progress(message: str) -> None:
        # Unbuffered by construction: `flush=True` forces the write out to the
        # job log file immediately, regardless of stdout's default buffering
        # mode when it is not a tty (plan U24) — this is the whole fix for a
        # grouping job that otherwise shows nothing for three and a half minutes.
        print(f"progress: {message}", flush=True)

    if getattr(args, "no_spec", False):
        try:
            outcome = compute_partition(
                plan_path=plan_path,
                repo_root=repo_root,
                config=config,
                llm_runner=llm_runner,
                client=client,
                allow_unknown_symbols=allow_unknown_symbols,
                recorder=recorder,
                llm_recorder=llm_recorder,
                provenance_recorder=provenance_recorder,
                progress=_progress,
            )
        except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
            _write_failure_trace(preview_dir, recorder, exc, preview_trace_path)
            _write_edge_provenance(preview_dir, provenance_recorder)
            return 1
        _write_trace(preview_dir, recorder)
        _write_edge_provenance(preview_dir, provenance_recorder)
        _append_metrics_log(repo_root, recorder.trace)
        _warn_self_modification(outcome.mapper_out.flags)
        _print_partition_report(recorder.trace)
        return 0

    try:
        result, base_context = run_grouping(
            plan_path=plan_path,
            repo_root=repo_root,
            config=config,
            llm_runner=llm_runner,
            client=client,
            allow_unknown_symbols=allow_unknown_symbols,
            recorder=recorder,
            llm_recorder=llm_recorder,
            provenance_recorder=provenance_recorder,
            progress=_progress,
        )
    except (GrouperError, GraphBuildError, GroupCycleError, LlmError) as exc:
        # A dry run that fails never intended to write groups.json either, so
        # its partial trace belongs in the preview location too.
        failure_dir = preview_dir if args.dry_run else out_dir
        failure_trace_path = preview_trace_path if args.dry_run else trace_path
        _write_failure_trace(failure_dir, recorder, exc, failure_trace_path)
        _write_edge_provenance(failure_dir, provenance_recorder)
        return 1
    _warn_self_modification(result.flags)
    llm_recorder.link_outputs(
        task_ids=[t for group in result.groups for t in group.tasks],
        group_ids=[group.id for group in result.groups],
    )

    if args.dry_run:
        _write_trace(preview_dir, recorder)
        _write_edge_provenance(preview_dir, provenance_recorder)
        _append_metrics_log(repo_root, recorder.trace)
        _print_report(result)
        return 0

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "groups.json").write_text(serialize_grouping(result))
    (out_dir / "base-context.md").write_text(base_context)
    _write_trace(out_dir, recorder)
    _write_edge_provenance(out_dir, provenance_recorder)
    _append_metrics_log(repo_root, recorder.trace)
    print(f"wrote {out_dir / 'groups.json'} and {out_dir / 'base-context.md'}")
    _print_report(result)
    return 0


def _write_edge_provenance(out_dir: Path, recorder: EdgeProvenanceRecorder) -> None:
    """The edge-provenance sidecar (plan P2), written whenever the partition got far
    enough to produce one — a grouping that raised before the graph was built simply
    has nothing to say, so nothing is written."""
    if recorder.document is None:
        return
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "edge-provenance.json").write_text(serialize_edge_provenance(recorder.document))


def _write_trace(out_dir: Path, recorder: TraceRecorder) -> None:
    """Every ``group`` mode writes the trace (plan U9), including modes that
    write nothing else — explaining a partition or a failure is the point."""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "grouping-trace.json").write_text(serialize_trace(recorder.trace))


def _append_metrics_log(repo_root: Path, trace: GroupingTrace) -> None:
    """Append one row to the durable, append-only metrics log (plan U5).

    Called only from the success paths in ``_cmd_group`` — a grouping that
    raises before producing a partition never reaches here, so
    ``.orchestrator/grouping-metrics.jsonl`` gains no line for it. Re-running
    the same grouping name appends a second line rather than replacing the
    first (unlike ``grouping-trace.json``, which ``cli.py``'s ``--name``
    handling overwrites) — this file is a log, not a snapshot.
    """
    if trace.scorecard is None or trace.provenance is None:
        return
    metrics_path = repo_root / ".orchestrator" / "grouping-metrics.jsonl"
    metrics_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "scorecard": trace.scorecard.model_dump(),
        "provenance": trace.provenance.model_dump(),
    }
    with metrics_path.open("a") as f:
        f.write(json.dumps(record) + "\n")


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

    # trace.dag maps upstream_gid -> {downstream_gids} (build_group_dag), so a
    # group's own *upstream* dependencies are found by inverting it — printing
    # trace.dag.get(gid) directly would list gid's dependents, mislabeled as
    # what gid "depends on" (plan U8).
    upstream_by_gid: dict[int, list[int]] = {}
    for up_gid, down_gids in trace.dag.items():
        for down_gid in down_gids:
            upstream_by_gid.setdefault(down_gid, []).append(up_gid)

    print(f"groups: {len(members_by_gid)} (partition-only — no specs, no LLM calls)")
    for gid, members in sorted(members_by_gid.items()):
        gid_str = group_label(gid)
        work = sum(node_work.get(node, 0.0) for node in members)
        upstream = sorted(group_label(up) for up in upstream_by_gid.get(gid, ()))
        print(f"\n{gid_str}:")
        print(f"  tasks: {', '.join(sorted(members))}")
        print(f"  node work: {work:.1f} / budget cap {budget_cap:.1f}")
        print(f"  depends on: {', '.join(upstream) if upstream else 'none'}")

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

    if trace.scorecard is not None:
        sc = trace.scorecard
        print("\nscorecard:")
        print(f"  groups: {sc.group_count}")
        print(f"  cross-group edges: {sc.cross_group_edges}")
        print(
            "  work fraction of cap (min/mean/max): "
            f"{sc.work_fraction_min:.2f} / {sc.work_fraction_mean:.2f} / {sc.work_fraction_max:.2f}"
        )
        print(f"  critical path length: {sc.critical_path_length}")
        print(f"  modularity: {sc.modularity:.3f}")
        print(f"  slice integrity: {'pass' if sc.slice_integrity_ok else 'FAIL'}")

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


def _verify_grouping_index_fingerprint(
    *,
    repo_root: Path,
    groups_path: Path,
    base_context_path: Path,
    grouping: GroupingResult,
    plan_path: Path,
    config: OrchestratorConfig,
    llm_runner: JsonRunner | None,
    client: CodegraphClient | None,
    allow_drift: bool,
) -> GroupingResult:
    """Plan U7: the `run`/`resume` reuse path for a grouping that already
    exists on disk. A grouping directory with no `grouping-trace.json` or no
    recorded provenance (older artifact, or `--no-spec`/`--dry-run` preview)
    has nothing to compare against and is used as-is — silently, exactly like
    a match.

    On mismatch: hard failure unless ``allow_drift``, in which case a full
    re-partition (`run_grouping`, the same pipeline `group` runs) replaces
    `groups_path`/`base_context_path` and the sibling trace in place, and the
    freshly computed result is returned for the caller to execute against —
    never the stale one on disk.
    """
    trace_path = groups_path.parent / "grouping-trace.json"
    if not trace_path.is_file():
        return grouping
    recorded_trace = GroupingTrace.model_validate_json(trace_path.read_text())
    if recorded_trace.provenance is None:
        return grouping

    fp_client = client or CodegraphClient(repo_root=repo_root)
    try:
        _current, matched = verify_index_fingerprint(
            recorded_trace.provenance.index_fingerprint,
            fp_client,
            allow_drift=allow_drift,
            log=lambda message: print(message, file=sys.stderr),
        )
    except GraphBuildError as exc:
        # The current index could not even be read (mirrors plan U2's
        # no-baseline degrade): this is an environmental failure to verify,
        # not evidence the index actually drifted, so it must not be treated
        # as a mismatch — that would turn "codegraph isn't reachable right
        # now" into a false "the partition is stale" verdict.
        print(
            f"warning: could not verify index fingerprint against the current "
            f"index ({exc}) — proceeding without verification",
            file=sys.stderr,
        )
        return grouping
    if matched:
        return grouping

    # allow_drift and mismatched: force a full re-partition rather than
    # silently reusing the recorded groups.json (the plan's explicit
    # requirement — "never a silent reuse").
    recorder = TraceRecorder()
    llm_recorder = JsonlCallRecorder(groups_path.parent, grouping_run_id=uuid.uuid4().hex)
    provenance_recorder = EdgeProvenanceRecorder()
    result, base_context = run_grouping(
        plan_path=plan_path,
        repo_root=repo_root,
        config=config,
        llm_runner=_speccer_json_runner(llm_runner, config),
        client=fp_client,
        recorder=recorder,
        llm_recorder=llm_recorder,
        provenance_recorder=provenance_recorder,
    )
    groups_path.write_text(serialize_grouping(result))
    base_context_path.write_text(base_context)
    (groups_path.parent / "grouping-trace.json").write_text(serialize_trace(recorder.trace))
    if provenance_recorder.document is not None:
        (groups_path.parent / "edge-provenance.json").write_text(
            serialize_edge_provenance(provenance_recorder.document)
        )
    return result


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


def build_usage_limit_gate(
    config: OrchestratorConfig, paths: RunPaths | None = None
) -> UsageLimitGate:
    """The run's one rate-limit gate, with its two visibility sinks attached.

    ``paths=None`` (the ``group`` command, which has no run directory) still gets
    a gate — it logs to stdout and writes no file. The run's gate logs through
    ``log_event``, so every arm/countdown/release line already flows to
    ``/events/log`` and the UI's event pane with no new plumbing, and mirrors its
    state into ``usage-limit.json`` for the snapshot.
    """
    if paths is None:
        return UsageLimitGate(config.session.usage_limit, log=print)

    def publish(state: UsageLimitState) -> None:
        atomic_write_text(paths.usage_limit_path, json.dumps(state.to_dict(), indent=2) + "\n")

    return UsageLimitGate(
        config.session.usage_limit,
        log=lambda message: log_event(paths, message),
        on_change=publish,
    )


def build_auth_ladder(auth: AuthConfig, *, log: Callable[[str], None] | None = None) -> AuthLadder:
    """Rungs (a)+(b) of the auth ladder (plan U4): read expiry, refresh in
    place. Constructed unconditionally — ``auth_gate`` is what actually opts
    the run in (see ``build_auth_gate``), so a ``recover()`` call against a
    disabled ladder is simply never made."""
    path = Path(auth.credentials_path).expanduser() if auth.credentials_path else None
    return AuthLadder(credentials_path=path, log=log)


def build_auth_gate(
    config: OrchestratorConfig, ladder: AuthLadder, paths: RunPaths | None = None
) -> UsageLimitGate | None:
    """The run's auth-pause gate (plan U4 rung c), mirroring
    ``build_usage_limit_gate``: same log/publish sinks, its own
    ``auth-pause.json`` file, and ``probe=ladder.recover`` so the pause
    self-releases the moment the credential is healthy rather than on a fixed
    deadline. ``None`` when the ladder is disabled — ``SessionRunner`` treats a
    ``None`` auth gate exactly like a ``None`` usage-limit gate: a 401 raises
    straight out of the call.
    """
    if not config.session.auth.enabled:
        return None
    auth_config = UsageLimitConfig(
        auto_resume=True,
        max_wait_s=config.session.auth.max_wait_s,
        max_attempts=config.session.auth.max_attempts,
        skew_s=0.0,
        fallback_poll_s=config.session.auth.poll_s,
    )
    if paths is None:
        return UsageLimitGate(auth_config, log=print, probe=ladder.recover, label="credential")

    def publish(state: UsageLimitState) -> None:
        atomic_write_text(paths.auth_pause_path, json.dumps(state.to_dict(), indent=2) + "\n")

    return UsageLimitGate(
        auth_config,
        log=lambda message: log_event(paths, message),
        on_change=publish,
        probe=ladder.recover,
        label="credential",
    )


def build_session_runner(
    config: OrchestratorConfig,
    gate: UsageLimitGate | None = None,
    *,
    auth_ladder: AuthLadder | None = None,
    auth_gate: UsageLimitGate | None = None,
) -> SessionRunner:
    """The one place a production ``SessionRunner`` is built.

    Extracted from ``_cmd_run`` so the wiring is assertable. It was inline, and
    it silently omitted ``confine``/``disallowed_tools``/``settings`` — leaving
    Landlock, the git deny list and per-worker settings unreachable in every real
    run while their own unit tests passed against directly-constructed runners.
    A construction site no test can see is a construction site that drifts.
    """
    session = config.session
    return SessionRunner(
        claude_bin=session.claude_bin,
        model=session.model,
        base_model=session.base_model,
        permission_mode=config.execution.permission_mode,
        allowed_tools=session.allowed_tools or None,
        transcript_root=(
            Path(session.transcript_root).expanduser() if session.transcript_root else None
        ),
        max_thinking_tokens=session.max_thinking_tokens,
        thinking=session.thinking,
        disallowed_tools=session.disallowed_tools or None,
        settings=session.settings,
        confine=session.confine,
        cache_root=_cache_root(session),
        extra_write_paths=[Path(p).expanduser() for p in session.extra_write_paths],
        gate=gate,
        auth_ladder=auth_ladder,
        auth_gate=auth_gate,
    )


def _auto_resume_line(usage_limit: UsageLimitConfig) -> str:
    """How the run will behave when the account limit lands — in the header an
    operator reads before anything spawns, for the same reason HITL is there."""
    if not usage_limit.auto_resume:
        return "auto-resume off (a usage limit stops the run)"
    bound = (
        "no wait limit"
        if usage_limit.max_wait_s <= 0
        else f"waiting at most {int(usage_limit.max_wait_s)}s"
    )
    return f"auto-resume on ({bound}, {usage_limit.max_attempts} attempts)"


def _cache_root(session: SessionConfig) -> Path:
    """The orchestrator-owned cache root for this run, resolved once."""
    if session.cache_root:
        return Path(session.cache_root).expanduser()
    return default_cache_root()


def _cmd_run(
    args: argparse.Namespace,
    llm_runner: JsonRunner | None,
    client: CodegraphClient | None = None,
    *,
    resume: bool,
) -> int:
    repo_root = args.repo.resolve()
    run_id = args.run_id if resume else (args.run_id or _default_run_id())
    paths = RunPaths(repo_root, run_id)
    orch_dir = repo_root / ".orchestrator"

    # Peeked early (before config is resolved) so a resumed run's own recorded
    # escalation tier can slot into `_load_config` beneath CLI flags but above
    # the library default (plan U2) — reused below at the manifest-load site
    # instead of reading `manifest.json` twice.
    store = ManifestStore(paths)
    persisted_manifest: RunManifest | None = None
    if resume and store.exists():
        persisted_manifest = store.load()

    config = _load_config(
        args,
        repo_root,
        persisted_escalation=(
            persisted_manifest.escalation if persisted_manifest is not None else None
        ),
        persisted_usage_limit=(
            persisted_manifest.usage_limit if persisted_manifest is not None else None
        ),
    )
    if config is None:
        return 1

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

    # Plan U7: the grouping's own recorded index fingerprint, read back and
    # compared against the current codegraph index (never done before this
    # unit — the fingerprint was written into `ProvenanceEntry` and never read
    # back). A mismatch here means `groups_path` was built against an index
    # that no longer describes the repo `run`/`resume` is about to execute
    # against.
    try:
        grouping = _verify_grouping_index_fingerprint(
            repo_root=repo_root,
            groups_path=groups_path,
            base_context_path=base_context_path,
            grouping=grouping,
            plan_path=plan_path,
            config=config,
            llm_runner=llm_runner,
            client=client,
            allow_drift=getattr(args, "allow_index_drift", False),
        )
    except IndexFingerprintMismatch as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    groups = grouping.groups
    intensity_override_line: str | None = None
    if getattr(args, "review_intensity", None):
        intensity = ReviewIntensity(args.review_intensity)
        changed = sum(1 for group in groups if group.intensity != intensity)
        groups = [group.model_copy(update={"intensity": intensity}) for group in groups]
        if changed:
            sessions = changed * _REVIEWER_SESSIONS[intensity]
            intensity_override_line = (
                f"warning: --review-intensity {intensity.value} overrides {changed} "
                f"group(s)' computed intensity — implies {sessions} reviewer session(s) "
                "for those groups (omit the flag to keep each group's recorded intensity)"
            )

    # Plan U7: the config file actually loaded, echoed with its own path before
    # anything spawns — two `.orchestrator/config.toml` files were once found
    # to disagree on `context_token_limit` (200000 vs 120000), and `.orchestrator/`
    # is gitignored so that drift never shows up in a diff. An operator who only
    # sees the resolved values (as the R8 line below already prints) has no way
    # to tell which file produced them.
    config_path = (args.config or repo_root / ".orchestrator" / "config.toml").resolve()
    print(
        f"config: {_config_banner_source(config_path)} (token_budget={config.estimator.token_budget}, "
        f"context_token_limit={config.breaker.context_token_limit}, "
        f"permission_mode={config.execution.permission_mode})"
    )

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
    # Whether workers are actually confined belongs in the header next to the
    # permission mode, not in a warning that scrolls past: `confine = true` with
    # no Landlock is a silently weaker run, and that is the case an operator most
    # needs to see before it starts.
    if not config.session.confine:
        confinement = "confinement off"
    else:
        abi = landlock_abi_version()
        confinement = (
            f"confinement on (landlock abi {abi})"
            if abi > 0
            else "confinement on but UNAVAILABLE (no landlock; deny-rules only)"
        )
    # Belt and braces with `main`'s line-buffering: this is the one line an
    # operator greps for immediately after backgrounding a run, so it must be on
    # disk before anything else happens.
    print(
        f"run {run_id}: {len(groups)} group(s), {mode}, {hitl}, "
        f"permission-mode {config.execution.permission_mode}, {confinement}, "
        f"{_auto_resume_line(config.session.usage_limit)}, "
        f"on-failure {config.execution.on_group_failure}, "
        f"cache {_cache_root(config.session)}",
        flush=True,
    )
    if intensity_override_line is not None:
        print(intensity_override_line)

    driver_lock = DriverLock(paths)
    try:
        gate = build_usage_limit_gate(config, paths)
        auth_ladder = build_auth_ladder(
            config.session.auth, log=lambda message: log_event(paths, message)
        )
        auth_gate = build_auth_gate(config, auth_ladder, paths)
        runner = build_session_runner(config, gate, auth_ladder=auth_ladder, auth_gate=auth_gate)
        try:
            runner.preflight()
        except SessionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        # Acquired only once preflight has passed, so a dead worker CLI still
        # never leaves a run directory behind (the lock file lives under
        # `run_dir`, same as the frozen grouping snapshot right below it) — and
        # held for the rest of this function (plan U11): a second `run`/`resume`
        # over the same run id must fail here, fast, rather than after paying
        # for a base-session fork a losing race would then have no use for.
        try:
            driver_lock.acquire()
        except DriverAlreadyRunning as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        # The run keeps its own frozen copy of the grouping it started with (plan
        # U10): a later `group --name <same>` against a different plan must not be
        # able to rewrite a finished run's history. Done only after preflight
        # succeeds, so a dead worker CLI never leaves a run directory behind.
        if not resume:
            snapshot_grouping(source_grouping_dir, paths.run_dir)
            # Plan U2: what was already red on the launch branch, captured once
            # before any group worktree exists — a resumed run reuses it rather
            # than recapturing against a launch branch it no longer sits on.
            launch_commit_sha = _git(repo_root, "rev-parse", "HEAD").stdout.strip()
            baseline = capture_preflight_baseline(
                repo_root,
                config=config.preflight,
                output_dir=paths.run_dir,
                commit_sha=launch_commit_sha,
                log=lambda message: log_event(paths, message),
            )
            save_baseline(paths.preflight_baseline_path, baseline)
        # Plan U3/R41: the resolved admission policy, recorded once — an operator
        # reading logs/run.log after the fact must be able to tell whether a halted
        # run was the default or an explicit --on-failure override.
        log_event(paths, f"run {run_id}: on_group_failure={config.execution.on_group_failure}")

        merger = IntegrationMerger(
            repo_root,
            run_id,
            preflight_config=config.preflight,
            preflight_output_dir=paths.group_dir,
            log=lambda message: log_event(paths, message),
            # The integration worktree is the tree that represents this run's
            # output (plan U32) — provisioned the same way a group worktree is,
            # with the same cache locality (worker_cache_env avoids warming the
            # operator's own `~/.cache/uv`, see `_workspace_seams`).
            provision_args=config.session.provision_args,
            provision_env_vars=worker_cache_env(_cache_root(config.session), base=dict(os.environ)),
        )
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

        resolve_deps = _resolve_deps(
            repo_root,
            run_id,
            merger,
            runner=runner,
            store=store,
            execution=config.execution,
            paths=paths,
        )

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
                breaker=config.breaker,
                resume=resume,
                broker=broker,
                policy=policy,
                resolve=resolve_deps,
            )
        except (SchedulerError, RunStateVersionError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        runner.tracker = scheduler.tracker
        # Wired after construction (plan U7): the broker is built before the
        # scheduler exists (the scheduler's resolve routine needs it), so its
        # stdout line naming blocked groups can only be plugged in here.
        if broker is not None:
            broker.pending_groups_provider = scheduler.pending_group_ids

        if resume:
            if persisted_manifest is None:
                print(f"error: no manifest at {paths.manifest_path}", file=sys.stderr)
                return 1
            manifest = persisted_manifest
            if not manifest.base_session_id:
                print("error: manifest has no base session — start a fresh run", file=sys.stderr)
                return 1
            base_session_id = manifest.base_session_id
        else:
            # Establishing the base session is the *first* long silence an operator
            # meets — it precedes every group, so no group heartbeat exists yet and
            # the run said nothing at all until it returned. Same machinery as a
            # group's, run-scoped: 15s file tick, one log line a minute.
            base_heartbeat = RoundHeartbeat(
                paths, None, log=lambda message: log_event(paths, message)
            )
            base_heartbeat.mark_phase("establishing the base session")
            base_heartbeat.start()
            try:
                base = runner.start_base(
                    run_id=run_id, base_context=base_context_path.read_text(), cwd=repo_root
                )
            except SessionError as exc:
                print(f"error: base session failed: {exc}", file=sys.stderr)
                return 1
            finally:
                base_heartbeat.stop()
            base_session_id = base.session_id
            manifest = RunManifest(
                run_id=run_id,
                plan_path=grouping.plan_path,
                base_session_id=base_session_id,
                grouping=grouping_name,
                escalation=config.escalation,
                usage_limit=config.session.usage_limit,
                launch_branch=_resolve_launch_branch(repo_root),
            )
            store.save(manifest)
            # Snapshot the DAG beside the manifest: `.orchestrator/groups.json` is
            # shared across runs and every planning cycle overwrites it, so without
            # this a post-mortem reader renders whatever DAG is on disk today
            # (ADR 0002). Resume keeps the snapshot its run started with.
            atomic_write_text(paths.groups_path, groups_path.read_text())

        workspace_for, base_ref_for = _workspace_seams(
            repo_root, run_id, merger, paths, config.session
        )
        deps = ReviewDeps(
            run_id=run_id,
            runner=runner,
            store=store,
            manifest=manifest,
            base_session_id=base_session_id,
            breaker=config.breaker,
            execution=config.execution,
            # groups=grouping.groups (plan U11): validates every affected_groups
            # id against the run's real group and task ids at mark time, instead
            # of silently accumulating dead buckets under ids nothing will ever
            # read.
            board=SurpriseBoard(paths, groups=grouping.groups),
            workspace_for=workspace_for,
            merge_group=merger.merge_group,
            # The rewrite path is the run's other claude call, and it is a one-shot
            # `claude -p` rather than a session — so it needs the same gate, applied
            # at its own boundary.
            rewrite_spec=_rewrite_provider(
                plan_text,
                with_usage_limit_retry(_speccer_json_runner(llm_runner, config), gate),
                orch_dir / "failures",
                recorder=JsonlCallRecorder(paths.run_dir, grouping_run_id=run_id),
            ),
            base_ref_for=base_ref_for,
            broker=broker,
            policy=policy,
            # plan U3: read back regardless of resume, so a resumed run's merge
            # gate still knows what was already red on the launch branch.
            preflight_baseline=load_baseline(paths.preflight_baseline_path),
        )
        executor_slot.append(make_executor(deps))

        try:
            with _interruptible_pause(gate):
                asyncio.run(scheduler.run())
        except RunAbort as exc:
            # The operator stopped the run; state stays resumable (mid-flight groups
            # restart from ready on `resume`).
            print(f"run aborted by operator: {exc}", file=sys.stderr)
            log_event(paths, f"run {run_id} aborted by operator: {exc}")
            _print_outcomes(scheduler.state, paths)
            print(f"resume with: smart-mcps-orchestrate resume {run_id}", file=sys.stderr)
            return 2
        except KeyboardInterrupt:
            # Ctrl-C previously left state.json indistinguishable from a live run, so
            # the next reader had to diff mtimes against a worker transcript to find
            # out the run was dead. Record it, say so, and point at resume.
            scheduler.mark_interrupted()
            log_event(paths, f"run {run_id} interrupted (SIGINT)")
            print("\nrun interrupted", file=sys.stderr)
            _print_outcomes(scheduler.state, paths)
            print(f"resume with: smart-mcps-orchestrate resume {run_id}", file=sys.stderr)
            return 130
        except SchedulerError as exc:
            print(f"error: {exc}", file=sys.stderr)
            _print_outcomes(scheduler.state, paths)
            return 1
        _maybe_auto_finish(repo_root, run_id, paths)
        return _print_outcomes(scheduler.state, paths)
    finally:
        driver_lock.release()


def _maybe_auto_finish(repo_root: Path, run_id: str, paths: RunPaths) -> None:
    """Invoke `finish` itself once every group is provably done (plan U8
    Decisions) — completed/resolved *and* its branch an ancestor of the
    integration tip; any other outcome just prints the command, and touches
    no worktree or branch. Never raises: a finish failure is reported and the
    run's own exit code still reflects the groups' outcomes."""
    finish_cmd = f"smart-mcps-orchestrate finish --repo {repo_root} {run_id}"
    ok, _ = run_is_finishable(repo_root, run_id)
    if not ok:
        print(f"finish when ready with: {finish_cmd}")
        return
    try:
        finish_run(repo_root, run_id, log=lambda m: log_event(paths, m))
    except FinishError as exc:
        print(f"error: finish failed: {exc} — retry with: {finish_cmd}", file=sys.stderr)


@contextmanager
def _interruptible_pause(gate: UsageLimitGate):
    """Make Ctrl-C release a usage-limit pause on its way through.

    Worker calls run in ``asyncio.to_thread`` pool threads, and ``asyncio.run``
    *joins that pool* as it unwinds — so a thread parked in a five-hour pause
    would hold the process open long after the operator pressed Ctrl-C, and the
    existing ``except KeyboardInterrupt`` below could not help: it runs after
    that join, not before. Cancelling from the signal handler itself is what
    makes the ordering work.

    The default behaviour is preserved exactly: the handler re-raises
    ``KeyboardInterrupt``, so every existing Ctrl-C path still runs. Installing a
    handler needs the main thread, and a caller that is not on it (an embedding
    test) simply keeps the default handler rather than failing the run.
    """
    try:
        previous = signal.signal(signal.SIGINT, _cancelling_handler(gate))
    except ValueError:  # not the main thread — nothing to install
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


def _cancelling_handler(gate: UsageLimitGate):
    def handler(signum, frame):  # noqa: ANN001 — signal handler signature
        gate.cancel()
        raise KeyboardInterrupt

    return handler


def _default_run_id() -> str:
    """Short, filesystem- and ref-safe; lands in branch names and session names."""
    return datetime.now(UTC).strftime("r%Y%m%d-%H%M%S")


def _resolve_launch_branch(repo_root: Path) -> str | None:
    """The branch `run` was launched from (plan U8, R29), resolved once at run
    start so `finish`'s PR base is a real branch, never `HEAD` and never a
    commit sha. None on a detached HEAD."""
    result = _git(repo_root, "symbolic-ref", "--short", "-q", "HEAD")
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _workspace_seams(
    repo_root: Path,
    run_id: str,
    merger: IntegrationMerger,
    paths: RunPaths,
    session: SessionConfig,
):
    """The workspace_for / base_ref_for pair, sharing one tip capture per group.

    The integration tip is read once per group at its ready→running transition —
    an interleaved sibling merge must not move a group's diff base between the
    reviewer's diff and the handoff's diff_stat. On resume the branch already
    exists, so the diff base is its original fork point (merge-base), not today's
    tip.
    """
    tips: dict[str, str] = {}
    # The same cache root the workers get. `provision_env` runs unconfined in this
    # process, so this is about cache *locality*, not permission: without it the
    # sync warms the operator's `~/.cache/uv` and every worker then rebuilds the
    # same downloads in the orchestrator's root, cold.
    cache_env = worker_cache_env(_cache_root(session), base=dict(os.environ))

    def workspace_for(group: Group) -> Path:
        branch = group_branch(run_id, group.id)
        tip = merger.tip()
        path = create_worktree(
            repo_root,
            run_id=run_id,
            group_id=group.id,
            name=group.name,
            branch=branch,
            start_point=tip,
        )

        def _record(state: str, argv: list[str]) -> None:
            # U32: kept beside the group's other run artifacts, not inside the
            # worktree, so it outlives a clean-merge teardown (remove_worktree).
            write_provisioning_record(
                paths.group_dir(group.id), worktree=path, command=argv, state=state
            )

        # U6/R16: the worktree owns its environment — provision after creation,
        # non-fatally (a failed sync logs and lets the worker re-sync itself).
        provision_env(
            path,
            log=lambda message: log_event(paths, message),
            env=cache_env,
            extra_args=session.provision_args,
            on_state=_record,
        )
        tips[group.id] = _git_ok(repo_root, "merge-base", tip, branch).strip()
        return path

    def base_ref_for(group: Group) -> str:
        return tips[group.id]

    return workspace_for, base_ref_for


def _resolve_deps(
    repo_root: Path,
    run_id: str,
    merger: IntegrationMerger,
    *,
    runner: SessionRunner,
    store: ManifestStore,
    execution: ExecutionConfig,
    paths: RunPaths,
) -> ResolveDeps:
    """Wires the scheduler's resolve routine (plan U2) to real git, translating
    ``MergeConflict`` into the scheduler's own ``ResolveConflict`` so scheduler.py
    never has to import merge/review machinery (review.py already imports
    scheduler.py — a reverse import there would cycle).

    ``merge_for_resolve`` also carries U5's conflict ladder: up to
    ``execution.max_conflict_resolve_attempts`` in-place resolution attempts,
    warm-resuming the group's own recorded coder session, before giving up and
    raising ``ResolveConflict`` — the same shape the approved path already
    uses in ``_GroupExecution._resolve_conflict_in_place``, transplanted here
    because the resolve path has no live ``_GroupExecution`` to call it on. A
    Preflight failure is not a conflict: it raises ``ResolvePreflightFailed``,
    which ``Scheduler._resolve_autonomously`` catches and turns into a
    (non-run-stopping) FAILED outcome, the work committed and unmerged on the
    group's own branch.
    """

    def branch_for(group: Group) -> str:
        return group_branch(run_id, group.id)

    def worktree_for(group: Group) -> Path:
        return worktree_path(repo_root, run_id, group.id, group.name)

    def commit_stranded(group: Group) -> bool:
        return commit_all(worktree_for(group), f"resolve({run_id}): {group.id} stranded work")

    def commits_ahead_fn(group: Group) -> int:
        return commits_ahead(merger.ensure(), merger.branch, branch_for(group))

    def latest_coder_session_id(gid: str) -> str | None:
        """The group's own recorded coder session (plan U5): re-read from disk
        at call time, since resolve can run long after ``_resolve_deps`` was
        built and manifest state changes throughout the run."""
        if not store.exists():
            return None
        manifest = store.load()
        entry = manifest.groups.get(gid)
        if entry is None:
            return None
        for session in reversed(entry.sessions):
            if session.role == SessionRole.CODER:
                return session.session_id
        return None

    def attempt_conflict_resolve(
        group: Group, worktree: Path, session_id: str, exc: MergeConflict
    ) -> bool:
        try:
            result = runner.resume(
                session_id=session_id,
                prompt=render_conflict_resolve_prompt(
                    group, conflict_summary=str(exc), integration_branch=merger.branch
                ),
                cwd=worktree,
            )
            report, _ = nudge_until_report(
                runner,
                result,
                CoderReport,
                cwd=worktree,
                verification_ids=[item.id for item in group.verification],
            )
        except (SessionError, ReportError) as inner_exc:
            log_event(
                paths,
                f"group {group.id}: resolve in-place conflict resolution attempt failed: {inner_exc}",
            )
            return False
        if report.status != "completed":
            log_event(
                paths,
                f"group {group.id}: resolve conflict resolution attempt ended ({report.status})",
            )
            return False
        return True

    def merge_for_resolve(group: Group) -> None:
        worktree = worktree_for(group)
        attempts_left = execution.max_conflict_resolve_attempts
        while True:
            try:
                merger.merge_group(group, worktree)
                return
            except MergeConflict as exc:
                session_id = latest_coder_session_id(group.id) if attempts_left > 0 else None
                if session_id is None:
                    raise ResolveConflict(f"resolving group {group.id}: {exc}") from exc
                attempts_left -= 1
                log_event(
                    paths,
                    f"group {group.id}: resolve — attempting in-place conflict resolution "
                    f"({attempts_left} attempt(s) left)",
                )
                if not attempt_conflict_resolve(group, worktree, session_id, exc):
                    raise ResolveConflict(f"resolving group {group.id}: {exc}") from exc
                # loop retries the merge against the resolved worktree
            except PreflightFailure as exc:
                retry_cmd = f"smart-mcps-orchestrate retry --repo {repo_root} {run_id} {group.id}"
                log_event(
                    paths,
                    f"group {group.id}: resolve preflight failed on branch "
                    f"{branch_for(group)}: {exc} — retry with: {retry_cmd}",
                )
                raise ResolvePreflightFailed(str(exc)) from exc
            except MergeError:
                return  # commits_ahead already gated this — defensive no-op

    return ResolveDeps(
        commit_stranded=commit_stranded,
        commits_ahead=commits_ahead_fn,
        merge_group=merge_for_resolve,
    )


def _rewrite_provider(
    plan_text: str,
    llm_runner: JsonRunner,
    failure_dir: Path,
    recorder: JsonlCallRecorder | None = None,
):
    """rewrite_spec seam: one-group skeleton through the Phase A speccer, with the
    surprises folded in as rewrite context (they are never empty on escalation
    paths — Phase B synthesizes a context surprise for blocked/too_hard/etc.).

    ``recorder``, when given, appends each rewrite speccer call to the run's own
    ``llm/calls.json`` (plan U14) — the same record shape grouping-time speccer
    calls already get, so a rewrite's cost, prompt and response survive."""

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
        spec = write_specs(
            plan_text, skeleton, llm_runner, failure_dir=failure_dir, recorder=recorder
        )[group.id]
        return group.model_copy(
            update={
                "name": spec.name,
                "summary": spec.summary,
                "spec": spec.spec,
                "verification": spec.verification,
            }
        )

    return rewrite_spec


def _print_outcomes(state: RunState, paths: RunPaths | None = None) -> int:
    """Print every group's outcome plus, for anything stalled, enough to act on
    it without diffing state.json (plan U3/R41): its failure text, what it
    holds and on which files, its branch, its re-entry count, and the command
    to act on it. Read-only — it derives everything from the already-persisted
    ``holds`` field each group carries from the scheduler's last admission
    pass, so calling it never changes state.json.

    ``paths`` is optional so every existing caller/test naming only ``state``
    keeps working unchanged; when given, the surprise-board residue (plan U12)
    is printed too, so an operator learns what never got delivered without a
    hand read of ``surprises.json``.
    """
    print(f"\nrun {state.run_id}:")
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        line = f"  {gid}: {entry.state.value}"
        if entry.generation > 1:
            line += f" (generation {entry.generation})"
        if entry.failure:
            line += f" — {entry.failure}"
        if entry.quarantined:
            line += " [quarantined]"
        print(line)
    if paths is not None:
        print(format_residue_report(surprise_residue(paths, state)))
    completed = all(entry.state == GroupState.COMPLETED for entry in state.groups.values())
    if completed:
        print("all groups completed; merge the integration branch when ready")
        return 0

    # Invert each group's persisted holds into "who does gid hold, and on what":
    # a FAILURE_GATE hold on gid2 naming gid1 means gid1 is holding gid2.
    holds_by: dict[str, list[tuple[str, list[str]]]] = {}
    halted_by: str | None = None
    not_admitted: list[str] = []
    for held_gid, entry in state.groups.items():
        for hold in entry.holds:
            if hold.reason == HoldReason.FAILURE_GATE:
                holds_by.setdefault(hold.group_id, []).append((held_gid, hold.files))
            elif hold.reason == HoldReason.RUN_HALTED:
                halted_by = hold.group_id
                not_admitted.append(held_gid)
    not_admitted.sort()

    stalled = sorted(
        gid
        for gid, entry in state.groups.items()
        if entry.state in (GroupState.FAILED, GroupState.INTERRUPTED)
    )
    for gid in stalled:
        entry = state.groups[gid]
        branch = group_branch(state.run_id, gid)
        overlap = sorted(holds_by.get(gid, []))
        held = (
            "; ".join(f"{other} ({', '.join(files)})" for other, files in overlap)
            if overlap
            else "none"
        )
        command = (
            f"smart-mcps-orchestrate retry --repo <repo> {state.run_id} {gid}"
            if entry.quarantined
            else f"smart-mcps-orchestrate resume {state.run_id}"
        )
        print(
            f"  {gid} ({entry.state.value}): {entry.failure or 'no failure text recorded'} — "
            f"holds: {held} — branch {branch} — reentry_count {entry.reentry_count} — {command}"
        )

    if halted_by is not None:
        trigger_state = state.groups[halted_by].state.value
        clears = (
            f"smart-mcps-orchestrate retry --repo <repo> {state.run_id} {halted_by}"
            if state.groups[halted_by].state == GroupState.FAILED
            else f"smart-mcps-orchestrate resume {state.run_id}"
        )
        print(
            f"\nrun halted: group {halted_by} ended {trigger_state}, so no further group "
            f"was admitted — not admitted: {', '.join(not_admitted) if not_admitted else 'none'}. "
            f"Fix {halted_by}, then `{clears}`; or re-run with `--on-failure overlap` "
            "to admit as far as possible."
        )

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
    # Said before the group list, because it changes how every line below
    # reads: a RUNNING group under a run nothing is driving is not running.
    # Two separate facts (plan U11): whether a process holds the driver lock
    # at all, and — only if one does — whether it looks like it is still
    # making progress, read from the freshest active group's heartbeat mtime
    # rather than the driver record's own `updated_at` (which advances just
    # because the process is alive, wedged or not).
    active_group_ids = [
        gid
        for gid, entry in state.groups.items()
        if entry.state in (GroupState.RUNNING, GroupState.REVIEWING, GroupState.MERGING)
    ]
    print(driver_status_line(paths, active_group_ids=active_group_ids))
    if state.interrupted_at is not None:
        print(f"interrupted at {state.interrupted_at}")
    if manifest is not None:
        print(f"plan: {manifest.plan_path}")
        print(f"base session: {manifest.base_session_id}")
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        line = f"\n{gid}: {entry.state.value} (generation {entry.generation})"
        if entry.failure:
            line += f"\n  failure: {entry.failure}"
        for hold in entry.holds:
            # Each hold reason reads differently on purpose (plan U9): a DAG
            # dependency, U2's failure gate, and U9's concurrent-overlap
            # exclusion are three different situations with three different fixes.
            shared = f" on {', '.join(hold.files)}" if hold.files else ""
            line += f"\n  held ({hold.reason.value}) by {hold.group_id}{shared}"
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
    resolve an escalation without touching the running process. The write itself
    lives in ``escalation.answer_escalation`` so the CLI and the Observatory's
    HTTP endpoint share one implementation of the contract."""
    paths = RunPaths(args.repo.resolve(), args.run_id)
    try:
        answer_escalation(paths, args.esc_id, args.action, args.text)
    except EscalationError as exc:
        print(f"error: {exc} (check `status {args.run_id}`)", file=sys.stderr)
        return 1
    print(f"answered {args.esc_id}: {args.action}")
    return 0


# --------------------------------------------------------------------- retry


def _cmd_retry(args: argparse.Namespace) -> int:
    """Release a terminally failed or quarantined group (plan U7): the
    deliberate operator override — everything else in the system treats both
    outcomes as something a plain `resume` must not touch on its own."""
    repo_root = args.repo.resolve()
    try:
        retry_group(repo_root, args.run_id, args.group_id)
    except RetryConflictError as exc:
        print(
            f"error: {exc} — conflicting file(s): {', '.join(exc.paths)}",
            file=sys.stderr,
        )
        return 1
    except RetryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"group {args.group_id} released — resume with: smart-mcps-orchestrate resume {args.run_id}"
    )
    return 0


# -------------------------------------------------------------------- finish


def _cmd_finish(args: argparse.Namespace) -> int:
    """Push the integration branch, open a PR, tear down merged groups
    (plan U8/U9) — callable directly by an operator, same routine `run`
    invokes itself once every group is provably merged."""
    repo_root = args.repo.resolve()
    paths = RunPaths(repo_root, args.run_id)
    if not paths.state_path.is_file():
        print(f"error: no run state at {paths.state_path}", file=sys.stderr)
        return 1
    try:
        finish_run(repo_root, args.run_id, log=lambda m: log_event(paths, m))
    except FinishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


# -------------------------------------------------------------------- calibrate


def _cmd_calibrate(args: argparse.Namespace) -> int:
    """Report what a finished run predicted against what it cost, so
    ``coder_slack_multiplier`` can be set from accumulated runs rather than a
    hand-measured sample. Read-only: it prints, it never edits config."""
    repo_root = args.repo.resolve()
    paths = RunPaths(repo_root, args.run_id)
    if not paths.state_path.is_file():
        print(f"error: no run state at {paths.state_path}", file=sys.stderr)
        return 1
    config = _load_config(args, repo_root)
    if config is None:
        return 1
    calibration = calibrate_run(paths, config.estimator.coder_slack_multiplier)
    print(format_calibration(args.run_id, calibration))
    return 0


# ------------------------------------------------------------------------- ui


def _cmd_ui(args: argparse.Namespace) -> int:
    """Serve the Observatory: read-only run views plus the one HITL write path.

    Local single-user tool — bound to 127.0.0.1 with no auth, per the plan."""
    try:
        import uvicorn

        from orchestrator.observatory.app import create_app
        from orchestrator.observatory.registry import default_registry_path
    except ImportError as exc:  # pragma: no cover - dependency smoke path
        print(f"error: the Observatory needs fastapi and uvicorn installed: {exc}", file=sys.stderr)
        return 1

    # An omitted --registry means the documented default path, not "no registry":
    # passing None here made the default `~/.orchestrator-ui.yaml` unreachable and
    # every launch fall back to --repo. load_registry still falls back when the
    # default path does not exist, so zero-config launches are unchanged.
    registry = args.registry.expanduser() if args.registry else default_registry_path()
    app = create_app(registry_path=registry, fallback_repo=args.repo.resolve())
    print(f"Observatory on http://{OBSERVATORY_HOST}:{args.port}")
    uvicorn.run(app, host=OBSERVATORY_HOST, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
