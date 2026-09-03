"""Per-group diffs for ``report.html`` (report v2.1 U3): one
``git diff <merge>^1 <merge>`` per merged group, computed at render time
from ``GroupFacts.merge_sha`` and never stored in ``facts.json`` — the same
git-at-render-time arrangement ``diagrams.architecture_delta`` uses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from orchestrator.report.facts import _numstat, _run_git

#: A group's unified diff is cut here (on a line boundary) so one runaway
#: group — a vendored bundle, a regenerated fixture — cannot bloat the report.
DEFAULT_MAX_BYTES = 400_000


@dataclass(frozen=True)
class GroupDiff:
    text: str
    truncated: bool
    files: int
    added: int
    deleted: int


def group_diff(repo_root: Path, merge_sha: str, *, max_bytes: int = DEFAULT_MAX_BYTES) -> GroupDiff:
    """The unified diff a merge commit brought onto the integration branch —
    ``git diff <sha>^1 <sha>`` with no colour — plus numstat totals. When the
    text exceeds ``max_bytes`` it is truncated at the last complete line
    within the budget and ``truncated`` is set. A sha git cannot diff yields
    an empty, untruncated ``GroupDiff`` rather than an error: the report
    still renders, with the "no diff" note for that card."""
    parent = f"{merge_sha}^1"
    text = _run_git(repo_root, "diff", "--no-color", parent, merge_sha) or ""
    rows = _numstat(repo_root, parent, merge_sha)
    truncated = False
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        cut = encoded[:max_bytes].rfind(b"\n")
        # Only complete lines survive; a first line longer than the budget
        # leaves an empty text rather than a half-escaped fragment.
        text = encoded[: cut + 1].decode("utf-8", errors="ignore") if cut >= 0 else ""
        truncated = True
    return GroupDiff(
        text=text,
        truncated=truncated,
        files=len(rows),
        added=sum(added for _path, added, _deleted in rows),
        deleted=sum(deleted for _path, _added, deleted in rows),
    )


# ------------------------------------------------------------ side by side

_HUNK_RE = re.compile(r"^@@ -(?P<old>\d+)(?:,\d+)? \+(?P<new>\d+)(?:,\d+)? @@")
_FILE_HEADER_PREFIXES = (
    "index ",
    "old mode ",
    "new mode ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
    "copy from ",
    "copy to ",
)


@dataclass(frozen=True)
class DiffRow:
    """One row of a side-by-side table. ``kind`` is ``ctx`` (both sides the
    same line), ``del`` (old only), ``add`` (new only), ``change`` (a removed
    line paired with the added line at the same offset in the hunk), or
    ``hunk`` (a ``@@`` header spanning the table; ``old`` carries its text)."""

    kind: str
    old_no: int | None = None
    old: str | None = None
    new_no: int | None = None
    new: str | None = None


@dataclass
class FileDiff:
    path: str
    binary: bool = False
    rows: list[DiffRow] = field(default_factory=list)


def split_diff(text: str) -> list[FileDiff]:
    """A unified diff as per-file side-by-side rows, VS Code style: within a
    hunk, a run of removed lines is paired index-wise with the run of added
    lines that follows it; the longer run's tail becomes one-sided rows.
    Tolerates a text cut mid-file (``group_diff`` truncation)."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    old_no = new_no = 0
    dels: list[str] = []
    adds: list[str] = []

    def flush() -> None:
        nonlocal old_no, new_no
        assert current is not None
        for index in range(max(len(dels), len(adds))):
            old = dels[index] if index < len(dels) else None
            new = adds[index] if index < len(adds) else None
            if old is not None and new is not None:
                current.rows.append(DiffRow("change", old_no, old, new_no, new))
                old_no += 1
                new_no += 1
            elif old is not None:
                current.rows.append(DiffRow("del", old_no, old, None, None))
                old_no += 1
            else:
                current.rows.append(DiffRow("add", None, None, new_no, new))
                new_no += 1
        dels.clear()
        adds.clear()

    for line in text.splitlines():
        if line.startswith("diff --git "):
            if current is not None:
                flush()
            # "diff --git a/<path> b/<path>": the b/ side is the surviving name.
            _, _, rest = line.partition(" b/")
            current = FileDiff(path=rest or line[len("diff --git ") :])
            files.append(current)
            continue
        if current is None:
            continue
        if line.startswith("+++ ") or line.startswith("--- "):
            if line.startswith("+++ b/"):
                current.path = line[len("+++ b/") :]
            continue
        if line.startswith(_FILE_HEADER_PREFIXES):
            continue
        if line.startswith("Binary files "):
            current.binary = True
            continue
        hunk = _HUNK_RE.match(line)
        if hunk:
            flush()
            old_no, new_no = int(hunk.group("old")), int(hunk.group("new"))
            current.rows.append(DiffRow("hunk", old=line))
            continue
        if line.startswith("\\"):  # "\ No newline at end of file"
            continue
        if line.startswith("-"):
            dels.append(line[1:])
            continue
        if line.startswith("+"):
            adds.append(line[1:])
            continue
        flush()
        body = line[1:] if line.startswith(" ") else line
        current.rows.append(DiffRow("ctx", old_no, body, new_no, body))
        old_no += 1
        new_no += 1
    if current is not None:
        flush()
    return files
