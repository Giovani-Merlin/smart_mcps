"""Worktree lifecycle under ``<repo>/.worktrees/`` (plan U5).

Worktree paths keep the repo directory name as a path substring: infinity-skills'
ingest allowlist substring-matches the encoded cwd and silently drops a worker's
sessions otherwise (docs/research/infinity-skills-analysis.md §6 rec 4). Nesting
under the repo root guarantees this by construction.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

from orchestrator.execution.manifest import atomic_write_text

#: Where a worktree's provisioning outcome is recorded (plan U32): a plain JSON
#: file under the group's run directory, never inside the worktree itself, so
#: it survives ``remove_worktree`` and is still readable long after the
#: worktree it describes is gone.
PROVISIONING_RECORD_NAME = "provisioning.json"


def provisioning_record_path(group_dir: Path) -> Path:
    """Where a worktree's provisioning record lives for ``group_dir`` — the same
    ``group_dir(gid)`` a run already keys reports and verdicts off of. The
    integration worktree uses ``group_dir("integration")``."""
    return group_dir / PROVISIONING_RECORD_NAME


def write_provisioning_record(
    group_dir: Path,
    *,
    worktree: Path,
    command: Sequence[str],
    state: str,
    detail: str = "",
    base_ref: str | None = None,
) -> None:
    """Persist what provisioning did to ``worktree`` (plan U32).

    Written beside the group's other run artifacts, never inside the worktree
    itself, so a group whose worktree was later torn down (``remove_worktree``,
    on a clean merge) still has a record of how it was provisioned — the
    drill-in reads this file, not the worktree.

    ``base_ref`` is the group's launch-time fork point (``merge-base`` of the
    integration tip and the group branch, captured at worktree creation). It is
    persisted here because a live ``merge-base`` recompute collapses to the
    branch head once the group merges into integration — every merged group's
    diff read "No changes" until the Observatory could read the ref the run
    actually branched from.
    """
    payload = {
        "worktree": str(worktree),
        "command": list(command),
        "state": state,
        "detail": detail,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    if base_ref is not None:
        payload["base_ref"] = base_ref
    atomic_write_text(provisioning_record_path(group_dir), json.dumps(payload, indent=2) + "\n")


def read_provisioning_record(group_dir: Path) -> dict | None:
    """The last-recorded provisioning outcome for ``group_dir``, or ``None`` if
    none was ever written (a run predating this feature, or a group that never
    reached worktree creation)."""
    try:
        payload = json.loads(provisioning_record_path(group_dir).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


class WorktreeError(Exception):
    """A git worktree operation failed or was refused."""


class WorktreeRefreshConflict(WorktreeError):
    """A resumed group's refresh onto the integration tip hit a real content
    conflict (plan U6). Distinct from ``WorktreeError`` so the scheduler can
    classify it ``INTERRUPTED`` (resumable) instead of terminal ``FAILED`` — the
    group's committed work is valid, only the merge needs a human or a later
    resume to resolve.

    ``paths`` carries the conflicted file names (plan U4): merge.py's
    ``merge_group`` re-raises this as the existing ``MergeConflict`` conflict
    ladder, which needs the same file list ``_groups_owning`` already keys off
    of — and by the time the exception propagates, the aborted merge has erased
    the conflict markers this would otherwise have to be re-derived from.
    """

    def __init__(self, message: str, paths: list[str] | None = None):
        super().__init__(message)
        self.paths = paths or []


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


def worktree_path(repo_root: Path, run_id: str, group_id: str, name: str) -> Path:
    """Run-scoped worktree path (plan U2, R19): ``<repo>/.worktrees/<run_id>/…`` —
    nested under the repo root, so the infinity-skills ingest allowlist's
    substring match on the encoded cwd still holds. The integration worktree
    (``group_id == "integration"``) gets no slug suffix, so it resolves to
    exactly ``<repo>/.worktrees/<run_id>/integration``.
    """
    if group_id == "integration":
        return repo_root / ".worktrees" / run_id / "integration"
    return repo_root / ".worktrees" / run_id / f"{group_id}-{slugify(name)}"


def _legacy_worktree_path(repo_root: Path, group_id: str, name: str) -> Path:
    """Pre-U2 (run-unscoped) worktree path — kept only so ``create_worktree`` can
    adopt one left behind by an older orchestrator version (plan R20)."""
    return repo_root / ".worktrees" / f"{group_id}-{slugify(name)}"


def existing_worktree_path(repo_root: Path, run_id: str, group_id: str, name: str) -> Path | None:
    """Where this run's worktree actually is on disk, or ``None``.

    ``worktree_path`` says where a worktree *would* go under the current
    (run-scoped) layout. A run started before U2 landed has its worktrees at the
    legacy run-unscoped paths, and only ``create_worktree`` adopts those — so any
    other caller that assumes the new layout is wrong about a pre-U2 run. Readers
    that merely need to *find* an existing worktree use this instead, which
    prefers the run-scoped path and falls back to the legacy one.

    The integration worktree's legacy name is not ``integration``: it was created
    as ``group_id=f"run-{run_id}", name="integration"``, so it resolves to
    ``.worktrees/run-<run_id>-integration``.
    """
    current = worktree_path(repo_root, run_id, group_id, name)
    if current.is_dir():
        return current
    legacy_gid = f"run-{run_id}" if group_id == "integration" else group_id
    legacy = _legacy_worktree_path(repo_root, legacy_gid, name)
    return legacy if legacy.is_dir() else None


def group_branch(run_id: str, group_id: str) -> str:
    """Branch a group's worktree lives on. Deliberately not nested under the
    integration branch name (``orchestrator/run-<run_id>``): git refuses a ref that
    is both a name and a directory."""
    return f"orchestrator/{run_id}-{group_id}"


def integration_branch(run_id: str) -> str:
    return f"orchestrator/run-{run_id}"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # A missing cwd otherwise surfaces as a bare `FileNotFoundError: [Errno 2]`
    # naming the directory but not the git command or why it was expected —
    # which is how a wrong-layout worktree path reads as an unrelated crash.
    if not cwd.is_dir():
        raise WorktreeError(f"git {' '.join(args)}: working directory does not exist: {cwd}")
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


def _worktree_of_branch(repo_root: Path, branch: str) -> Path | None:
    """The registered worktree that has ``branch`` checked out, else ``None`` —
    ``_registered_branch``'s inverse, over the same porcelain listing."""
    out = _git_ok(repo_root, "worktree", "list", "--porcelain")
    current: str | None = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line.startswith("branch ") and current is not None:
            if line.removeprefix("branch ").removeprefix("refs/heads/") == branch:
                return Path(current)
    return None


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
    repo_root: Path, *, run_id: str, group_id: str, name: str, branch: str, start_point: str
) -> Path:
    """Create (or reuse) the group's worktree. Idempotent: an existing worktree
    already on ``branch`` is returned as-is; an existing branch without a worktree
    is checked out where it left off (the resume case).

    A registered legacy (run-unscoped) worktree still on ``branch`` is adopted in
    place via ``git worktree move`` rather than duplicated (plan R20) — a plain
    ``mv`` would desync git's own worktree registry.

    Both re-entry paths commit any stranded uncommitted/untracked work (plan R3)
    before refreshing onto ``start_point`` (plan U1, amended R2): a resumed
    group's branch is otherwise never brought up to date with work merged while
    it was down, and worktrees are not removed on interrupt, so the
    existing-worktree path is the *more* common resume case, not an edge one.
    """
    path = worktree_path(repo_root, run_id, group_id, name)
    if not path.exists():
        legacy = _legacy_worktree_path(repo_root, group_id, name)
        if legacy.exists() and legacy != path and _registered_branch(repo_root, legacy) == branch:
            path.parent.mkdir(parents=True, exist_ok=True)
            result = _git(repo_root, "worktree", "move", str(legacy), str(path))
            if result.returncode != 0:
                if "cross-device" not in result.stderr.lower():
                    raise WorktreeError(
                        f"git worktree move {legacy} {path} failed: {result.stderr.strip()[:500]}"
                    )
                # `git worktree move` shells out to a plain rename, which fails
                # EXDEV when .worktrees/<run_id>/ lands on a different mount
                # (e.g. a tmpfs /tmp) than the legacy directory it is adopting.
                # shutil.move falls back to copy+delete across devices; `git
                # worktree repair` then re-links both sides of the gitdir
                # pointer that a plain move leaves stale.
                shutil.move(str(legacy), str(path))
                _git_ok(repo_root, "worktree", "repair", str(path))
    if path.exists():
        existing = _registered_branch(repo_root, path)
        if existing == branch:
            _ensure_worktree_config_extension(path)
            commit_all(path, f"recover({run_id}): {group_id} work stranded by an interrupted run")
            _refresh_onto_tip(path, group_id=group_id, branch=branch, tip=start_point)
            return path
        raise WorktreeError(
            f"{path} exists but is not a worktree on {branch}"
            f" (found: {existing or 'unregistered directory'})"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    if _branch_exists(repo_root, branch):
        result = _git(repo_root, "worktree", "add", str(path), branch)
        if result.returncode != 0:
            # Name the directory that actually holds the branch: a residual
            # name mismatch (e.g. a rewritten group resolved through a stale
            # name) otherwise surfaces as a bare git error with no path to act
            # on (the r20260830-163212 resume failure mode).
            holder = _worktree_of_branch(repo_root, branch)
            hint = (
                f" — {branch} is already checked out at {holder}; the run may be "
                "resolving this group through a stale name"
                if holder is not None
                else ""
            )
            raise WorktreeError(
                f"git worktree add {path} {branch} failed: {result.stderr.strip()[:500]}{hint}"
            )
        _ensure_worktree_config_extension(path)
        commit_all(path, f"recover({run_id}): {group_id} work stranded by an interrupted run")
        _refresh_onto_tip(path, group_id=group_id, branch=branch, tip=start_point)
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


def _refresh_onto_tip(worktree: Path, *, group_id: str, branch: str, tip: str) -> None:
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
            f"{', '.join(conflicted)}",
            paths=conflicted,
        )
    # git refused before starting the merge — e.g. uncommitted local changes
    # would be overwritten (a reason the stranded-work commit in create_worktree
    # did not clear, e.g. a file git refuses to overwrite for another reason).
    # Nothing to abort (no MERGE_HEAD was created), and the uncommitted changes
    # are exactly what must survive untouched.
    raise WorktreeError(
        f"refreshing group {group_id}'s worktree (branch {branch}) onto {tip} failed: "
        f"{result.stderr.strip()[:500]} — resolve the conflict by hand in the worktree, "
        "then `retry` the group"
    )


def provision_env(
    worktree: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    log: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    extra_args: Sequence[str] | None = None,
    on_state: Callable[[str, list[str]], None] | None = None,
) -> bool:
    """Provision the worktree's own venv via ``uv sync`` (plan U6, R16, U32).

    Runs only when the worktree root carries ``pyproject.toml`` or ``uv.lock``
    (a uv-managed checkout); anything else is skipped silently. A failing sync
    is non-fatal — the worker can re-sync per its guidance, so a fixable env
    hiccup must never kill the group: log the lifecycle event, warn on stderr,
    move on. ``runner`` is the injectable subprocess seam for offline tests.

    ``env`` is overlaid on the current environment and exists for cache
    *locality*, not permission — this runs in the orchestrator process, entirely
    unconfined. It used to run with no ``env=`` at all, so it warmed
    ``~/.cache/uv`` while the worker it was provisioning for used the
    orchestrator's cache root: two caches, and the worker's one cold on a venv
    the other had already built. It also produced the observed ``EXDEV`` — `uv`
    finishes by renaming out of its cache, which fails across filesystems.

    ``extra_args`` is appended to ``uv sync`` (``["--all-extras"]`` in
    production): a group's venv should mirror the environment its work is
    verified against, or its reviewer cannot tell a missing extra from a
    regression.

    ``on_state`` (plan U32), if given, is called exactly once with
    ``("skipped" | "provisioned" | "failed", argv)`` — the exact ``uv sync``
    invocation this call would run (empty on ``"skipped"``) — so a caller can
    persist the outcome (``write_provisioning_record``) without duplicating the
    uv-managed-checkout test above.
    """
    if not (worktree / "pyproject.toml").is_file() and not (worktree / "uv.lock").is_file():
        if on_state is not None:
            on_state("skipped", [])
        return False
    run = runner or subprocess.run
    argv = ["uv", "sync", *(extra_args or [])]
    try:
        result = run(
            argv,
            cwd=worktree,
            capture_output=True,
            text=True,
            env={**os.environ, **env} if env else None,
        )
    except OSError as exc:  # uv missing entirely — same non-fatal contract
        _report_sync_failure(f"uv sync failed in {worktree}: {exc}", log)
        if on_state is not None:
            on_state("failed", argv)
        return False
    if result.returncode != 0:
        _report_sync_failure(f"uv sync failed in {worktree}: {result.stderr.strip()[:500]}", log)
        if on_state is not None:
            on_state("failed", argv)
        return False
    if log is not None:
        # The line an operator needs by itself (plan U32): which worktree, the
        # exact command, and when — no need to cross-reference a separate
        # provisioning record just to answer "did this get set up?".
        when = datetime.now(UTC).strftime("%H:%M")
        log(f"worktree {worktree} was provisioned with `{' '.join(argv)}` at {when}")
    if on_state is not None:
        on_state("provisioned", argv)
    return True


def provision_node_env(
    worktree: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    log: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    on_state: Callable[[str, list[str]], None] | None = None,
) -> bool:
    """Provision ``ui/node_modules`` via ``npm ci`` — ``provision_env``'s
    contract, for the JavaScript half of the checkout.

    A worktree is a fresh checkout and ``node_modules/`` is gitignored, so
    without this the merge gate's ``vitest``/``tsc`` steps never resolve and
    silently skip (``detect_check_steps`` requires ``ui/node_modules``). Runs
    only when ``ui/package.json`` exists; anything else is skipped silently.

    Non-fatal by the same reasoning as ``provision_env``, and here it matters
    more: a failed install leaves the UI steps skipped, which is a *weaker*
    gate, never a failed run. A machine without npm must not be able to halt a
    run under ``on-failure halt``.
    """
    ui = worktree / "ui"
    if not (ui / "package.json").is_file():
        if on_state is not None:
            on_state("skipped", [])
        return False
    run = runner or subprocess.run
    argv = ["npm", "ci", "--no-audit", "--fund=false"]
    try:
        result = run(
            argv,
            cwd=ui,
            capture_output=True,
            text=True,
            env={**os.environ, **env} if env else None,
        )
    except OSError as exc:  # npm missing entirely — same non-fatal contract
        _report_sync_failure(f"npm ci failed in {ui}: {exc}", log)
        if on_state is not None:
            on_state("failed", argv)
        return False
    if result.returncode != 0:
        _report_sync_failure(f"npm ci failed in {ui}: {(result.stderr or '').strip()[:500]}", log)
        if on_state is not None:
            on_state("failed", argv)
        return False
    if log is not None:
        when = datetime.now(UTC).strftime("%H:%M")
        log(f"worktree {ui} was provisioned with `{' '.join(argv)}` at {when}")
    if on_state is not None:
        on_state("provisioned", argv)
    return True


def _report_sync_failure(message: str, log: Callable[[str], None] | None) -> None:
    print(f"warning: {message}", file=sys.stderr)
    if log is not None:
        log(message)


def ensure_excluded(worktree: Path, relative_path: str) -> None:
    """Add ``relative_path`` to this worktree's local exclude file (plan U6),
    never the target repo's tracked ``.gitignore``.

    ``git rev-parse --git-path info/exclude`` is what "this worktree's own
    exclude file" means to git itself: ``info/exclude`` lives under the
    *common* gitdir every linked worktree of a repo shares (there is no
    per-worktree exclude file at all — ``.git`` inside a linked worktree is a
    file pointing at the common dir, not a directory of its own), so this
    write is idempotent and safe to repeat across every group's worktree and
    every round.
    """
    exclude_path = Path(_git_ok(worktree, "rev-parse", "--git-path", "info/exclude").strip())
    if not exclude_path.is_absolute():
        exclude_path = worktree / exclude_path
    existing = exclude_path.read_text() if exclude_path.is_file() else ""
    if relative_path in existing.splitlines():
        return
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    with exclude_path.open("a") as fh:
        if existing and not existing.endswith("\n"):
            fh.write("\n")
        fh.write(f"{relative_path}\n")


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
