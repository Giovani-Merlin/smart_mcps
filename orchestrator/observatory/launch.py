"""Starting work from the Observatory: group a plan, start a run, resume one.

This reverses a documented non-goal. The UI's only write used to be answering an
escalation, and "no launching, resuming or aborting a run from the UI" was
carried verbatim in three places. It was the right call while the UI was young;
it stopped being right the moment ``--intensity`` turned out to be droppable on
a terminal ``resume`` (a run silently reverting to block-forever HITL) — a form
with the tier as a visible field is a *safer* surface than a remembered flag,
not a more dangerous one. The three comments that stated the non-goal are
rewritten rather than left contradicting the code.

Shape follows the escalation precedent: the mechanism lives in plain testable
functions here, and the route layer's own job is mapping their failures onto
status codes.

Three things are worth knowing before editing this module.

**argv, never a shell.** ``build_argv`` is the single place a UI option becomes
a CLI flag, it returns a list, and it starts ``[sys.executable, "-u", "-m",
"orchestrator.cli"]``. The interpreter is this process's own, so there is no
PATH dependency and no console-script resolution; ``-u`` is there because the
console-script entry point cannot pass it and a block-buffered job log reads
exactly like a hung job (finding P5).

**Jobs are detached; job liveness is pid-derived, run liveness is not.**
``start_new_session=True`` means a run outlives the UI process that started
it — restarting the server must not kill a four-hour run. The consequence is
that this server can never ``wait()`` on the child (it is not its parent
after a restart, and even before one nothing reaps it), so a *job's* running
flag is answered by ``os.kill(pid, 0)``-plus-``started_at``, which stays a
real approximation (``pid_alive`` cross-checks against ``/proc`` start time
where available, to close the reboot-recycled-pid case as far as it can be
closed without a lock of its own). A *run's* liveness (``run_liveness``,
``check_not_live``) does not use this at all — plan U11 rebuilt it on the
driver's advisory ``flock`` (`orchestrator.execution.driver`), which the
kernel releases on any process death with no staleness window.

**Double-launch is refused, not de-duplicated.** Two schedulers over one set of
worktrees is the worst thing this surface could do, and a double-clicked button
is the ordinary way to ask for it. ``check_not_live`` is a fast pre-check
against the driver lock, not itself the final arbiter — the real exclusion is
the launched process's own ``DriverLock.acquire()``, which a losing race still
hits: its spawned subprocess starts, fails to acquire, and exits immediately
with an error instead of two schedulers silently sharing one set of worktrees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from orchestrator.config import (
    DEFAULT_BASE_MODEL,
    DEFAULT_SPECCER_MODEL,
    DEFAULT_WORKER_MODEL,
    load_config,
)
from orchestrator.execution.driver import is_driving, read_driver_record
from orchestrator.execution.manifest import RunPaths, atomic_write_text, describe_groupings
from orchestrator.observatory.events import tail_file
from orchestrator.observatory.runs import resolve_repo

JobKind = Literal["group", "run", "resume"]

#: Where plan documents are looked for when the request names no path. Kept a
#: convention rather than config: the picker is a convenience, and the free-text
#: path field is the escape hatch for a plan that lives anywhere else.
PLAN_GLOBS = ("docs/plans/*.md", "docs/*plan*.md")

JOBS_DIRNAME = "jobs"


class LaunchError(Exception):
    """A launch was refused for a reason the operator can act on."""


class ConflictError(LaunchError):
    """A run is already live; starting a second scheduler over it is refused."""


# ------------------------------------------------------------------- options


class ExecutionOptions(BaseModel):
    """Every field of ``cli._add_execution_args``, and nothing else.

    Mirrored one-for-one on purpose: the moment this surface grows an option the
    CLI does not have (or drops one it does), the UI and the terminal start
    producing different runs from the same intent — which is the class of bug
    the droppable ``--intensity`` already demonstrated.

    ``None`` means "not specified", which the CLI resolves the same way it does
    for an omitted flag: config file, then default. It never means "off".
    """

    sequential: bool = False
    concurrency: int | None = None
    permission_mode: str | None = None
    review_intensity: str | None = None
    hitl: bool = False
    intensity: Literal["autonomous", "on_failure", "on_stuck", "interactive"] | None = None
    escalation_source: Literal["orchestrator_only", "workers_via_orchestrator"] | None = None
    escalation_timeout: float | None = None
    auto_resume: bool | None = None
    # Plan U18: the three model knobs (U17/U36), exposed on the form. `None`
    # means "not specified" exactly as the others do — the CLI falls through to
    # the config file, then the built-in default.
    model_worker: str | None = None
    model_base: str | None = None
    model_speccer: str | None = None

    def to_argv(self) -> list[str]:
        argv: list[str] = []
        if self.sequential:
            argv.append("--sequential")
        if self.concurrency is not None:
            argv += ["--concurrency", str(self.concurrency)]
        if self.permission_mode:
            argv += ["--permission-mode", self.permission_mode]
        if self.review_intensity:
            argv += ["--review-intensity", self.review_intensity]
        if self.hitl:
            argv.append("--hitl")
        if self.intensity:
            argv += ["--intensity", self.intensity]
        if self.escalation_source:
            argv += ["--escalation-source", self.escalation_source]
        if self.escalation_timeout is not None:
            argv += ["--escalation-timeout", str(self.escalation_timeout)]
        if self.auto_resume is not None:
            argv.append("--auto-resume" if self.auto_resume else "--no-auto-resume")
        if self.model_worker:
            argv += ["--model-worker", self.model_worker]
        if self.model_base:
            argv += ["--model-base", self.model_base]
        if self.model_speccer:
            argv += ["--model-speccer", self.model_speccer]
        return argv


class ResolvedOptions(BaseModel):
    """Every execution option's *effective* default, resolved exactly as the CLI
    would resolve it for a run started with no flags at all: config file, then
    the library default (``apply_overrides`` with an all-``None`` namespace).

    This is what the launch form shows next to a field left unspecified (plan
    U18/F14) — most importantly ``concurrency``, whose library default of 1 ran
    thirteen groups serially on a DAG whose widest wave was three, with nothing
    on the form suggesting that is what leaving it blank means.
    """

    concurrency: int
    permission_mode: str
    escalation_intensity: Literal["autonomous", "on_failure", "on_stuck", "interactive"]
    escalation_source: Literal["orchestrator_only", "workers_via_orchestrator"]
    escalation_timeout: float | None
    auto_resume: bool
    model_worker: str
    model_base: str
    model_speccer: str
    # F2: the model choices the launch form's dropdowns offer. Served here
    # rather than on a new endpoint because the UI already fetches the resolved
    # options for every form. Advisory only — the CLI accepts any string.
    known_models: list[str]


def resolve_options(repo: Path) -> ResolvedOptions:
    """The values an unspecified execution option would resolve to right now —
    the project's ``config.toml`` layered over the library defaults, with no CLI
    flags involved. Pure read: no run, no job, is required for this to answer."""
    config = load_config(repo / ".orchestrator" / "config.toml")
    return ResolvedOptions(
        concurrency=config.execution.concurrency,
        permission_mode=config.execution.permission_mode,
        escalation_intensity=config.escalation.intensity,
        escalation_source=config.escalation.source,
        escalation_timeout=config.escalation.timeout_s,
        auto_resume=config.session.usage_limit.auto_resume,
        model_worker=config.session.model or DEFAULT_WORKER_MODEL,
        model_base=config.session.base_model or DEFAULT_BASE_MODEL,
        model_speccer=config.session.speccer_model or DEFAULT_SPECCER_MODEL,
        known_models=list(config.session.known_models),
    )


class GroupJobBody(BaseModel):
    plan: str
    name: str | None = None
    granularity: Literal["independent", "balanced", "monolithic"] | None = None
    token_budget: int | None = None
    dry_run: bool = False
    auto_resume: bool | None = None


class RunJobBody(BaseModel):
    grouping: str | None = None
    run_id: str | None = None
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)


class ResumeJobBody(BaseModel):
    run_id: str
    options: ExecutionOptions = Field(default_factory=ExecutionOptions)


def build_argv(kind: JobKind, options: BaseModel, *, repo: Path) -> list[str]:
    """The one place a UI option becomes a CLI flag.

    Pure and total: it touches no disk and spawns nothing, so the whole
    option→flag table is a table test rather than a subprocess test.
    """
    argv = [sys.executable, "-u", "-m", "orchestrator.cli", kind]
    if kind == "group":
        assert isinstance(options, GroupJobBody)
        argv.append(options.plan)
        if options.name:
            argv += ["--name", options.name]
        if options.granularity:
            argv += ["--granularity", options.granularity]
        if options.token_budget is not None:
            argv += ["--token-budget", str(options.token_budget)]
        if options.dry_run:
            argv.append("--dry-run")
        if options.auto_resume is not None:
            argv.append("--auto-resume" if options.auto_resume else "--no-auto-resume")
    elif kind == "run":
        assert isinstance(options, RunJobBody)
        if options.grouping:
            argv += ["--grouping", options.grouping]
        if options.run_id:
            argv += ["--run-id", options.run_id]
        argv += options.options.to_argv()
    elif kind == "resume":
        assert isinstance(options, ResumeJobBody)
        argv.append(options.run_id)
        argv += options.options.to_argv()
    else:  # pragma: no cover — JobKind is closed
        raise LaunchError(f"unknown job kind {kind!r}")
    argv += ["--repo", str(repo)]
    return argv


# ---------------------------------------------------------------------- jobs


class JobInfo(BaseModel):
    """One launched job. ``running`` is derived from the pid at read time — see
    the module docstring on why it cannot be derived from a wait."""

    job_id: str
    kind: JobKind
    argv: list[str]
    pid: int | None = None
    started_at: datetime | None = None
    running: bool = False
    log_path: str = ""
    # Echoed back so the job list can say *what* was launched without the reader
    # having to parse argv — and so a UI form can be pre-filled from a past job.
    options: dict = Field(default_factory=dict)


def jobs_dir(repo: Path) -> Path:
    return repo / ".orchestrator" / JOBS_DIRNAME


def job_dir(repo: Path, job_id: str) -> Path:
    """A job's own directory. ``job_id`` is server-minted (a uuid4 hex), never
    client-supplied, so no path check is needed here — and the one caller that
    reads a client string looks it up in ``list_jobs`` rather than joining it."""
    return jobs_dir(repo) / job_id


def job_log_path(repo: Path, job_id: str) -> Path:
    return job_dir(repo, job_id) / "log"


def new_job_id() -> str:
    # Time-prefixed so `ls` sorts chronologically; the uuid tail is what makes
    # it unique when two land in the same second.
    return datetime.now(UTC).strftime("j%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


#: Jobs *this* process started, kept so their exits can be reaped. Without this
#: a finished job is a zombie — still in the process table, so ``os.kill(pid, 0)``
#: succeeds — and every completed job would read as "running" for as long as the
#: server stayed up. Jobs from a previous server process are absent from here
#: and need no entry: they were reparented to init, which reaps them, so the
#: signal probe below tells the truth about those on its own.
_OWN_CHILDREN: dict[int, subprocess.Popen] = {}


#: How far a live process's actual start time may drift from the job record's
#: `started_at` (itself sampled just before `Popen`) and still count as "the
#: same process". A recycled pid reused by an unrelated process — the classic
#: post-reboot case — differs by far more than scheduling jitter ever would.
_PID_RECYCLE_SLACK_SECONDS = 10.0


def _process_start_time(pid: int) -> float | None:
    """A running process's wall-clock start time (Linux `/proc`), or None when
    it cannot be determined — process gone, non-Linux, or `/proc` unavailable.
    Read from `/proc/<pid>/stat`'s starttime field (in clock ticks since boot)
    plus the kernel's own boot time from `/proc/stat`, not from the directory's
    ctime, which is not guaranteed to track process start."""
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fields = fh.read().split()
        starttime_ticks = int(fields[21])
        clk_tck = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat") as fh:
            btime = next(int(line.split()[1]) for line in fh if line.startswith("btime"))
    except (OSError, IndexError, ValueError, StopIteration):
        return None
    return btime + starttime_ticks / clk_tck


def pid_alive(pid: int | None, started_at: datetime | None = None) -> bool:
    """Whether the recorded pid still names *the same* process that started it.

    Signal 0 is the portable "does this exist and may I signal it" probe, used
    for pids we do not own. A ``PermissionError`` counts as alive: the process
    exists, it simply is not ours. Neither step distinguishes the process this
    job actually launched from an unrelated one that has since reused its pid —
    the classic post-reboot false positive — so when ``started_at`` is given,
    the live process's own start time (read from `/proc`) is cross-checked
    against it; a mismatch beyond scheduling jitter means the pid was recycled,
    and the job reads as not running even though *some* process answers to it.
    """
    if not pid:
        return False
    child = _OWN_CHILDREN.get(pid)
    if child is not None:
        if child.poll() is None:
            return True
        _OWN_CHILDREN.pop(pid, None)  # reaped by poll(); no zombie left behind
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if started_at is not None:
        actual_start = _process_start_time(pid)
        if actual_start is not None and abs(actual_start - started_at.timestamp()) > (
            _PID_RECYCLE_SLACK_SECONDS
        ):
            return False
    return True


def spawn_job(repo: Path, argv: list[str], kind: JobKind, options: dict | None = None) -> JobInfo:
    """Start a detached job and record everything needed to follow it.

    ``start_new_session=True`` puts the child in its own session and process
    group, so it survives the UI process exiting *and* a Ctrl-C in the terminal
    that started the UI — a run must not die because its launcher did.

    stdout and stderr are merged into one log file, in that order, because
    interleaving is what makes a log readable: the CLI prints its header to
    stdout and its warnings to stderr, and two separate files put those in two
    places with no way to tell which came first.
    """
    directory = job_dir(repo, new_job_id())
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "log"
    started_at = datetime.now(UTC)
    with log_path.open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 — argv is built by build_argv, never a shell
            argv,
            cwd=str(repo),
            stdout=log,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    _OWN_CHILDREN[process.pid] = process
    info = JobInfo(
        job_id=directory.name,
        kind=kind,
        argv=argv,
        pid=process.pid,
        started_at=started_at,
        running=True,
        log_path=str(log_path),
        options=options or {},
    )
    atomic_write_text(directory / "command.json", info.model_dump_json(indent=2) + "\n")
    return info


def read_job(repo: Path, job_id: str) -> JobInfo | None:
    """One job's record with ``running`` refreshed, or None if there is none."""
    path = job_dir(repo, job_id) / "command.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        info = JobInfo.model_validate(payload)
    except ValueError:
        return None
    return info.model_copy(update={"running": pid_alive(info.pid, started_at=info.started_at)})


def list_jobs(repo: Path, limit: int = 50) -> list[JobInfo]:
    """Newest first. An absent ``jobs/`` is the normal state of a repo nothing
    has been launched from, so it lists empty rather than erroring."""
    root = jobs_dir(repo)
    if not root.is_dir():
        return []
    infos = [
        info
        for entry in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True)
        if (info := read_job(repo, entry.name)) is not None
    ]
    return infos[:limit]


# ------------------------------------------------------------------ guardrails


@dataclass(frozen=True)
class RunLiveness:
    """Why a run does or does not look live, as the double-launch guard sees it.

    Rebuilt on the driver lock (plan U11), not ``state.json``'s ``live_pids``:
    those are *worker* subprocess pids, empty between workers on a perfectly
    healthy run, so treating an empty dict as "not live" misread that ordinary
    gap as a crash. The lock has no such gap — a driver holds it for its whole
    lifetime, not just while a worker subprocess happens to be up.
    """

    exists: bool
    driving: bool
    driver_pid: int | None

    @property
    def live(self) -> bool:
        return self.exists and self.driving


def run_liveness(repo: Path, run_id: str) -> RunLiveness:
    paths = RunPaths(repo, run_id)
    if not paths.state_path.is_file():
        return RunLiveness(exists=False, driving=False, driver_pid=None)
    driving = is_driving(paths)
    record = read_driver_record(paths) if driving else None
    return RunLiveness(
        exists=True, driving=driving, driver_pid=record.get("pid") if record else None
    )


def check_not_live(repo: Path, run_id: str | None) -> None:
    """Refuse to start a second scheduler over a run that is already running.

    A double-clicked button is the ordinary way to ask for this, and two
    schedulers sharing one set of worktrees would interleave commits and merges
    with nothing arbitrating. Raised as ``ConflictError`` → 409, which is the
    honest code: the request was well-formed, the state says no.
    """
    if not run_id:
        return
    liveness = run_liveness(repo, run_id)
    if liveness.live:
        raise ConflictError(
            f"run {run_id} is already running (driver pid {liveness.driver_pid}) — "
            "stop it before starting another, or resume it after it exits"
        )


# ----------------------------------------------------------------- discovery


class PlanDoc(BaseModel):
    """A plan document offered to the picker. ``title`` is the first ATX H1, or
    the filename stem when the document has none — a plan with no heading is
    still a plan, and dropping it from the list would hide it."""

    path: str
    title: str
    modified_at: datetime | None = None


def _plan_title(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(40):  # a title below line 40 is not a title
                line = handle.readline()
                if not line:
                    break
                if line.startswith("# "):
                    return line[2:].strip()
    except OSError:
        pass
    return path.stem


def list_plans(repo: Path, globs: tuple[str, ...] = PLAN_GLOBS) -> list[PlanDoc]:
    """Plan documents under the conventional locations, newest first.

    Paths are returned *relative to the repo*, which is also how they are
    accepted back: the CLI already re-anchors a relative plan path against
    ``--repo`` (``cli._anchor_plan_path``), so nothing here has to send an
    absolute path into a form field.
    """
    seen: dict[Path, PlanDoc] = {}
    for pattern in globs:
        for path in repo.glob(pattern):
            if not path.is_file() or path in seen:
                continue
            try:
                modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            except OSError:
                modified = None
            seen[path] = PlanDoc(
                path=str(path.relative_to(repo)),
                title=_plan_title(path),
                modified_at=modified,
            )
    plans = list(seen.values())
    plans.sort(key=lambda p: p.modified_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return plans


class GroupingSummary(BaseModel):
    """One named grouping, exactly as ``groupings`` prints it. The listing logic
    itself is ``manifest.describe_groupings`` — shared with the CLI command so
    the UI's list and the terminal's can never disagree."""

    name: str
    plan_path: str
    group_count: int


def list_groupings(repo: Path) -> list[GroupingSummary]:
    return [
        GroupingSummary(name=i.name, plan_path=i.plan_path, group_count=i.group_count)
        for i in describe_groupings(repo)
    ]


# ---------------------------------------------------------------------- routes
#
# Thin by design, like `escalations.py`: every route resolves the project,
# calls one function above, and maps its failure to a status code. Nothing
# below decides anything.

router = APIRouter(tags=["launch"], prefix="/api/projects/{project}")

#: The job log's SSE stream. A second router because it is the one route here
#: that does *not* hang off the project prefix — it lives under ``/events`` with
#: the run streams, so the SPA catch-all's prefix rule needs no new entry and
#: the client's two stream helpers stay symmetrical.
events_router = APIRouter(tags=["launch"])


@router.get("/plans", response_model=list[PlanDoc])
def get_plans(request: Request, project: str) -> list[PlanDoc]:
    """Plan documents the picker offers. A repo with none is an empty list —
    the form's free-text path field covers a plan that lives anywhere else."""
    return list_plans(resolve_repo(request, project))


@router.get("/groupings", response_model=list[GroupingSummary])
def get_groupings(request: Request, project: str) -> list[GroupingSummary]:
    return list_groupings(resolve_repo(request, project))


@router.get("/resolved-options", response_model=ResolvedOptions)
def get_resolved_options(request: Request, project: str) -> ResolvedOptions:
    return resolve_options(resolve_repo(request, project))


@router.get("/jobs", response_model=list[JobInfo])
def get_jobs(request: Request, project: str, limit: int = 50) -> list[JobInfo]:
    return list_jobs(resolve_repo(request, project), limit=limit)


@router.get("/jobs/{job_id}", response_model=JobInfo)
def get_job(request: Request, project: str, job_id: str) -> JobInfo:
    info = read_job(resolve_repo(request, project), job_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"no job {job_id!r}")
    return info


@router.post("/jobs/group", response_model=JobInfo, status_code=201)
def post_group_job(request: Request, project: str, body: GroupJobBody) -> JobInfo:
    repo = resolve_repo(request, project)
    return _spawn(repo, "group", body)


@router.post("/jobs/run", response_model=JobInfo, status_code=201)
def post_run_job(request: Request, project: str, body: RunJobBody) -> JobInfo:
    repo = resolve_repo(request, project)
    return _spawn(repo, "run", body, guard=body.run_id)


@router.post("/jobs/resume", response_model=JobInfo, status_code=201)
def post_resume_job(request: Request, project: str, body: ResumeJobBody) -> JobInfo:
    repo = resolve_repo(request, project)
    return _spawn(repo, "resume", body, guard=body.run_id)


def _spawn(repo: Path, kind: JobKind, body: BaseModel, *, guard: str | None = None) -> JobInfo:
    try:
        check_not_live(repo, guard)
        argv = build_argv(kind, body, repo=repo)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LaunchError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        return spawn_job(repo, argv, kind, options=body.model_dump(mode="json"))
    except OSError as exc:
        # The interpreter or the repo directory went away between resolution and
        # spawn. 500 rather than 400: nothing about the request was wrong.
        raise HTTPException(status_code=500, detail=f"could not start {kind}: {exc}") from exc


@events_router.get("/events/job")
async def stream_job(request: Request, project: str, job: str) -> EventSourceResponse:
    """Tail one job's log — how a grouping is watched while it runs, before any
    run directory exists for ``/events/log`` to point at."""
    repo = resolve_repo(request, project)
    info = read_job(repo, job)
    if info is None:
        raise HTTPException(status_code=404, detail=f"no job {job!r}")
    return EventSourceResponse(tail_file(Path(info.log_path), request))
