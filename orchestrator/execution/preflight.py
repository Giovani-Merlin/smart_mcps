"""LLM-free merge gate: clean worktree, check command exits zero (plan U4).

Standalone rather than a method on ``IntegrationMerger`` — it runs from two
callers that share no class (the approved-path merge and the resolve-path
merge), needs a config object neither holds, and standalone is what makes it
testable without a session, a merger, or a run. No LLM is ever invoked here:
``Group.verification`` items are prose with no executable field and stay the
reviewer's contract; Preflight only runs a mechanical check command.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from orchestrator.config import PreflightConfig
from orchestrator.execution.worktrees import is_dirty

# Detected in this order: a uv-managed checkout wins over a node one when a repo
# somehow carries both, since `pyproject.toml`/`uv.lock` are the more specific
# signal of what this orchestrator itself is built with.
_DETECTORS: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    (("pyproject.toml", "uv.lock"), ["uv", "run", "pytest"]),
    (("package.json",), ["npm", "test"]),
)


class PreflightFailure(Exception):
    """Preflight declined to let this tree merge.

    ``output_path`` points at the captured check-command output when the
    failure came from a nonzero exit or a timeout; ``None`` for the
    dirty-worktree failure, which has no check output to point at.
    """

    def __init__(self, reason: str, *, output_path: Path | None = None):
        super().__init__(reason)
        self.reason = reason
        self.output_path = output_path


def detect_check_command(worktree: Path) -> list[str] | None:
    """Resolve the check command from the checkout's own markers (plan R7).

    ``pyproject.toml``/``uv.lock`` -> ``uv run pytest``; ``package.json`` (and
    no uv markers) -> ``npm test``; neither -> ``None``, meaning no check
    command is applied at all (plan R8).
    """
    for markers, command in _DETECTORS:
        if any((worktree / marker).is_file() for marker in markers):
            return command
    return None


def run_preflight(
    worktree: Path,
    *,
    config: PreflightConfig,
    output_dir: Path,
    log: Callable[[str], None] | None = None,
    declared_files: Sequence[str] = (),
) -> None:
    """Run Preflight's two checks against ``worktree``, in order.

    1. The worktree must be clean (plan R6a) — evaluated by the caller's
       ordering, not here: this function only reads ``is_dirty``, so archiving
       reviewer scratch (plan U6) *before* calling this is what makes a
       scratch-only worktree pass.
    2. The resolved check command (configured, or detected — plan R7/R8) must
       exit zero within ``config.check_timeout_s``; a still-running command is
       killed and counted as a failure (plan Decisions), never degraded to "no
       check applied".

    Raises ``PreflightFailure`` on either failure; returns ``None`` on success.

    ``declared_files`` — the group's own declared file list — is *reported*,
    never gated on: any entry missing from the worktree is logged as one
    warning line and nothing else. Folding work into an existing file instead
    of creating the declared one is often the right call (g1 of
    r20260819-crashrec put its worktree coverage in ``test_scheduler.py``
    rather than the declared ``tests/test_worktrees.py``), so a hard gate would
    fail honest work. ``PreflightFailure`` stays reserved for the dirty-tree
    and check-command failures.
    """
    _log = log or (lambda _text: None)
    missing = [name for name in declared_files if not (worktree / name).exists()]
    if missing:
        _log(
            f"preflight: {len(missing)} declared file(s) not present in the worktree "
            f"(reported, not blocking): {', '.join(sorted(missing))}"
        )
    dirty_paths = _dirty_paths(worktree)
    if dirty_paths:
        raise PreflightFailure(f"worktree {worktree} is not clean: {', '.join(dirty_paths)}")

    command = config.check_command or detect_check_command(worktree)
    if command is None:
        _log("preflight: no check command configured or detected — check skipped")
        return
    _log(f"preflight: check command resolved to {' '.join(command)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "preflight-check.log"
    try:
        result = subprocess.run(
            command,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=config.check_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        combined = (exc.stdout or "") + (exc.stderr or "")
        output_path.write_text(combined)
        raise PreflightFailure(
            f"check command {' '.join(command)} timed out after "
            f"{config.check_timeout_s}s — output at {output_path}",
            output_path=output_path,
        ) from exc
    output_path.write_text((result.stdout or "") + (result.stderr or ""))
    if result.returncode != 0:
        raise PreflightFailure(
            f"check command {' '.join(command)} exited {result.returncode} — "
            f"output at {output_path}",
            output_path=output_path,
        )


def _dirty_paths(worktree: Path) -> list[str]:
    if not is_dirty(worktree):
        return []
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    )
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]
