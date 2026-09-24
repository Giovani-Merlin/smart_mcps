"""The run-level Artifact Manifest (plan U6): a single index of what every
group produced, fed to downstream prompts and additive on the Run Bundle.

A ``code`` entry is written by the orchestrator at merge time from data it
already has (the merge commit, the files the diff actually touched, the
final ``CoderReport.summary``) — the coder's own prompt and report are
untouched by this module. A ``run`` recipe entry (a later plan) registers
through the same store. Only non-``code`` entries are ever injected into a
downstream prompt (see ``render_artifact_inputs_block``); a ``code`` entry
exists for the Run Bundle and for a human reading ``artifacts.json``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from orchestrator.execution.manifest import RunPaths, atomic_write_text

#: R16's cap on an entry's summary — validated here, never truncated later.
ARTIFACT_SUMMARY_MAX_CHARS = 2000

#: Total size of the block folded into a downstream coder's prompt (Goal):
#: measurements are truncated first, ending in a "+N more" line, before an
#: entry itself would ever be dropped.
ARTIFACT_BLOCK_MAX_CHARS = 8000

Measurement = float | int | str | bool


class ArtifactEntry(BaseModel):
    """One group's registered output. ``schema`` (aliased — the bare name
    shadows ``BaseModel``) names the contract model this entry's shape
    follows (``"CoderReport"`` for every ``code`` entry today)."""

    model_config = ConfigDict(populate_by_name=True)

    artifact_id: str
    group_id: str
    tasks: list[str] = Field(default_factory=list)
    recipe: str
    paths: list[str] = Field(default_factory=list)
    #: Per-file content hash, for files that exist. Empty for ``code`` —
    #: git already content-addresses every file at ``commit``.
    sha256: dict[str, str] = Field(default_factory=dict)
    commit: str | None = None
    schema_name: str = Field(alias="schema")
    summary: str = Field(max_length=ARTIFACT_SUMMARY_MAX_CHARS)
    status: Literal["complete", "partial"]
    measurements: dict[str, Measurement] = Field(default_factory=dict)
    recorded_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


class ArtifactManifest(BaseModel):
    """The content of ``artifacts.json``: every entry, keyed by ``artifact_id``."""

    entries: dict[str, ArtifactEntry] = Field(default_factory=dict)


class ArtifactManifestStore:
    """Load/save the run's Artifact Manifest, guarded by a process-local lock
    so concurrent groups merging at once cannot tear a load-modify-save."""

    def __init__(self, paths: RunPaths):
        self.paths = paths
        self._lock = threading.Lock()

    def load(self) -> ArtifactManifest:
        path = self.paths.artifact_manifest_path
        if not path.is_file():
            return ArtifactManifest()
        return ArtifactManifest.model_validate_json(path.read_text())

    def save(self, manifest: ArtifactManifest) -> None:
        atomic_write_text(
            self.paths.artifact_manifest_path,
            manifest.model_dump_json(indent=2, by_alias=True) + "\n",
        )

    def register(self, entry: ArtifactEntry) -> None:
        """Idempotent per ``artifact_id``: a re-merge (or a Resolve settling
        a group the same run already registered once) replaces the entry
        rather than duplicating it."""
        with self._lock:
            manifest = self.load()
            manifest.entries[entry.artifact_id] = entry
            self.save(manifest)

    def get(self, artifact_id: str) -> ArtifactEntry | None:
        return self.load().entries.get(artifact_id)


def _format_measurement(value: Measurement) -> str:
    return str(value)


def render_artifact_inputs_block(entries: Sequence[ArtifactEntry]) -> str:
    """The block folded into a downstream coder's prompt (Goal): each
    upstream entry's id, summary and measurements, in the given order, never
    its ``paths`` or file bytes. Capped at ``ARTIFACT_BLOCK_MAX_CHARS``
    total — measurements are truncated first, per entry, ending in a
    "``+N more — see artifacts.json``" line. Empty input renders "" so a
    prompt with no non-``code`` upstream is unchanged byte-for-byte."""
    if not entries:
        return ""
    close_line = "+0 more — see artifacts.json\n"
    reserve = len(close_line)
    parts: list[str] = ["## Upstream artifacts\n\n"]
    total = len(parts[0])
    for entry in entries:
        header = f"### {entry.artifact_id} (group {entry.group_id})\n{entry.summary}\n"
        measurement_items = sorted(entry.measurements.items())
        kept_lines: list[str] = []
        remaining = list(measurement_items)
        if remaining:
            header += "Measurements:\n"
        budget_before_measurements = total + len(header)
        while remaining:
            key, value = remaining[0]
            line = f"- {key}: {_format_measurement(value)}\n"
            projected = budget_before_measurements + sum(len(x) for x in kept_lines) + len(line)
            if projected + reserve > ARTIFACT_BLOCK_MAX_CHARS:
                break
            kept_lines.append(line)
            remaining.pop(0)
        entry_text = header + "".join(kept_lines)
        if remaining:
            entry_text += f"+{len(remaining)} more — see artifacts.json\n"
        entry_text += "\n"
        if total + len(entry_text) > ARTIFACT_BLOCK_MAX_CHARS and parts[1:]:
            # This whole entry cannot fit alongside what is already kept —
            # stop rather than silently drop it mid-entry.
            break
        parts.append(entry_text)
        total += len(entry_text)
    return "".join(parts).rstrip() + "\n"
