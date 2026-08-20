"""Push the integration branch, open a draft PR, and tear down exactly what is
provably merged (plan U8/U9).

Teardown gates on *completeness*, not worktree state: the CLAUDE.md rule
"never clean a crashed group's uncommitted worktree progress" is about
completeness — while the plan is unfinished, a crashed group's worktree may be
the only copy of work a `retry` will build on. Once a group's branch is an
ancestor of the integration tip, that work is banked in the integration
branch's own history, and the worktree is safe to remove. ``git branch -d``
(never ``-D``) is the second, independent guard behind that ancestry check.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from orchestrator.config import ExecutionConfig
from orchestrator.execution.manifest import (
    ManifestStore,
    RunPaths,
    archive_review_scratch,
)
from orchestrator.execution.prompting import REVIEW_SCRATCH_DIRNAME
from orchestrator.execution.scheduler import GroupState, RunState
from orchestrator.execution.worktrees import (
    _branch_exists,
    _git,
    _git_ok,
    ensure_excluded,
    group_branch,
    integration_branch,
    is_dirty,
    remove_worktree,
    worktree_path,
)
from orchestrator.model import GroupingResult, RunManifest


class FinishError(Exception):
    """`finish` could not complete a required step (e.g. the push failed)."""


@dataclass
class FinishResult:
    integration_branch: str
    integration_sha: str
    pr_url: str | None
    pr_skip_reason: str | None
    # Groups not provably merged (not completed/resolved, or not an ancestor
    # of the integration tip): their worktree and branch are left untouched.
    unmerged: list[str] = field(default_factory=list)
    # Groups whose worktree was torn down but whose branch survived because
    # git's own `-d` merge check refused it (plan R33) — reported, not forced.
    kept_branches: list[str] = field(default_factory=list)


def run_is_finishable(repo_root: Path, run_id: str) -> tuple[bool, list[str]]:
    """Whether every group ended COMPLETED/RESOLVED *and* its branch is an
    ancestor of the integration tip (plan U8 Decisions) — the gate the run
    itself checks before invoking `finish` on its own. Returns ``(ok, bad)``
    where ``bad`` names every group failing either check."""
    paths = RunPaths(repo_root, run_id)
    state = RunState.model_validate_json(paths.state_path.read_text())
    branch = integration_branch(run_id)
    tip = _git_ok(repo_root, "rev-parse", branch).strip()
    bad = [
        gid
        for gid in sorted(state.groups)
        if not _group_is_merged(repo_root, run_id, tip, gid, state.groups[gid])
    ]
    return (not bad, bad)


def _group_is_merged(repo_root: Path, run_id: str, tip: str, gid: str, entry) -> bool:
    if entry.state not in (GroupState.COMPLETED, GroupState.RESOLVED):
        return False
    branch = group_branch(run_id, gid)
    if not _branch_exists(repo_root, branch):
        return False
    return _is_ancestor(repo_root, branch, tip)


def finish_run(
    repo_root: Path,
    run_id: str,
    *,
    log: Callable[[str], None] | None = None,
    announce: Callable[[str], None] | None = None,
) -> FinishResult:
    """Push the integration branch, open a draft PR against the run's launch
    branch, then tear down every group provably merged into it (plan U8/U9).

    ``log`` is the run's own event log (best-effort, silent by default in
    tests); ``announce`` is user-facing stdout (defaults to ``print``).
    """
    log = log or (lambda _text: None)
    announce = announce or print
    paths = RunPaths(repo_root, run_id)
    if not paths.state_path.is_file():
        raise FinishError(f"no run state at {paths.state_path}")
    state = RunState.model_validate_json(paths.state_path.read_text())
    store = ManifestStore(paths)
    manifest = store.load() if store.exists() else None

    branch = integration_branch(run_id)
    tip = _git_ok(repo_root, "rev-parse", branch).strip()
    _push_integration_branch(repo_root, run_id)
    log(f"finish {run_id}: pushed {branch} to origin at {tip}")

    launch_branch = manifest.launch_branch if manifest is not None else None
    pr_url: str | None = None
    pr_skip_reason: str | None = None
    if launch_branch is None:
        pr_skip_reason = "run was launched from a detached HEAD"
    else:
        body = _render_pr_body(repo_root, run_id, tip, state, manifest, paths)
        ok, result = _open_draft_pr(repo_root, run_id, launch_branch, body)
        if ok:
            pr_url = result
        else:
            pr_skip_reason = result

    if pr_url is not None:
        log(f"finish {run_id}: opened draft PR {pr_url}")
        announce(f"opened draft PR: {pr_url}")
    else:
        message = (
            f"integration branch {branch} is ready at {tip}; could not open a PR ({pr_skip_reason})"
        )
        log(f"finish {run_id}: {message}")
        announce(message)

    unmerged: list[str] = []
    kept_branches: list[str] = []
    integration_wt = worktree_path(repo_root, run_id, "integration", "integration")
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        if not _group_is_merged(repo_root, run_id, tip, gid, entry):
            unmerged.append(gid)
            continue
        _teardown_group(repo_root, run_id, gid, paths)
        gbranch = group_branch(run_id, gid)
        if not _delete_branch_if_merged(integration_wt, gbranch):
            kept_branches.append(gid)
            log(f"finish {run_id}: kept branch {gbranch} — git considers it unmerged")

    if unmerged:
        announce(f"kept (not provably merged into {branch}): {', '.join(unmerged)}")
    if kept_branches:
        announce(
            f"worktree removed but branch kept (git considers unmerged): {', '.join(kept_branches)}"
        )

    return FinishResult(
        integration_branch=branch,
        integration_sha=tip,
        pr_url=pr_url,
        pr_skip_reason=pr_skip_reason,
        unmerged=unmerged,
        kept_branches=kept_branches,
    )


# --------------------------------------------------------------------- push


def _push_integration_branch(repo_root: Path, run_id: str) -> None:
    branch = integration_branch(run_id)
    result = subprocess.run(
        ["git", "push", "origin", f"{branch}:{branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise FinishError(f"push of {branch} to origin failed: {result.stderr.strip()[:500]}")


# ----------------------------------------------------------------------- PR


_VERDICT_RE = re.compile(r"^verdict-g(\d+)-r(\d+)\.json$")


def _latest_verdict_status(paths: RunPaths, gid: str) -> str | None:
    """The most recent reviewer verdict's status for ``gid``, or None when no
    reviewer round ever ran (e.g. a self_verify group)."""
    group_dir = paths.group_dir(gid)
    best_path: Path | None = None
    best_key = (-1, -1)
    if not group_dir.is_dir():
        return None
    for path in group_dir.glob("verdict-g*-r*.json"):
        match = _VERDICT_RE.match(path.name)
        if match is None:
            continue
        key = (int(match.group(1)), int(match.group(2)))
        if key > best_key:
            best_key = key
            best_path = path
    if best_path is None:
        return None
    try:
        payload = json.loads(best_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    status = payload.get("status")
    return status if isinstance(status, str) else None


def _render_pr_body(
    repo_root: Path,
    run_id: str,
    tip: str,
    state: RunState,
    manifest: RunManifest | None,
    paths: RunPaths,
) -> str:
    lines = [f"Orchestrator run `{run_id}`, integration tip `{tip[:12]}`.", ""]
    unmerged: list[str] = []
    for gid in sorted(state.groups):
        entry = state.groups[gid]
        group_entry = manifest.groups.get(gid) if manifest is not None else None
        summary = group_entry.summary if group_entry is not None else "(no summary recorded)"
        sessions = len(group_entry.sessions) if group_entry is not None else 0
        verdict = _latest_verdict_status(paths, gid)
        verdict_text = f", reviewer verdict: {verdict}" if verdict else ""
        lines.append(
            f"- **{gid}** ({entry.state.value}{verdict_text}, {sessions} session(s)): {summary}"
        )
        if not _group_is_merged(repo_root, run_id, tip, gid, entry):
            unmerged.append(gid)
    if unmerged:
        lines.append("")
        lines.append(f"**Unmerged groups:** {', '.join(unmerged)}")
    return "\n".join(lines) + "\n"


def _open_draft_pr(repo_root: Path, run_id: str, launch_branch: str, body: str) -> tuple[bool, str]:
    """Open a draft PR for the integration branch against ``launch_branch``.

    Returns ``(True, pr_url)`` on success or ``(False, reason)`` — a missing,
    unauthenticated, or non-GitHub `gh` never raises (plan R30): the caller
    treats a False result as "continue into cleanup, exit zero".
    """
    if shutil.which("gh") is None:
        return False, "gh is not installed"
    auth = subprocess.run(["gh", "auth", "status"], cwd=repo_root, capture_output=True, text=True)
    if auth.returncode != 0:
        return False, "gh is not authenticated"
    remote = subprocess.run(
        ["git", "remote", "get-url", "origin"], cwd=repo_root, capture_output=True, text=True
    )
    if remote.returncode != 0 or "github.com" not in remote.stdout:
        return False, "origin is not a GitHub remote"
    branch = integration_branch(run_id)
    result = subprocess.run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            launch_branch,
            "--head",
            branch,
            "--title",
            f"orchestrator run {run_id}",
            "--body-file",
            "-",
        ],
        cwd=repo_root,
        input=body,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, (result.stderr.strip()[:300] or "gh pr create failed")
    return True, result.stdout.strip()


# ------------------------------------------------------------------ ancestry


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# -------------------------------------------------------------- teardown


def _group_name(paths: RunPaths, group_id: str) -> str:
    result = GroupingResult.model_validate_json(paths.groups_path.read_text())
    for group in result.groups:
        if group.id == group_id:
            return group.name
    raise FinishError(f"group {group_id} not found in {paths.groups_path}")


def _teardown_group(repo_root: Path, run_id: str, gid: str, paths: RunPaths) -> None:
    """Archive remaining scratch, write a leftover patch for any uncommitted
    change, then force-remove the worktree (plan U9). A no-op when the
    worktree is already gone."""
    worktree = worktree_path(repo_root, run_id, gid, _group_name(paths, gid))
    if not worktree.is_dir():
        return
    scratch_dir = worktree / REVIEW_SCRATCH_DIRNAME
    if scratch_dir.exists():
        ensure_excluded(worktree, REVIEW_SCRATCH_DIRNAME)
        archive_review_scratch(
            scratch_dir,
            paths.review_scratch_archive_dir(gid),
            cap_bytes=ExecutionConfig().review_scratch_cap_bytes,
            log=None,
        )
    if is_dirty(worktree):
        _write_leftover_patch(worktree, paths.group_dir(gid) / "leftover.patch")
    remove_worktree(repo_root, worktree, force=True)


def _write_leftover_patch(worktree: Path, dest: Path) -> None:
    """A patch of every uncommitted change (tracked and untracked), written
    before the worktree that produced it is force-removed (plan R32). Staging
    everything is safe here — the worktree is about to be deleted regardless —
    and it is what makes untracked files show up in the diff at all."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(worktree, "add", "-A")
    diff = _git(worktree, "diff", "--cached").stdout
    dest.write_text(diff)


def _delete_branch_if_merged(integration_wt: Path, branch: str) -> bool:
    """``git branch -d``, run from the integration worktree so git's own merge
    check is against the integration branch's own HEAD (plan U9 Decisions) —
    the second, independent guard behind the ancestry check already performed.
    Never ``-D``: a branch git itself considers unmerged survives untouched."""
    result = _git(integration_wt, "branch", "-d", branch)
    return result.returncode == 0
