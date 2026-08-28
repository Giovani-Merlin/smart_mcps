"""Per-group reports and verdicts as the run wrote them (plan U8).

``groups/<gid>/`` accumulates one ``report-g<N>-r<M>.json`` per coder round and
one ``verdict-g<N>-r<M>.json`` per reviewer round. They are served parsed but
unvalidated: a verdict written by an older schema should still be readable in
the drill-in, so a file that no longer matches ``CoderReport`` /
``ReviewerVerdict`` is returned as-is rather than rejected.

Routes are registered on this module's ``router``, which ``app.py`` already
includes — adding an endpoint here needs no edit there.

``load_json``/``load_text`` are the package's one artifact reader. Every
Observatory router reads the same kind of thing — a file some other process
wrote, which may be absent, half-written, or from a schema this code predates —
and each one that rolled its own ``try: json.loads`` picked a slightly different
answer for those three cases. They live here rather than in a new module because
this is already the module about reading artifacts off disk.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from orchestrator.execution.denial import classify_denial
from orchestrator.execution.manifest import RunPaths
from orchestrator.execution.worktrees import group_branch, integration_branch
from orchestrator.model import SessionRole
from orchestrator.observatory.runs import RUN_PREFIX, load_manifest, resolve_run

router = APIRouter(tags=["artifacts"], prefix=RUN_PREFIX)

#: A diff above this many UTF-8 bytes is truncated rather than sent whole — the
#: pane exists to be read, and a multi-megabyte diff is not (plan U29).
DIFF_TRUNCATE_BYTES = 200_000


# ------------------------------------------------------------------ git diffs


class DiffResult(BaseModel):
    """A best-effort diff between two refs. Never raises: an unreadable branch,
    a torn-down worktree or missing timing data all degrade to
    ``available=False`` with a human-readable ``reason`` rather than a 500, on
    the same contract every other artifact read here follows."""

    available: bool = True
    reason: str | None = None
    from_ref: str | None = None
    to_ref: str | None = None
    diff: str = ""
    truncated: bool = False
    total_bytes: int | None = None


def _run_git(repo_root: Path, *args: str) -> tuple[str, str, int]:
    result = subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True, errors="replace"
    )
    return result.stdout, result.stderr, result.returncode


def _branch_exists(repo_root: Path, branch: str) -> bool:
    _, _, code = _run_git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
    return code == 0


def _truncate_diff(text: str) -> tuple[str, bool, int]:
    encoded = text.encode("utf-8")
    total = len(encoded)
    if total <= DIFF_TRUNCATE_BYTES:
        return text, False, total
    return encoded[:DIFF_TRUNCATE_BYTES].decode("utf-8", errors="ignore"), True, total


def _diff_between(repo_root: Path, from_ref: str, to_ref: str) -> DiffResult:
    stdout, stderr, code = _run_git(repo_root, "diff", from_ref, to_ref)
    if code != 0:
        return DiffResult(available=False, reason=f"git diff failed: {stderr.strip()[:300]}")
    text, truncated, total = _truncate_diff(stdout)
    return DiffResult(
        from_ref=from_ref, to_ref=to_ref, diff=text, truncated=truncated, total_bytes=total
    )


def _fork_point(paths: RunPaths, branch: str) -> tuple[str | None, DiffResult | None]:
    """``merge-base(integration, branch)`` — the same fork point
    ``base_ref_for`` captured at group launch (``cli.py:_workspace_seams``),
    recomputed live since the Observatory keeps no in-memory cache across
    requests. Returns ``(ref, None)`` on success or ``(None, failure)``."""
    integration = integration_branch(paths.run_id)
    if not _branch_exists(paths.repo_root, integration):
        return None, DiffResult(
            available=False, reason=f"integration branch {integration!r} not found"
        )
    stdout, stderr, code = _run_git(paths.repo_root, "merge-base", integration, branch)
    if code != 0:
        return None, DiffResult(
            available=False, reason=f"could not find fork point: {stderr.strip()[:300]}"
        )
    return stdout.strip(), None


def group_diff(paths: RunPaths, group_id: str) -> DiffResult:
    """The group's whole diff against the integration tip it branched from
    (plan U29, R4) — a single ``git diff <fork point>..<group branch>``."""
    branch = group_branch(paths.run_id, group_id)
    if not _branch_exists(paths.repo_root, branch):
        return DiffResult(
            available=False,
            reason=(
                f"branch {branch!r} no longer exists — its worktree was torn down after merging"
            ),
        )
    fork_point, failure = _fork_point(paths, branch)
    if failure is not None:
        return failure
    return _diff_between(paths.repo_root, fork_point, branch)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def _commit_log(repo_root: Path, base_ref: str, branch: str) -> list[tuple[str, datetime]]:
    """Ordered ``(sha, commit_date)`` pairs on ``branch`` since ``base_ref``."""
    stdout, _stderr, code = _run_git(
        repo_root, "log", "--reverse", "--format=%H%x09%cI", f"{base_ref}..{branch}"
    )
    if code != 0:
        return []
    commits: list[tuple[str, datetime]] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        sha, _, iso = line.partition("\t")
        dt = _parse_iso(iso)
        if dt is not None:
            commits.append((sha, dt))
    return commits


def generation_diff(paths: RunPaths, group_id: str, generation: int) -> DiffResult:
    """A generation's final diff — the commits its coder session(s) made,
    bucketed by session ``started_at`` against the run manifest (plan U29, R3).

    There is no git ref recorded per generation, so this is a best-effort
    reconstruction from timing evidence already on disk: a generation's window
    runs from its own coder session's earliest ``started_at`` up to (but not
    including) the next generation's earliest ``started_at``, and every commit
    whose commit date falls in that window is treated as that generation's
    work. Missing timestamps or an empty window degrade to ``available=False``
    rather than a guess.
    """
    branch = group_branch(paths.run_id, group_id)
    if not _branch_exists(paths.repo_root, branch):
        return DiffResult(
            available=False,
            reason=(
                f"branch {branch!r} no longer exists — its worktree was torn down after merging"
            ),
        )
    manifest = load_manifest(paths)
    entry = manifest.groups.get(group_id) if manifest else None
    if entry is None:
        return DiffResult(available=False, reason=f"no manifest entry for group {group_id!r}")

    starts: dict[int, datetime] = {}
    for session in entry.sessions:
        if session.role != SessionRole.CODER:
            continue
        dt = _parse_iso(session.started_at)
        if dt is None:
            continue
        if session.generation not in starts or dt < starts[session.generation]:
            starts[session.generation] = dt

    if generation not in starts:
        return DiffResult(
            available=False,
            reason=(
                f"no recorded start time for generation {generation} of group "
                f"{group_id!r} — its coder session predates timing being recorded"
            ),
        )

    fork_point, failure = _fork_point(paths, branch)
    if failure is not None:
        return failure

    commits = _commit_log(paths.repo_root, fork_point, branch)
    if not commits:
        return DiffResult(
            available=False, reason=f"branch {branch!r} has no commits ahead of its fork point"
        )

    # Floored to whole seconds: a session's ``started_at`` carries microseconds
    # but git commit timestamps do not, so an unfloored boundary can exclude a
    # commit made in the same wall-clock second the session was recorded as
    # starting.
    window_start = starts[generation].replace(microsecond=0)
    later_starts = sorted(
        dt.replace(microsecond=0) for gen, dt in starts.items() if gen > generation
    )
    window_end = later_starts[0] if later_starts else None

    bucket = [
        (sha, dt)
        for sha, dt in commits
        if dt >= window_start and (window_end is None or dt < window_end)
    ]
    if not bucket:
        return DiffResult(
            available=False,
            reason=f"generation {generation} of group {group_id!r} made no commits",
        )

    first_index = commits.index(bucket[0])
    from_ref = commits[first_index - 1][0] if first_index > 0 else fork_point
    to_ref = bucket[-1][0]
    return _diff_between(paths.repo_root, from_ref, to_ref)


@router.get("/groups/{group_id}/diff", response_model=DiffResult)
def get_group_diff(request: Request, project: str, run_id: str, group_id: str) -> DiffResult:
    return group_diff(resolve_run(request, project, run_id), group_id)


@router.get(
    "/groups/{group_id}/generations/{generation}/diff",
    response_model=DiffResult,
)
def get_generation_diff(
    request: Request, project: str, run_id: str, group_id: str, generation: int
) -> DiffResult:
    return generation_diff(resolve_run(request, project, run_id), group_id, generation)


# ------------------------------------------------------------- shared reading


def load_json(path: Path) -> tuple[Any, str | None]:
    """``(content, error)`` for a JSON artifact — never raises.

    A file that is absent, unreadable or half-written all come back as
    ``(None, "<why>")``. Every Observatory surface degrades on artifacts rather
    than failing the request, so the error is data to be shown next to the path
    it came from, not an exception to propagate.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace")), None
    except (OSError, json.JSONDecodeError) as exc:
        return None, str(exc)


def load_text(path: Path) -> tuple[str | None, str | None]:
    """``(content, error)`` for a text artifact, on ``load_json``'s contract.

    ``errors="replace"`` matches the transcript reader: a prompt or a raw model
    response is whatever bytes the runner wrote, and mojibake in one line is
    worth reading around, not worth losing the file over.
    """
    try:
        return path.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, str(exc)


class Artifact(BaseModel):
    """``kind`` comes from the ``artifact_name`` convention so the pane can group
    reports and verdicts without re-parsing filenames in TypeScript."""

    name: str
    kind: str  # report | verdict | other
    content: Any = None
    error: str | None = None
    #: For a `permission_denied` report only: which of the three unrelated causes
    #: it was (plan P2). Derived on read by the *same* `classify_denial` the review
    #: loop uses, rather than stored — so artifacts are never rewritten, every
    #: report already on disk gains attribution retroactively, and there is exactly
    #: one classifier to keep correct.
    denial_kind: str | None = None
    #: True for `verdict-g<N>-r<M>-extra.json` — the mandatory second
    #: verification pass a `paired_plus` group earns above `d_hard` (plan U28).
    #: `finish.py`'s `_VERDICT_RE` deliberately does not match this filename, so
    #: it is derived here on read rather than in the PR-body verdict lookup.
    is_extra: bool = False


@router.get("/groups/{group_id}/artifacts", response_model=list[Artifact])
def get_artifacts(request: Request, project: str, run_id: str, group_id: str) -> list[Artifact]:
    """A group that has not finished a round yet simply has no directory — that
    is an empty list, not a 404."""
    paths = resolve_run(request, project, run_id)
    directory = paths.group_dir(group_id)
    if not directory.is_dir():
        return []
    return [_read(path) for path in sorted(directory.glob("*.json"))]


def _read(path: Path) -> Artifact:
    kind = path.name.split("-", 1)[0]
    artifact = Artifact(
        name=path.name,
        kind=kind if kind in ("report", "verdict") else "other",
        is_extra=path.stem.endswith("-extra"),
    )
    content, error = load_json(path)
    # Half-written or unreadable: name it rather than failing the whole list.
    return artifact.model_copy(
        update={"content": content, "error": error, "denial_kind": _denial_kind(content)}
    )


def _denial_kind(content: Any) -> str | None:
    """Attribute a denial report on read, or return None for anything else.

    Unvalidated on purpose, like everything else here: a report from an older
    schema has no `denial_error`/`denial_source` and must still classify (as
    UNKNOWN, honestly) rather than 500 the Artifacts tab. `deny_rules` is not
    available to this process — it belongs to the run that spawned the worker — so
    a `POLICY_FORBIDDEN` can only be attributed live, in the run log; here that
    same report reads as whatever its text supports.
    """
    if not isinstance(content, dict) or content.get("status") != "permission_denied":
        return None
    return str(
        classify_denial(
            denied_command=str(content.get("denied_command") or ""),
            denial_error=str(content.get("denial_error") or ""),
            denial_source=str(content.get("denial_source") or ""),
        )
    )
