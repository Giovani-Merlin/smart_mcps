"""Run-artifact persistence under ``<repo>/.orchestrator/runs/<run_id>/`` (plan U5).

All run artifacts live in the target repo — never under ``~/.claude/projects/``,
whose glob would ingest stray ``.jsonl`` files as malformed transcripts
(docs/research/infinity-skills-analysis.md §6 rec 9). The manifest is the
analyzer's only cross-session join (origin R17); reports and verdicts persist here
as the artifacts round triggers point to (pointers, not payloads).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from orchestrator.model import GroupingResult, GroupManifestEntry, RunManifest, SessionEntry


def atomic_write_text(path: Path, text: str) -> None:
    """Write-then-rename so a crash mid-write never leaves a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class GroupingNameError(Exception):
    """A ``--name``/``--grouping`` tag is unsafe to use as a directory component."""


class GroupingSelectionError(Exception):
    """``run``/``resume`` could not unambiguously select a grouping directory."""


def validate_grouping_name(name: str) -> None:
    """Reject a path separator or ``..`` before anything is written to disk (plan U10)."""
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        raise GroupingNameError(
            f"invalid grouping name {name!r}: must not contain a path separator or '..'"
        )


def groupings_root(repo_root: Path) -> Path:
    return repo_root / ".orchestrator" / "groupings"


def grouping_dir(repo_root: Path, name: str) -> Path:
    """The one place a named grouping's directory path is spelled out (plan U10)."""
    validate_grouping_name(name)
    return groupings_root(repo_root) / name


def list_grouping_names(repo_root: Path) -> list[str]:
    root = groupings_root(repo_root)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


@dataclass(frozen=True)
class GroupingInfo:
    name: str
    plan_path: str
    group_count: int


def describe_groupings(repo_root: Path) -> list[GroupingInfo]:
    """Every named grouping present, with its plan path and group count (plan U10)."""
    infos = []
    for name in list_grouping_names(repo_root):
        groups_path = grouping_dir(repo_root, name) / "groups.json"
        if not groups_path.is_file():
            continue
        result = GroupingResult.model_validate_json(groups_path.read_text())
        infos.append(
            GroupingInfo(name=name, plan_path=result.plan_path, group_count=len(result.groups))
        )
    return infos


def snapshot_grouping(source_dir: Path, dest_dir: Path) -> None:
    """Copy a grouping directory's files into a run directory (plan U10).

    Generic copy rather than an enumerated list, so a later artifact is
    snapshotted automatically. The run keeps its own frozen copy so a later
    ``group --name <same>`` against a different plan cannot rewrite a finished
    run's history.

    Subdirectories are copied too: the grouper's LLM call records live in a
    nested ``llm/``, and a files-only copy dropped them from every snapshot
    while appearing to succeed.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    for path in source_dir.iterdir():
        if path.is_file():
            shutil.copy2(path, dest_dir / path.name)
        elif path.is_dir():
            shutil.copytree(path, dest_dir / path.name, dirs_exist_ok=True)


def archive_review_scratch(
    scratch_dir: Path,
    dest_dir: Path,
    *,
    cap_bytes: int,
    log: Callable[[str], None] | None = None,
) -> None:
    """Move a group's reviewer scratch litter out of its worktree (plan U6).

    Files are visited in sorted (deterministic) order; once the running total
    would exceed ``cap_bytes`` every remaining file is left out of the archive
    and named — with its size — in ``skipped.txt`` rather than silently
    dropped. The whole scratch directory is removed from the worktree either
    way: an oversized directory does not get to keep blocking every future
    Preflight cleanliness check, and a skipped file's record survives in the
    archive even though its bytes do not.

    A no-op when ``scratch_dir`` does not exist — the common case for a group
    whose reviewer never wrote to it, and for every ``self_verify`` group,
    which has no reviewer session to write to it at all.
    """
    if not scratch_dir.is_dir():
        return
    dest_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    skipped: list[tuple[str, int]] = []
    for path in sorted(scratch_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(scratch_dir)
        size = path.stat().st_size
        if total + size > cap_bytes:
            skipped.append((str(rel), size))
            continue
        total += size
        target = dest_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    if skipped:
        lines = "\n".join(f"{name} ({size} bytes)" for name, size in skipped)
        (dest_dir / "skipped.txt").write_text(lines + "\n")
        if log is not None:
            log(
                f"review scratch archive: {len(skipped)} file(s) exceeded the "
                f"{cap_bytes} byte cap and were skipped (see skipped.txt)"
            )
    shutil.rmtree(scratch_dir)


class RunPaths:
    """The one place the run-directory layout is spelled out."""

    def __init__(self, repo_root: Path, run_id: str):
        self.repo_root = repo_root
        self.run_id = run_id

    @property
    def run_dir(self) -> Path:
        return self.repo_root / ".orchestrator" / "runs" / self.run_id

    @property
    def manifest_path(self) -> Path:
        return self.run_dir / "manifest.json"

    @property
    def state_path(self) -> Path:
        return self.run_dir / "state.json"

    @property
    def logs_dir(self) -> Path:
        return self.run_dir / "logs"

    @property
    def event_log_path(self) -> Path:
        """The live human-readable event log the main session tails (plan Phase D)."""
        return self.logs_dir / "run.log"

    @property
    def groups_path(self) -> Path:
        """This run's DAG snapshot. ``.orchestrator/groups.json`` is shared and every
        planning cycle overwrites it, so a run keeps its own copy (ADR 0002)."""
        return self.run_dir / "groups.json"

    @property
    def escalations_dir(self) -> Path:
        """Correlation-ID request/response files for the human channel (plan Phase D)."""
        return self.run_dir / "escalations"

    @property
    def usage_limit_path(self) -> Path:
        """The rate-limit gate's current pause, for the snapshot and the UI banner.

        A file of its own rather than a field on ``state.json``: the gate fires
        from a worker thread while the scheduler's event loop owns ``state.json``,
        and a separate write-then-rename file avoids that cross-thread hazard
        entirely instead of needing a lock nothing else in the run needs.
        """
        return self.run_dir / "usage-limit.json"

    @property
    def auth_pause_path(self) -> Path:
        """The auth ladder's current pause, mirroring ``usage_limit_path`` (plan
        U4): the gate fires from a worker thread, so this is its own
        write-then-rename file rather than a field the scheduler's event loop
        would have to lock around."""
        return self.run_dir / "auth-pause.json"

    @property
    def surprises_path(self) -> Path:
        """Persisted SurpriseBoard state (plan U7): an in-memory-only board dies
        with the process, silently dropping a surprise marked for a group that
        has not yet run when the run restarts."""
        return self.run_dir / "surprises.json"

    @property
    def preflight_baseline_path(self) -> Path:
        """What was already red on the launch branch before the run started
        (plan U2) — captured once, on a fresh run, and read back by the merge
        gate to tell a new failure from a pre-existing one."""
        return self.run_dir / "preflight-baseline.json"

    def group_dir(self, group_id: str) -> Path:
        return self.run_dir / "groups" / group_id

    def review_scratch_archive_dir(self, group_id: str) -> Path:
        """Where reviewer scratch litter lands once archived out of the group's
        worktree (plan U6)."""
        return self.group_dir(group_id) / "review-scratch"

    @property
    def driver_lock_path(self) -> Path:
        """The advisory lock a driver process holds for its lifetime (plan U11).

        The kernel drops an ``flock`` on any process death, SIGKILL included, so
        this file's *lock state* — not its contents — is the liveness authority;
        the record below is evidence for a human, not what `run_liveness` reads.
        """
        return self.run_dir / "driver.lock"

    @property
    def driver_record_path(self) -> Path:
        """Human-readable evidence of who last held ``driver_lock_path``: pid,
        when it started, and a freshening ``updated_at`` (plan U11)."""
        return self.run_dir / "driver.json"


def log_event(paths: RunPaths, text: str) -> None:
    """Append one timestamped line to the run's event log (plan Phase D).

    O_APPEND makes single small writes atomic across the process's group threads,
    so concurrent group transitions interleave line-by-line rather than tearing.

    F22: timestamps are the operator's local zone (not UTC, as before) so they
    read alongside a quoted usage-limit reset instant without a silent zone
    mismatch (``UsageLimitGate._until_phrase`` converts to this same zone). The
    zone is stated once, as the log's own first line, rather than repeated on
    every line.
    """
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    path = paths.event_log_path
    now = datetime.now().astimezone()
    lines = []
    if not path.exists():
        lines.append(f"{now.isoformat(timespec='seconds')}  {_zone_header(now)}\n")
    lines.append(f"{now.isoformat(timespec='seconds')}  {text}\n")
    with path.open("a") as fh:
        fh.writelines(lines)


def _zone_header(now: datetime) -> str:
    """ "zone: PDT (UTC-07:00)" — the label the run log's timestamps carry."""
    offset = now.strftime("%z")  # e.g. "-0700"
    formatted = f"{offset[:3]}:{offset[3:]}" if offset else "+00:00"
    zone_name = now.tzname() or "local"
    return f"log timestamps below are local time, zone {zone_name} (UTC{formatted})"


class ManifestStore:
    """Load/save the run manifest and persist per-group round artifacts."""

    def __init__(self, paths: RunPaths):
        self.paths = paths

    def exists(self) -> bool:
        return self.paths.manifest_path.is_file()

    def load(self) -> RunManifest:
        return RunManifest.model_validate_json(self.paths.manifest_path.read_text())

    def save(self, manifest: RunManifest) -> None:
        atomic_write_text(self.paths.manifest_path, manifest.model_dump_json(indent=2) + "\n")

    def save_group_artifact(self, group_id: str, filename: str, payload: BaseModel) -> Path:
        """Persist a report/verdict; returns the path round triggers ferry as a pointer."""
        path = self.paths.group_dir(group_id) / filename
        atomic_write_text(path, payload.model_dump_json(indent=2) + "\n")
        return path


def record_session(
    manifest: RunManifest, *, group_id: str, group_name: str, summary: str, entry: SessionEntry
) -> None:
    """Append a session to its group's manifest entry, creating the entry on first use."""
    group = manifest.groups.get(group_id)
    if group is None:
        group = GroupManifestEntry(group_id=group_id, group_name=group_name, summary=summary)
        manifest.groups[group_id] = group
    group.sessions.append(entry)


def artifact_name(kind: str, generation: int, round_no: int) -> str:
    """Canonical artifact filename: e.g. ``report-g1-r2.json``, ``verdict-g2-r1.json``."""
    return f"{kind}-g{generation}-r{round_no}.json"


_REPORT_ROUND_RE = re.compile(r"^report-g(\d+)-r(\d+)\.json$")


def completed_round_count(paths: RunPaths, group_id: str, generation: int) -> int:
    """How many rounds of ``generation`` already have a saved report on disk.

    Re-entry (warm-resumed or fallback-forked) continues the same generation
    number rather than starting a fresh one, so round numbering must resume
    from here instead of colliding with — and overwriting — pre-crash
    artifacts still on disk.
    """
    group_dir = paths.group_dir(group_id)
    rounds = [
        int(match.group(2))
        for path in group_dir.glob(f"report-g{generation}-r*.json")
        if (match := _REPORT_ROUND_RE.match(path.name))
    ]
    return max(rounds, default=0)
