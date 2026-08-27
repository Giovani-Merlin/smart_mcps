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
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from orchestrator.config import PreflightConfig
from orchestrator.execution.worktrees import is_dirty

# Detected in this order: a uv-managed checkout wins over a node one when a repo
# somehow carries both, since `pyproject.toml`/`uv.lock` are the more specific
# signal of what this orchestrator itself is built with.
_DETECTORS: tuple[tuple[tuple[str, ...], list[str]], ...] = (
    (("pyproject.toml", "uv.lock"), ["uv", "run", "pytest"]),
    (("package.json",), ["npm", "test"]),
)

# pytest exit codes (https://docs.pytest.org/en/stable/reference/exit-codes.html):
# 1 tests ran and some failed (a real regression); 2 interrupted; 3 internal
# error; 4 usage error; 5 no tests collected. Only 1 can possibly be evidence
# about the diff — 2/3/4/5 mean zero tests ran under conditions unrelated to
# what the diff changed.

# Substrings that mark a pytest collection-phase failure: the run never got to
# execute a single test, so an exit code of 1 here is not evidence about the
# diff either. Deliberately literal string matching, not output-format
# parsing — the JUnit XML is the structured source of truth (see
# `_parse_junit_results`); this is the fallback for check commands that never
# produce one.
_COLLECTION_ERROR_MARKERS = (
    "ImportError while loading conftest.py",
    "ModuleNotFoundError",
    "ImportError",
)

PreflightFailureKind = Literal["env", "timeout", "regression"]


class PreflightFailure(Exception):
    """Preflight declined to let this tree merge.

    ``output_path`` points at the captured check-command output when the
    failure came from a nonzero exit or a timeout; ``None`` for the
    dirty-worktree failure, which has no check output to point at.

    ``kind`` classifies *why*, so a caller can route the failure instead of
    treating every failure identically: ``"env"`` covers a dirty worktree, a
    collection-phase failure, or an interrupted/internal/usage pytest exit —
    none of which are evidence about the diff; ``"timeout"`` is a hung check
    command; ``"regression"`` is tests that actually ran and actually failed.
    """

    def __init__(
        self,
        reason: str,
        *,
        kind: PreflightFailureKind,
        output_path: Path | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.output_path = output_path


def _classify_exit_failure(returncode: int, output: str) -> PreflightFailureKind:
    """Classify a nonzero, non-timeout check-command exit (plan U1)."""
    if returncode == 1:
        if any(marker in output for marker in _COLLECTION_ERROR_MARKERS):
            return "env"
        return "regression"
    # 2/3/4/5 and any other unrecognized code (e.g. a signal kill) all mean
    # the check command did not produce a real pass over the diff.
    return "env"


def detect_check_command(worktree: Path, *, junit_xml_path: Path | None = None) -> list[str] | None:
    """Resolve the check command from the checkout's own markers (plan R7).

    ``pyproject.toml``/``uv.lock`` -> ``uv run pytest``; ``package.json`` (and
    no uv markers) -> ``npm test``; neither -> ``None``, meaning no check
    command is applied at all (plan R8).

    When ``junit_xml_path`` is given and the resolved command is pytest, it is
    extended with ``-p no:cacheprovider`` (so pytest never writes
    ``.pytest_cache`` into the worktree — the clean-tree requirement holds
    after the run, not just before it) and ``--junitxml=<junit_xml_path>``,
    which is always a path outside the worktree, for structured per-test
    results (plan U1/U2).
    """
    for markers, command in _DETECTORS:
        if any((worktree / marker).is_file() for marker in markers):
            resolved = list(command)
            if junit_xml_path is not None and resolved[-1] == "pytest":
                resolved += ["-p", "no:cacheprovider", f"--junitxml={junit_xml_path}"]
            return resolved
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
        raise PreflightFailure(
            f"worktree {worktree} is not clean: {', '.join(dirty_paths)}", kind="env"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    junit_path = output_dir / "preflight-junit.xml"
    command = config.check_command or detect_check_command(worktree, junit_xml_path=junit_path)
    if command is None:
        _log("preflight: no check command configured or detected — check skipped")
        return
    _log(f"preflight: check command resolved to {' '.join(command)}")

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
            kind="timeout",
            output_path=output_path,
        ) from exc
    combined = (result.stdout or "") + (result.stderr or "")
    output_path.write_text(combined)
    if result.returncode != 0:
        kind = _classify_exit_failure(result.returncode, combined)
        raise PreflightFailure(
            f"check command {' '.join(command)} exited {result.returncode} — "
            f"output at {output_path}",
            kind=kind,
            output_path=output_path,
        )


def _dirty_paths(worktree: Path) -> list[str]:
    if not is_dirty(worktree):
        return []
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    )
    return [line[3:] for line in result.stdout.splitlines() if line.strip()]


def failing_tests_from_junit(xml_path: Path) -> frozenset[str]:
    """Failing/error test ids from a JUnit XML report (plan U3), for the merge
    gate to compare a group's own preflight run against the launch-branch
    baseline. Empty when ``xml_path`` was never written — a collection-phase
    failure never reaches the point of emitting one."""
    if not xml_path.is_file():
        return frozenset()
    results = _parse_junit_results(xml_path)
    return frozenset(
        test_id for test_id, outcome in results.items() if outcome in ("failed", "error")
    )


def _parse_junit_results(xml_path: Path) -> dict[str, str]:
    """Map ``classname::name`` -> outcome (``passed``/``failed``/``error``/
    ``skipped``) from a JUnit XML report written by ``--junitxml`` (plan U2)."""
    tree = ET.parse(xml_path)
    results: dict[str, str] = {}
    for case in tree.getroot().iter("testcase"):
        classname = case.get("classname", "")
        name = case.get("name", "")
        test_id = f"{classname}::{name}" if classname else name
        if case.find("failure") is not None:
            outcome = "failed"
        elif case.find("error") is not None:
            outcome = "error"
        elif case.find("skipped") is not None:
            outcome = "skipped"
        else:
            outcome = "passed"
        results[test_id] = outcome
    return results


BaselineVerdict = Literal["new_failures", "pre_existing", "no_baseline"]


class PreflightBaseline(BaseModel):
    """What was already red on the launch branch before the run started
    (plan U2), persisted as ``preflight-baseline.json``.

    ``captured`` is ``False`` when the check command could not be run at all
    (timed out, or the command itself failed to launch) — distinct from a
    successful run with an empty ``tests`` mapping, which means the check
    command ran and every test passed (a *clean* baseline, not an absent
    one).
    """

    command: list[str]
    commit_sha: str
    exit_code: int
    captured: bool = True
    tests: dict[str, str] = Field(default_factory=dict)

    @property
    def failing_tests(self) -> frozenset[str]:
        return frozenset(
            test_id for test_id, outcome in self.tests.items() if outcome in ("failed", "error")
        )


class BaselineComparison(BaseModel):
    """The result of comparing a group's failing tests against the baseline."""

    verdict: BaselineVerdict
    new_failures: frozenset[str] = Field(default_factory=frozenset)


def capture_preflight_baseline(
    repo_root: Path,
    *,
    config: PreflightConfig,
    output_dir: Path,
    commit_sha: str,
    log: Callable[[str], None] | None = None,
) -> PreflightBaseline:
    """Run the check command once on the launch branch and record its
    per-test outcome set (plan U2).

    Runs directly against ``repo_root`` — the launch branch, not a group
    worktree — so there is no clean-tree gate here: a baseline capture is not
    a merge attempt. A command that cannot be run at all (times out, or fails
    to launch) is recorded ``captured=False`` — never as a false "everything
    passed" or a false "everything failed".
    """
    _log = log or (lambda _text: None)
    output_dir.mkdir(parents=True, exist_ok=True)
    junit_path = output_dir / "preflight-baseline-junit.xml"
    command = config.check_command or detect_check_command(repo_root, junit_xml_path=junit_path)
    if command is None:
        _log("preflight baseline: no check command configured or detected — baseline is empty")
        return PreflightBaseline(command=[], commit_sha=commit_sha, exit_code=0, tests={})

    try:
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=config.check_timeout_s,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        _log(f"preflight baseline: could not capture ({exc}) — recording as absent")
        return PreflightBaseline(
            command=command, commit_sha=commit_sha, exit_code=-1, captured=False, tests={}
        )

    (output_dir / "preflight-baseline.log").write_text(
        (result.stdout or "") + (result.stderr or "")
    )
    tests = _parse_junit_results(junit_path) if junit_path.is_file() else {}
    _log(
        f"preflight baseline: captured {len(tests)} test outcome(s) at {commit_sha} "
        f"(exit {result.returncode})"
    )
    return PreflightBaseline(
        command=command,
        commit_sha=commit_sha,
        exit_code=result.returncode,
        captured=True,
        tests=tests,
    )


def save_baseline(path: Path, baseline: PreflightBaseline) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.model_dump_json(indent=2) + "\n")


def load_baseline(path: Path) -> PreflightBaseline | None:
    if not path.is_file():
        return None
    return PreflightBaseline.model_validate_json(path.read_text())


def compare_to_baseline(
    baseline: PreflightBaseline | None, failing_tests: Iterable[str]
) -> BaselineComparison:
    """Compare a group's failing-test set against the launch-branch baseline
    (plan U2/U3).

    ``no_baseline`` — no baseline could be captured — never reports a failure
    as new: the caller cannot attribute anything without something to compare
    against. ``pre_existing`` means every failing test was already failing on
    the launch branch. Anything else is ``new_failures``, carrying exactly the
    set of test ids the baseline did not already have failing.
    """
    if baseline is None or not baseline.captured:
        return BaselineComparison(verdict="no_baseline", new_failures=frozenset())
    new = frozenset(failing_tests) - baseline.failing_tests
    verdict: BaselineVerdict = "new_failures" if new else "pre_existing"
    return BaselineComparison(verdict=verdict, new_failures=new)
