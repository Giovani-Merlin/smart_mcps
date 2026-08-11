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


@dataclass
class ConfinementPolicy:
    """What one worker subprocess may touch: full read-write on its own
    worktree and project dir, read-only on the rest of the enumerated
    ``~/.claude`` allowlist."""

    read_write: list[Path] = field(default_factory=list)
    read_only: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class ConfinementResult:
    applied: bool
    abi_version: int
    warning: str | None = None


def build_policy(
    *, worktree: Path, claude_home: Path, project_slug: str | None = None
) -> ConfinementPolicy:
    """The policy for a worker running in ``worktree`` with ``~/.claude`` at
    ``claude_home``. ``project_slug`` defaults to the real CLI's own encoding
    of ``worktree`` (its transcript directory name)."""
    slug = project_slug or encode_cwd(worktree)
    project_dir = claude_home / "projects" / slug
    project_dir.mkdir(parents=True, exist_ok=True)
    read_only = [claude_home / name for name in probe_claude_runtime_dirs(claude_home)]
    return ConfinementPolicy(read_write=[worktree, project_dir], read_only=read_only)


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
    fd = os.open(str(path), os.O_PATH | os.O_CLOEXEC)
    try:
        attr = _PathBeneathAttr(allowed_access=access, parent_fd=fd)
        _LIBC.syscall(
            _SYS_LANDLOCK_ADD_RULE, ruleset_fd, _LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(attr), 0
        )
    finally:
        os.close(fd)
