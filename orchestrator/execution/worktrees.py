"""Worktree lifecycle under ``<repo>/.worktrees/`` (plan U5).

Worktree paths keep the repo directory name as a path substring: infinity-skills'
ingest allowlist substring-matches the encoded cwd and silently drops a worker's
sessions otherwise (docs/research/infinity-skills-analysis.md §6 rec 4). Nesting
under the repo root guarantees this by construction.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path


class WorktreeError(Exception):
    """A git worktree operation failed or was refused."""


class WorktreeRefreshConflict(WorktreeError):
    """A resumed group's refresh onto the integration tip hit a real content
    conflict (plan U6). Distinct from ``WorktreeError`` so the scheduler can
    classify it ``INTERRUPTED`` (resumable) instead of terminal ``FAILED`` — the
    group's committed work is valid, only the merge needs a human or a later
    resume to resolve."""


# Repo-global git mutators a worker must never run (plan U5). ``refs/stash`` is
# shared across every worktree of a repo (git-worktree(1) REFS) — a worker's
# ``git stash`` collided with an unrelated operator stash on 2026-06-11 and
# resurrected a long-deleted file, killing group g1 on run r20260808. The others
# either rewrite shared refs/reflogs or delete worktree metadata other groups
# still hold.
DENIED_GIT_SUBCOMMANDS: tuple[tuple[str, ...], ...] = (
    ("stash",),
    ("reset", "--hard"),
    ("clean",),
    ("gc",),
    ("worktree", "prune"),
)


def is_denied_git_invocation(args: Sequence[str]) -> bool:
    """True if ``args`` (a git argv without the leading ``git``) invokes one of
    the repo-global mutators workers must not run."""
    for denied in DENIED_GIT_SUBCOMMANDS:
        if tuple(args[: len(denied)]) == denied:
            return True
    return False


def denied_git_tool_patterns() -> list[str]:
    """``--disallowedTools`` patterns blocking each denied git subcommand via the
    Bash tool (plan U5/U2): the boundary a PreToolUse-style allowlist can see."""
    return [f"Bash(git {' '.join(denied)}:*)" for denied in DENIED_GIT_SUBCOMMANDS]


def slugify(name: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "group"


def worktree_path(repo_root: Path, group_id: str, name: str) -> Path:
    return repo_root / ".worktrees" / f"{group_id}-{slugify(name)}"


def group_branch(run_id: str, group_id: str) -> str:
    """Branch a group's worktree lives on. Deliberately not nested under the
    integration branch name (``orchestrator/run-<run_id>``): git refuses a ref that
    is both a name and a directory."""
    return f"orchestrator/{run_id}-{group_id}"


def integration_branch(run_id: str) -> str:
    return f"orchestrator/run-{run_id}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _git_ok(cwd: Path, *args: str) -> str:
    result = _git(cwd, *args)
    if result.returncode != 0:
        raise WorktreeError(f"git {' '.join(args)} failed: {result.stderr.strip()[:500]}")
    return result.stdout


def _branch_exists(repo_root: Path, branch: str) -> bool:
    return (
        _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    )


def _registered_branch(repo_root: Path, path: Path) -> str | None:
    """The branch checked out at ``path`` if it is a registered worktree, else None."""
    out = _git_ok(repo_root, "worktree", "list", "--porcelain")
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line.startswith("branch ") and current is not None:
            if Path(current).resolve() == path.resolve():
                return line.removeprefix("branch ").removeprefix("refs/heads/")
    return None


def create_worktree(
    repo_root: Path, *, group_id: str, name: str, branch: str, start_point: str
) -> Path:
    """Create (or reuse) the group's worktree. Idempotent: an existing worktree
    already on ``branch`` is returned as-is; an existing branch without a worktree
    is checked out where it left off (the resume case).

    Both re-entry paths refresh onto ``start_point`` (plan U1, amended R2) before
    returning: a resumed group's branch is otherwise never brought up to date with
    work merged while it was down, and worktrees are not removed on interrupt, so
    the existing-worktree path is the *more* common resume case, not an edge one.
    """
    path = worktree_path(repo_root, group_id, name)
    if path.exists():
        existing = _registered_branch(repo_root, path)
        if existing == branch:
            _ensure_worktree_config_extension(path)
            _refresh_onto_tip(path, group_id=group_id, tip=start_point)
            return path
        raise WorktreeError(
            f"{path} exists but is not a worktree on {branch}"
            f" (found: {existing or 'unregistered directory'})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_root, branch):
        _git_ok(repo_root, "worktree", "add", str(path), branch)
        _ensure_worktree_config_extension(path)
        _refresh_onto_tip(path, group_id=group_id, tip=start_point)
    else:
        _git_ok(repo_root, "worktree", "add", "-b", branch, str(path), start_point)
        _ensure_worktree_config_extension(path)
    return path


def _ensure_worktree_config_extension(path: Path) -> None:
    """Enable per-worktree config (plan U5) so ``git config --worktree`` inside
    ``path`` writes to that worktree's own ``config.worktree`` file rather than
    the repo-common config every worktree shares — a worker's ``git config
    user.email`` must not be able to mutate the operator's repo."""
    _git_ok(path, "config", "extensions.worktreeConfig", "true")


def _refresh_onto_tip(worktree: Path, *, group_id: str, tip: str) -> None:
    """Bring a re-entered group worktree's branch up to date with ``tip``.

    Plain ``git merge``, never ``--ff-only`` and never a rebase (plan U1, amended
    R2): a branch that has committed anything has diverged by definition, so
    ``--ff-only`` would reject exactly the resumed groups this refresh exists to
    rescue, and a rebase rewrites SHAs a warm coder session already has in
    context. Fast-forwards silently when possible (no merge commit); makes a
    merge commit when diverged; raises on a real content conflict or on git
    refusing to touch uncommitted local changes — either way the working tree's
    uncommitted changes are never discarded.
    """
    result = _git(worktree, "merge", tip, "-m", f"refresh({group_id}): onto {tip}")
    if result.returncode == 0:
        return
    conflicted = _git_ok(worktree, "diff", "--name-only", "--diff-filter=U").splitlines()
    if conflicted:
        _git(worktree, "merge", "--abort")
        raise WorktreeRefreshConflict(
            f"refreshing group {group_id}'s worktree onto {tip} conflicted on: "
            f"{', '.join(conflicted)}"
        )
    # git refused before starting the merge — e.g. uncommitted local changes
    # would be overwritten. Nothing to abort (no MERGE_HEAD was created), and the
    # uncommitted changes are exactly what must survive untouched.
    raise WorktreeError(
        f"refreshing group {group_id}'s worktree onto {tip} failed: {result.stderr.strip()[:500]}"
    )


def provision_env(
    worktree: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    log: Callable[[str], None] | None = None,
) -> bool:
    """Provision the worktree's own venv via ``uv sync`` (plan U6, R16).

    Runs only when the worktree root carries ``pyproject.toml`` or ``uv.lock``
    (a uv-managed checkout); anything else is skipped silently. A failing sync
    is non-fatal — the worker can re-sync per its guidance, so a fixable env
    hiccup must never kill the group: log the lifecycle event, warn on stderr,
    move on. ``runner`` is the injectable subprocess seam for offline tests.
    """
    if not (worktree / "pyproject.toml").is_file() and not (worktree / "uv.lock").is_file():
        return False
    run = runner or subprocess.run
    try:
        result = run(["uv", "sync"], cwd=worktree, capture_output=True, text=True)
    except OSError as exc:  # uv missing entirely — same non-fatal contract
        _report_sync_failure(f"uv sync failed in {worktree}: {exc}", log)
        return False
    if result.returncode != 0:
        _report_sync_failure(f"uv sync failed in {worktree}: {result.stderr.strip()[:500]}", log)
        return False
    return True


def _report_sync_failure(message: str, log: Callable[[str], None] | None) -> None:
    print(f"warning: {message}", file=sys.stderr)
    if log is not None:
        log(message)


def is_dirty(worktree: Path) -> bool:
    """Uncommitted changes, including untracked files — anything removal would lose."""
    return bool(_git_ok(worktree, "status", "--porcelain").strip())


def commit_all(worktree: Path, message: str) -> bool:
    """Commit every uncommitted change (tracked and untracked) in ``worktree``.
    False — a no-op — when the worktree is missing or already clean: the
    resolve routine's "nothing lost" case (plan U2)."""
    if not worktree.is_dir() or not is_dirty(worktree):
        return False
    _git_ok(worktree, "add", "-A")
    _git_ok(worktree, "commit", "-m", message)
    return True


def diff_stat(worktree: Path, base_ref: str) -> str:
    """Best-effort diff summary for generation handoffs (plan U7); never raises."""
    committed = _git(worktree, "diff", "--stat", base_ref)
    if committed.returncode != 0:
        return "(diff unavailable)"
    return committed.stdout.strip() or "(no changes yet)"


def remove_worktree(repo_root: Path, path: Path, *, force: bool = False) -> None:
    """Remove a worktree. Idempotent on a missing path; refuses a dirty worktree
    unless ``force`` is explicit (plan U5 test scenario)."""
    if not path.exists():
        return
    if is_dirty(path) and not force:
        raise WorktreeError(f"refusing to remove dirty worktree {path}; pass force=True")
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    _git_ok(repo_root, *args, str(path))
