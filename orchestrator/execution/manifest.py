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
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from orchestrator.model import GroupManifestEntry, RunManifest, SessionEntry


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
    def escalations_dir(self) -> Path:
        """Correlation-ID request/response files for the human channel (plan Phase D)."""
        return self.run_dir / "escalations"

    def group_dir(self, group_id: str) -> Path:
        return self.run_dir / "groups" / group_id


def log_event(paths: RunPaths, text: str) -> None:
    """Append one timestamped line to the run's event log (plan Phase D).

    O_APPEND makes single small writes atomic across the process's group threads,
    so concurrent group transitions interleave line-by-line rather than tearing.
    """
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(UTC).isoformat(timespec='seconds')}  {text}\n"
    with paths.event_log_path.open("a") as fh:
        fh.write(line)


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
