"""Kernel-enforced worker confinement via Landlock (plan U2).

``permission_mode="acceptEdits"`` accepts ``Edit``/``Write`` at any filesystem
path, and a ``PreToolUse`` hook only ever sees tool-routed writes — it is blind
to ``bash -c 'cat > ~/x'``, a Python ``open(..., "w")``, or git writing refs.
A worker used exactly that gap to write a false claim into the operator's
global memory (``~/.claude/projects/<operator-slug>/memory/``).

Landlock is the boundary that actually holds: unprivileged, inherited by
every child process a worker's shell spawns, and enforced by the kernel
rather than an allowlist a tool can be talked around. It is allowlist-only
with no subtraction — a rule on a parent grants the whole subtree — so the
allowlist here is built from two things only: the worker's own worktree
(read-write) and an *enumerated* probe of ``~/.claude`` (read-only, and
deliberately excluding ``projects/`` wholesale — only the worker's own
project slug is added, with read-write, which is what excludes every other
slug's ``memory/`` by construction rather than by exclusion).

No image, no volume, no ``--sandbox`` flag (the CLI has none — plan's
resolved context). ``landlock_restrict_self`` is called from a
``preexec_fn``, i.e. in the forked child after ``fork()`` and before
``exec()`` — the exact point at which a restriction applies to the process
about to become ``claude`` and everything it in turn spawns, without
touching the orchestrator process itself.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long

# x86_64 syscall numbers (Landlock landed in 5.13; stable since).
_SYS_LANDLOCK_CREATE_RULESET = 444
_SYS_LANDLOCK_ADD_RULE = 445
_SYS_LANDLOCK_RESTRICT_SELF = 446
_PR_SET_NO_NEW_PRIVS = 38

_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1

# ABI 1 filesystem access rights. Deliberately capped here — ABI 2+ adds bits
# (REFER, TRUNCATE, ...) that are additive; their absence on an ABI-1-only
# kernel just leaves those specific operations ungoverned, never a crash.
_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_REG = 1 << 8
_ACCESS_FS_MAKE_SOCK = 1 << 9
_ACCESS_FS_MAKE_FIFO = 1 << 10
_ACCESS_FS_MAKE_BLOCK = 1 << 11
_ACCESS_FS_MAKE_SYM = 1 << 12

# Only the write-class rights are ever put in a ruleset's handled_access_fs
# (plan U2's title: "deny writes outside the worktree" — not reads). Landlock
# only governs the access classes a ruleset declares; leaving READ_FILE/
# READ_DIR/EXECUTE out of handled_access_fs means they are never restricted at
# all, so system binaries (bash, python, git, ...) keep resolving and running
# exactly as without Landlock. Governing reads too would need a blanket rule
# covering the whole filesystem just to keep exec() working, in exchange for a
# read boundary nothing here asks for.
_WRITE_ACCESS_FS = (
    _ACCESS_FS_WRITE_FILE
    | _ACCESS_FS_REMOVE_DIR
    | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR
    | _ACCESS_FS_MAKE_DIR
    | _ACCESS_FS_MAKE_REG
    | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO
    | _ACCESS_FS_MAKE_BLOCK
    | _ACCESS_FS_MAKE_SYM
)

UNAVAILABLE_WARNING = (
    "Landlock is unavailable on this kernel — worker confinement degrades to "
    "deny-rules only (--disallowedTools); the filesystem boundary is not "
    "kernel-enforced for this round"
)


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi_version() -> int:
    """The kernel's Landlock ABI version, or 0 when Landlock is unavailable —
    too old a kernel, disabled at build time, or blocked by an outer sandbox.
    Never raises: an unsupported ``syscall()`` number simply returns -1."""
    result = _LIBC.syscall(_SYS_LANDLOCK_CREATE_RULESET, None, 0, _LANDLOCK_CREATE_RULESET_VERSION)
    return result if result > 0 else 0


def encode_cwd(cwd: Path) -> str:
    """The directory-name encoding the real CLI uses under
    ``~/.claude/projects/`` for a given working directory — mirrors
    ``tests/fake_claude.py``'s ``_write_transcript`` so the worker's own
    project dir (computed here) and the transcript it actually writes to
    (discovered by ``SessionRunner.transcript_path``) name the same directory.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def probe_claude_runtime_dirs(claude_home: Path) -> list[str]:
    """Real subdirectories of ``~/.claude`` at confinement-setup time — an
    executed probe, not a hardcoded guess, so a future runtime subdir a worker
    legitimately needs (or one that stops existing) is picked up automatically.

    Excludes ``projects``: that is where every worker's own transcripts *and*
    every operator's memory live side by side, keyed by slug. It is handled
    separately — only the worker's own project slug is ever added to the
    allowlist, with its own rule — so the rest of ``projects`` (every operator
    memory dir among them) is excluded by construction, not by a subtraction
    Landlock cannot express.
    """
    if not claude_home.is_dir():
        return []
    return sorted(p.name for p in claude_home.iterdir() if p.is_dir() and p.name != "projects")


#: Paths every ordinary Unix process expects to be able to write, and which
#: protect nothing by being denied.
#:
#: Found by running a real confined subprocess rather than by reasoning: without
#: ``/dev/null`` even ``git --version`` exits 128 ("could not open '/dev/null'"),
#: which would have broken every confined worker on its first commit, and without
#: a temp dir git, editors and ``tempfile`` all fail. Denying these buys no
#: isolation — the asset under guard is the operator's memory, not the null
#: device — while breaking essentially every tool a worker runs.
_SYSTEM_WRITE_PATHS = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
    "/dev/shm",
    "/tmp",
    "/var/tmp",
)


def system_write_paths() -> list[Path]:
    """The always-writable system paths, including ``$TMPDIR`` when it points
    somewhere other than the standard temp dirs.

    Deliberately **no cache dirs**. They used to be appended here, which meant
    the policy grew a line per ecosystem — `~/.cache`, then `~/.npm`, with
    `~/.cargo`, `~/.gradle`, `~/.m2`, `~/go/pkg` queued behind them, each
    omission presenting as a mysterious `permission_denied`. A worker's caches
    now live under one orchestrator-owned root that the worker's *environment*
    points every toolchain at (see ``_CACHE_LAYOUT``), so the allowlist carries
    one rule instead of an enumeration of other people's conventions.
    """
    paths = [Path(p) for p in _SYSTEM_WRITE_PATHS]
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        candidate = Path(tmpdir)
        if candidate not in paths:
            paths.append(candidate)
    return paths


#: The directory name under ``${XDG_CACHE_HOME:-$HOME/.cache}`` that holds every
#: worker cache, for every group of every run.
CACHE_ROOT_NAME = "smart-mcps-orchestrator"

#: ``env var -> subdirectory`` under the cache root. The single source of truth
#: for both halves of the mechanism: ``worker_cache_env`` (what the worker's
#: toolchains are told) and ``worker_cache_dirs`` (what Landlock allows). Deriving
#: both from this tuple is what makes them incapable of drifting — a new entry
#: lands in the environment *and* the allowlist at once, and the anti-drift test
#: proves it.
#:
#: Notes on the awkward ones:
#:
#: - ``CARGO_HOME``/``RUSTUP_HOME``/``GRADLE_USER_HOME``/``GOPATH`` are not
#:   caches strictly speaking — they are tool *homes* that happen to hold the
#:   cache. Redirecting the whole home is what the tools support, and the content
#:   is regenerable either way.
#: - Maven has no cache env var; it takes ``-Dmaven.repo.local`` on the command
#:   line, which is why ``MAVEN_OPTS`` is handled specially (and *appended*) in
#:   ``worker_cache_env`` rather than assigned from here.
#: - ``XDG_CACHE_HOME`` comes last as the catch-all, covering every XDG-abiding
#:   tool nobody has enumerated. ``XDG_DATA_HOME`` and ``XDG_CONFIG_HOME`` are
#:   deliberately *not* redirected: those hold credentials and configuration a
#:   worker legitimately reads, not regenerable caches.
_CACHE_LAYOUT: tuple[tuple[str, str], ...] = (
    ("UV_CACHE_DIR", "uv"),
    ("npm_config_cache", "npm"),
    ("PIP_CACHE_DIR", "pip"),
    ("CARGO_HOME", "cargo"),
    ("RUSTUP_HOME", "rustup"),
    ("GRADLE_USER_HOME", "gradle"),
    ("GOPATH", "go"),
    ("GOCACHE", "go-build"),
    ("XDG_CACHE_HOME", "xdg"),
)

#: Maven's repository is set on the command line, not by an env var.
_MAVEN_SUBDIR = "maven"
_MAVEN_OPT = "-Dmaven.repo.local="


def default_cache_root(environ: dict[str, str] | None = None) -> Path:
    """``${XDG_CACHE_HOME:-$HOME/.cache}/smart-mcps-orchestrator``.

    Resolved from the **orchestrator's** environment, once, at CLI startup — not
    from a worker's, which is about to have its own ``XDG_CACHE_HOME`` pointed
    inside this very root. Reading it later, from the wrong environment, would
    nest a root inside itself.

    User-level rather than per-repo, on purpose. The Landlock cost is one rule
    either way, while per-repo would pay a full cold cache for every repo *and*
    park a multi-gigabyte cache inside the same `.orchestrator/` tree operators
    delete to clean up run artifacts. Shared across groups and across runs, so
    the second group of a run finds the first one's downloads already there.
    """
    env = os.environ if environ is None else environ
    raw = env.get("XDG_CACHE_HOME")
    if raw:
        base = Path(raw)
    else:
        home = env.get("HOME")
        base = Path(home) / ".cache" if home else Path.home() / ".cache"
    return base / CACHE_ROOT_NAME


def worker_cache_env(root: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment overlay that points a worker's toolchains at ``root``.

    ``base`` is consulted for one reason only: ``MAVEN_OPTS`` is a free-form
    option string an operator may already be setting, so this **appends** to it
    rather than clobbering it. Everything else is an assignment.
    """
    env = {var: str(root / subdir) for var, subdir in _CACHE_LAYOUT}
    existing = (base or {}).get("MAVEN_OPTS", "").strip()
    maven_opt = f"{_MAVEN_OPT}{root / _MAVEN_SUBDIR}"
    env["MAVEN_OPTS"] = f"{existing} {maven_opt}".strip() if existing else maven_opt
    return env


def worker_cache_dirs(root: Path, *, create: bool = False) -> list[Path]:
    """Every directory ``worker_cache_env`` points a toolchain at, for the
    allowlist.

    ``create=True`` materializes them, and callers spawning a worker must pass it
    every time rather than once at startup: **Landlock rules address existing
    paths** (see ``_add_rule``'s early return) so a directory that has been
    deleted mid-run — by an operator clearing caches, say — would silently drop
    out of the ruleset and reproduce the original cache-`EACCES` defect exactly.
    """
    dirs = [root / subdir for _, subdir in _CACHE_LAYOUT]
    dirs.append(root / _MAVEN_SUBDIR)
    if create:
        for path in dirs:
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError:
                continue
    return dirs


def worktree_git_dirs(worktree: Path) -> list[Path]:
    """The git directories a **linked worktree** must write to in order to commit.

    In a `git worktree`, `<worktree>/.git` is a *file* pointing elsewhere:

        $ cat .worktrees/g1-…/.git
        gitdir: /home/gbm1996/wksp/drummAI/.git/worktrees/g1-…

    That per-worktree dir holds HEAD, the index and the reflog; the common dir
    (`…/drummAI/.git`) holds `objects/` and `refs/`. **Both live outside the
    worktree**, so a policy granting only the worktree leaves `git commit`
    impossible — which is exactly what happened on run r20260812-202855:

        status: permission_denied
        denied_command: git add -A && git commit -q -m "test"
        summary: U1 and U2 are both implemented, tested and verified … the work is
                 complete on disk but uncommitted because the sandbox blocks all
                 git writes.

    A group that cannot commit is a group that merges empty while reporting
    success, so this has to be writable for confinement to be usable at all.

    Returns an empty list when *worktree* is not a git repo, or git is unusable.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", "--git-dir", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    dirs: list[Path] = []
    for line in result.stdout.split("\n"):
        line = line.strip()
        if not line:
            continue
        # `rev-parse` may answer relatively (".git") for a normal checkout.
        path = Path(line)
        if not path.is_absolute():
            path = (worktree / path).resolve()
        if path.is_dir() and path not in dirs:
            dirs.append(path)
    return dirs


#: File-editing tools that could rewrite an operator memory file. ``Read`` is
#: deliberately absent: a worker reading the operator's notes is not the damage
#: mode (both observed incidents were *edits*), and denying reads would also
#: block legitimate context the permission layer cannot tell apart.
_EDITING_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")


def operator_memory_deny_patterns(claude_home: Path) -> list[str]:
    """``--disallowedTools`` rules blocking writes to *every* project's auto-memory
    directory under ``claude_home``.

    This is defence in depth, not the boundary. Landlock is the boundary — it
    excludes operator memory by construction (see ``probe_claude_runtime_dirs``).
    But confinement degrades to a warning on a kernel without Landlock, and that
    degrade path is exactly where the two observed incidents would recur, so the
    deny rules have to stand on their own there.

    The pattern is deliberately ``**/memory/**`` across all of ``projects/``
    rather than the operator's slug alone: a worker in project A has written into
    project B's memory before, so naming one slug would leave the observed hole
    open.
    """
    root = claude_home / "projects"
    # Claude Code permission rules take gitignore-style paths, where a leading
    # `//` anchors at the filesystem root. `~` is not expanded here: the rule is
    # handed to a subprocess whose HOME we do not control.
    pattern = f"//{root.as_posix().lstrip('/')}/**/memory/**"
    return [f"{tool}({pattern})" for tool in _EDITING_TOOLS]


@dataclass
class ConfinementPolicy:
    """What one worker subprocess may write: its own worktree, its own project
    dir, and the CLI's own runtime scratch dirs under ``~/.claude``.

    Only write rights are handled (see ``_apply_landlock``), so reads are
    unrestricted throughout — a confined worker still loads its interpreter,
    system libraries and certificates normally. ``read_only`` therefore carries
    no rule; it records what was probed and deliberately left unwritable.
    """

    read_write: list[Path] = field(default_factory=list)
    read_only: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class ConfinementResult:
    applied: bool
    abi_version: int
    warning: str | None = None


def build_policy(
    *,
    worktree: Path,
    claude_home: Path,
    project_slug: str | None = None,
    system_paths: Sequence[Path] | None = None,
    cache_dirs: Sequence[Path] | None = None,
) -> ConfinementPolicy:
    """The policy for a worker running in ``worktree`` with ``~/.claude`` at
    ``claude_home``. ``project_slug`` defaults to the real CLI's own encoding
    of ``worktree`` (its transcript directory name).

    ``system_paths`` defaults to ``system_write_paths()`` and exists so a test
    can pass ``[]``: those defaults include ``/tmp``, which is where pytest puts
    its ``tmp_path`` fixtures, so a boundary assertion written against a fake
    ``claude_home`` under ``/tmp`` would otherwise be allowed by the ``/tmp``
    rule rather than by the rule under test. Production never passes it.

    ``cache_dirs`` is allowlisted **exactly as handed in** — this function never
    re-derives it from a root. The caller (``SessionRunner``) computes the cache
    root once and derives both the worker's environment and this list from it, so
    there is exactly one place where "which caches exist" is decided; deriving it
    again here would be a second place, free to disagree.
    """
    slug = project_slug or encode_cwd(worktree)
    project_dir = claude_home / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    # The probed runtime dirs are writable, not read-only. `claude` writes to its
    # own scratch continuously — `shell-snapshots/`, `sessions/`, `session-env/`,
    # `file-history/`, `tasks/` were all touched within minutes on a live box —
    # so denying writes there does not harden anything, it just breaks the worker.
    # The isolation that matters is `projects/`, which is excluded wholesale by
    # `probe_claude_runtime_dirs` and re-added one slug at a time; every operator
    # memory dir lives under some *other* slug and stays unwritable.
    runtime_dirs = [claude_home / name for name in probe_claude_runtime_dirs(claude_home)]
    # `probe_claude_runtime_dirs` enumerates *directories*, so the credential file
    # sitting directly in `~/.claude` was readable (reads are never restricted)
    # but unwritable — and refreshing an expired OAuth token means rewriting
    # exactly that file. A worker that crossed a token expiry therefore died with
    # "401 OAuth access token has expired. Re-authenticate to continue." on a box
    # where re-authentication was never the problem; an hour-long usage-limit
    # pause makes crossing one near-certain. Granted as a single file rule rather
    # than by opening `~/.claude` itself: everything else at that root
    # (settings.json, history.jsonl) stays unwritable, and a worker could already
    # read this file long before it could write it.
    credentials = [claude_home / ".credentials.json"]
    system = system_write_paths() if system_paths is None else list(system_paths)
    # A linked worktree keeps HEAD, its index and the object store outside the
    # worktree; without these the worker can edit and test but never commit.
    git_dirs = worktree_git_dirs(worktree)
    return ConfinementPolicy(
        read_write=[
            worktree,
            project_dir,
            *git_dirs,
            *runtime_dirs,
            *credentials,
            *system,
            *(list(cache_dirs) if cache_dirs else []),
        ],
        read_only=[],
    )


def landlock_preexec(
    policy: ConfinementPolicy,
) -> tuple[Callable[[], None] | None, ConfinementResult]:
    """Build the ``preexec_fn`` a caller passes to ``subprocess.Popen`` to
    confine the child to ``policy``, plus the outcome of trying. Returns
    ``(None, result)`` — never raising — when Landlock is unavailable: an
    unsupported kernel feature must never fail a group (plan U2); the caller
    just runs the round unconfined (deny-rules from ``--disallowedTools`` are
    the remaining layer)."""
    abi = landlock_abi_version()
    if abi <= 0:
        return None, ConfinementResult(applied=False, abi_version=0, warning=UNAVAILABLE_WARNING)

    read_write = list(policy.read_write)
    read_only = list(policy.read_only)

    def _restrict() -> None:
        _apply_landlock(read_write=read_write, read_only=read_only)

    return _restrict, ConfinementResult(applied=True, abi_version=abi, warning=None)


def warn_once(result: ConfinementResult, *, already_warned: bool) -> bool:
    """Print ``result.warning`` to stderr unless ``already_warned``. Returns
    whether a warning is now considered emitted, so a caller can thread a
    single boolean through many rounds and log the degrade exactly once."""
    if result.warning and not already_warned:
        print(f"warning: {result.warning}", file=sys.stderr)
        return True
    return already_warned


def _apply_landlock(*, read_write: Sequence[Path], read_only: Sequence[Path]) -> None:
    """Runs inside the forked child (via ``preexec_fn``), before ``exec()``.
    Best-effort and silent on internal failure: the parent already decided
    Landlock is available before wiring this in, and there is no good channel
    to report a ``preexec_fn`` failure back to the parent anyway — a broken
    rule here must not corrupt the child's exit status for the round's actual
    work.

    ``read_only`` gets no rule at all: with only the write-class rights
    handled (see ``_WRITE_ACCESS_FS``), the *absence* of a write rule already
    denies writing there — an explicit read-only rule would add nothing. The
    field still documents what was probed and is available to callers that
    want to record or assert on it.
    """
    ruleset_attr = _RulesetAttr(handled_access_fs=_WRITE_ACCESS_FS, handled_access_net=0)
    ruleset_fd = _LIBC.syscall(
        _SYS_LANDLOCK_CREATE_RULESET,
        ctypes.byref(ruleset_attr),
        ctypes.sizeof(ruleset_attr),
        0,
    )
    if ruleset_fd < 0:
        return
    try:
        for path in read_write:
            _add_rule(ruleset_fd, path, _WRITE_ACCESS_FS)
        _LIBC.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
        _LIBC.syscall(_SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0)
    finally:
        os.close(ruleset_fd)


def _add_rule(ruleset_fd: int, path: Path, access: int) -> None:
    if not path.exists():
        return
    # A path_beneath rule on a non-directory is rejected with EINVAL if the mask
    # carries directory-only rights (MAKE_DIR, REMOVE_FILE, …), and the syscall
    # error here is deliberately ignored — so the rule would vanish silently.
    # That is what made `/dev/null` unwritable: `git --version` exited 128 while
    # the rule looked present. Device and file targets get file rights only.
    if not path.is_dir():
        access &= _ACCESS_FS_WRITE_FILE
        if not access:
            return
    fd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)
    try:
        attr = _PathBeneathAttr(allowed_access=access, parent_fd=fd)
        _LIBC.syscall(
            _SYS_LANDLOCK_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(attr), 0
        )
    finally:
        os.close(fd)
