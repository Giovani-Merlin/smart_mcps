"""Worktree lifecycle under ``<repo>/.worktrees/`` (plan U5).

Worktree paths keep the repo directory name as a path substring: infinity-skills'
ingest allowlist substring-matches the encoded cwd and silently drops a worker's
sessions otherwise (docs/research/infinity-skills-analysis.md §6 rec 4). Nesting
under the repo root guarantees this by construction.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class WorktreeError(Exception):
    """A git worktree operation failed or was refused."""


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
    is checked out where it left off (the resume case)."""
    path = worktree_path(repo_root, group_id, name)
    if path.exists():
        existing = _registered_branch(repo_root, path)
        if existing == branch:
            return path
        raise WorktreeError(
            f"{path} exists but is not a worktree on {branch}"
            f" (found: {existing or 'unregistered directory'})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_root, branch):
        _git_ok(repo_root, "worktree", "add", str(path), branch)
    else:
        _git_ok(repo_root, "worktree", "add", "-b", branch, str(path), start_point)
    return path


def is_dirty(worktree: Path) -> bool:
    """Uncommitted changes, including untracked files — anything removal would lose."""
    return bool(_git_ok(worktree, "status", "--porcelain").strip())


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
