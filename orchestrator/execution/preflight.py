"""LLM-free merge gate: clean worktree, check steps exit zero (plan U4).

Standalone rather than a method on ``IntegrationMerger`` — it runs from two
callers that share no class (the approved-path merge and the resolve-path
merge), needs a config object neither holds, and standalone is what makes it
testable without a session, a merger, or a run. No LLM is ever invoked here:
``Group.verification`` items are prose with no executable field and stay the
reviewer's contract; Preflight only runs mechanical check steps.

The gate used to resolve exactly *one* check command, by marker precedence:
``pyproject.toml``/``uv.lock`` won, and ``package.json`` was reached only when
there were no uv markers. In a repo carrying both — this one — that meant every
merge was gated on ``uv run pytest`` alone and no group's JavaScript was ever
compiled or tested (r20260828-220035 merged ``ui/src/routes/Launch.tsx``
unverified). A check run is now a *sequence* of ``CheckStep``s, so a Python and
a TypeScript suite can both stand between a diff and the integration branch.
"""

from __future__ import annotations

import json
import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from orchestrator.config import DEFAULT_FRONTEND_DIRS, PreflightConfig
from orchestrator.execution.worktrees import is_dirty

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

PreflightFailureKind = Literal["env", "timeout", "regression", "untracked"]


class PreflightFailure(Exception):
    """Preflight declined to let this tree merge.

    ``output_path`` points at the captured check output when the failure came
    from a nonzero exit or a timeout; ``None`` for the dirty-worktree failure,
    which has no check output to point at.

    ``kind`` classifies *why*, so a caller can route the failure instead of
    treating every failure identically: ``"env"`` covers a dirty worktree, a
    collection-phase failure, or an interrupted/internal/usage pytest exit —
    none of which are evidence about the diff; ``"timeout"`` is a hung check
    command; ``"regression"`` is tests that actually ran and actually failed;
    ``"untracked"`` is a tree whose *only* dirt is untracked files (``??``) —
    the coder's own leftovers, which cost a cheap same-spec relaunch to clean
    up rather than a rewrite, and which ``paths`` names.

    ``comparison`` is the baseline comparison the gate already performed
    before deciding to raise (``None`` when it never got that far — a dirty
    tree, a timeout, an env failure). Carried on the exception so
    ``review.py::_classify_preflight`` reads the verdict the gate reached
    rather than re-deriving it from a guessed JUnit path, which knew nothing
    about the UI steps' reports.

    ``step_name`` names which check step failed, so a diagnosis says "tsc"
    rather than only quoting an argv.
    """

    def __init__(
        self,
        reason: str,
        *,
        kind: PreflightFailureKind,
        output_path: Path | None = None,
        comparison: "BaselineComparison | None" = None,
        step_name: str | None = None,
        paths: Sequence[str] = (),
        exit_code: int | None = None,
    ):
        super().__init__(reason)
        self.reason = reason
        self.kind = kind
        self.output_path = output_path
        self.comparison = comparison
        self.step_name = step_name
        self.paths = list(paths)
        self.exit_code = exit_code


def _classify_step_failure(step: "CheckStep", returncode: int, output: str) -> PreflightFailureKind:
    """Classify a nonzero, non-timeout exit for one step.

    The exit-code table below is *pytest's*, and applying it to another tool
    misreads it. ``tsc`` exits **2** on an ordinary type error — the table
    calls 2 "interrupted", which is ``env``, which is not attributable, which
    means the coder never gets a rewrite for a type error it introduced and,
    under ``on-failure halt``, the run dies instead. So the table applies only
    where it is true: the pytest step, and an operator-configured command
    (whose historical contract this is). Any other step's nonzero exit is a
    ``regression`` — evidence about the diff — which is also the safer error:
    a misjudged regression costs a rewrite, a misjudged ``env`` costs the run.
    """
    if step.name not in ("pytest", "configured"):
        return "regression"
    return _classify_exit_failure(returncode, output)


def _classify_exit_failure(returncode: int, output: str) -> PreflightFailureKind:
    """Classify a nonzero, non-timeout check-command exit (plan U1)."""
    if returncode == 1:
        if any(marker in output for marker in _COLLECTION_ERROR_MARKERS):
            return "env"
        return "regression"
    # 2/3/4/5 and any other unrecognized code (e.g. a signal kill) all mean
    # the check command did not produce a real pass over the diff.
    return "env"


# --------------------------------------------------------------- check steps


class CheckStep(BaseModel):
    """One mechanical check the gate runs, in order.

    ``subdir`` is relative to the checkout root (``"ui"`` for the dashboard's
    suites, ``"."`` for the repo's own). ``junit_path`` is where this step
    writes its JUnit report, or ``None`` for a step that emits none (``tsc``,
    a bare ``npm test``) — the distinction decides how a failure of this step
    can be excused (see ``run_preflight``). ``id_prefix`` namespaces the ids
    parsed out of that report so a vitest file path can never collide with a
    pytest one in the union the baseline is compared against.
    """

    name: str
    argv: list[str]
    subdir: str = "."
    junit_path: Path | None = None
    id_prefix: str = ""


def _dev_dependencies(package_json: Path) -> dict[str, str]:
    try:
        data = json.loads(package_json.read_text())
    except (OSError, ValueError):
        return {}
    deps = data.get("devDependencies")
    return deps if isinstance(deps, dict) else {}


def detect_check_steps(
    root: Path,
    *,
    output_dir: Path,
    junit_stem: str = "preflight-junit",
    uv_run_args: Sequence[str] = (),
    frontend_dirs: Sequence[str] = DEFAULT_FRONTEND_DIRS,
) -> list[CheckStep]:
    """Resolve every check step this checkout's own markers imply (plan R7).

    - ``pyproject.toml``/``uv.lock`` -> ``uv run pytest``, with
      ``-p no:cacheprovider`` (so pytest never writes ``.pytest_cache`` into
      the worktree — the clean-tree requirement holds *after* the run, not
      only before it) and ``--junitxml=`` pointing outside the worktree.
    - ``package.json`` at the root, and no uv markers -> ``npm test`` (the
      pre-existing behaviour for a node-only checkout).
    - ``<dir>/package.json`` **and** ``<dir>/node_modules``, for the first
      ``dir`` in ``frontend_dirs`` that has both -> that frontend's own
      suites: ``vitest`` when it is a devDependency (JUnit reporter for the
      gate plus the default reporter so the log keeps vitest's own output,
      ids prefixed ``<dir>::``), otherwise ``npm test``; plus ``tsc --noEmit``
      when ``typescript`` is a devDependency and ``<dir>/tsconfig.json``
      exists.

    ``<dir>/node_modules`` is required rather than provisioned here: a fresh
    worktree has none until ``provision_node_env`` has run, and a machine
    without npm must not be able to fail a merge (see ``run_preflight``).

    An empty list means no check is applied at all (plan R8).
    """
    steps: list[CheckStep] = []
    has_uv = any((root / marker).is_file() for marker in ("pyproject.toml", "uv.lock"))
    if has_uv:
        junit = output_dir / f"{junit_stem}.xml"
        # ``uv_run_args`` are the run's ``provision_args`` (``--all-extras``):
        # the gate must test the environment the worktree was provisioned
        # with. Without them a bare ``uv run`` syncs core deps only — on
        # r20260830-211717 ``uv sync --all-extras`` failed on the TTS extra in
        # every worktree while plain ``uv run pytest`` built a core-only venv
        # and passed 16 tests that never imported the library under test.
        steps.append(
            CheckStep(
                name="pytest",
                argv=[
                    "uv",
                    "run",
                    *uv_run_args,
                    "pytest",
                    "-p",
                    "no:cacheprovider",
                    f"--junitxml={junit}",
                ],
                junit_path=junit,
            )
        )
    elif (root / "package.json").is_file():
        steps.append(CheckStep(name="npm-test", argv=["npm", "test"]))

    frontend = detect_frontend_dir(root, frontend_dirs)
    if frontend is not None:
        ui = root / frontend
        dev = _dev_dependencies(ui / "package.json")
        if "vitest" in dev:
            ui_junit = output_dir / f"{junit_stem}-{frontend}.xml"
            steps.append(
                CheckStep(
                    name="vitest",
                    # Two reporters (F3, r20260924): `junit` for the gate's
                    # failing-test set, `default` so the step log still carries
                    # vitest's own diagnosis — with junit alone the log held
                    # nothing but the XML path, and an "Unhandled Errors" exit
                    # was undiagnosable from the run directory. The per-reporter
                    # `--outputFile.junit=` keeps `default` on stdout.
                    argv=[
                        "npx",
                        "vitest",
                        "run",
                        "--reporter=default",
                        "--reporter=junit",
                        f"--outputFile.junit={ui_junit}",
                    ],
                    subdir=frontend,
                    junit_path=ui_junit,
                    id_prefix=f"{frontend}::",
                )
            )
        else:
            steps.append(
                CheckStep(name=f"npm-test-{frontend}", argv=["npm", "test"], subdir=frontend)
            )
        if "typescript" in dev and (ui / "tsconfig.json").is_file():
            steps.append(CheckStep(name="tsc", argv=["npx", "tsc", "--noEmit"], subdir=frontend))
    return steps


def detect_frontend_dir(root: Path, frontend_dirs: Sequence[str]) -> str | None:
    """The first of ``frontend_dirs`` with both ``package.json`` and
    ``node_modules`` — the one whose suites the gate runs."""
    for name in frontend_dirs:
        if (root / name / "package.json").is_file() and (root / name / "node_modules").is_dir():
            return name
    return None


def frontend_detection_notes(root: Path, frontend_dirs: Sequence[str]) -> list[str]:
    """What the gate will and will not see of this checkout's frontends, for
    the baseline capture log: the detected dir (if any), and a warning per
    top-level ``package.json`` that is *not* the one provisioned — either its
    dir is not in ``frontend_dirs`` or it has no ``node_modules`` — so a
    consumer whose frontend lives somewhere unexpected finds out at launch
    rather than by a suite that silently never ran."""
    detected = detect_frontend_dir(root, frontend_dirs)
    notes = [
        f"preflight baseline: frontend detected in {detected}/ (vitest/tsc steps apply)"
        if detected is not None
        else "preflight baseline: no frontend detected "
        f"(looked for package.json + node_modules in {', '.join(frontend_dirs)})"
    ]
    for child in sorted(root.iterdir()):
        if child.name == detected or not child.is_dir() or child.name.startswith("."):
            continue
        if child.name == "node_modules" or not (child / "package.json").is_file():
            continue
        if child.name not in frontend_dirs:
            why = f"{child.name!r} is not in [preflight] frontend_dirs"
        else:
            why = f"no {child.name}/node_modules"
        notes.append(
            f"preflight baseline: warning — {child.name}/package.json exists but is not "
            f"provisioned or gated ({why})"
        )
    return notes


def configured_check_step(
    command: Sequence[str], *, output_dir: Path, junit_stem: str
) -> CheckStep:
    """Wrap an explicitly configured ``check_command`` as a single step.

    A configured command ending in ``pytest`` is extended the same way the
    detected one is. Before that, ``config.check_command`` short-circuited the
    ``or`` in front of ``detect_check_command``, so an operator-configured
    pytest run produced no JUnit report at all — and a ``regression`` with no
    report compared an *empty* failing set against the baseline, which is a
    subset of everything and let the tree merge unverified.

    ``junit_path`` is set regardless: a configured command may write the report
    itself, and the gate should read one wherever it lands.
    """
    argv = list(command)
    junit = output_dir / f"{junit_stem}.xml"
    if argv and argv[-1] == "pytest":
        argv += ["-p", "no:cacheprovider", f"--junitxml={junit}"]
    return CheckStep(name="configured", argv=argv, junit_path=junit)


def detect_check_command(worktree: Path, *, junit_xml_path: Path | None = None) -> list[str] | None:
    """Legacy single-command view of ``detect_check_steps``.

    Kept because it is the shape external callers and older tests know: the
    *root* check command only, so it cannot see the ``ui/`` steps. Nothing in
    the gate itself calls it any more — ``run_preflight`` and
    ``capture_preflight_baseline`` both resolve the full step list.
    """
    output_dir = junit_xml_path.parent if junit_xml_path is not None else Path(".")
    stem = junit_xml_path.stem if junit_xml_path is not None else "preflight-junit"
    root_steps = [
        step
        for step in detect_check_steps(worktree, output_dir=output_dir, junit_stem=stem)
        if step.subdir == "."
    ]
    if not root_steps:
        return None
    step = root_steps[0]
    if junit_xml_path is None and step.name == "pytest":
        return ["uv", "run", "pytest"]
    return list(step.argv)


def _resolve_steps(
    root: Path,
    *,
    config: PreflightConfig,
    output_dir: Path,
    junit_stem: str,
    uv_run_args: Sequence[str] = (),
) -> list[CheckStep]:
    if config.check_command:
        steps = [
            configured_check_step(
                config.check_command, output_dir=output_dir, junit_stem=junit_stem
            )
        ]
    else:
        # Only non-default values are passed: tests (and any caller) that stand
        # in for ``detect_check_steps`` with a two-keyword double keep working.
        kwargs: dict[str, object] = {}
        if uv_run_args:
            kwargs["uv_run_args"] = uv_run_args
        if list(config.frontend_dirs) != list(DEFAULT_FRONTEND_DIRS):
            kwargs["frontend_dirs"] = config.frontend_dirs
        steps = detect_check_steps(root, output_dir=output_dir, junit_stem=junit_stem, **kwargs)
    for index, command in enumerate(config.extra_check_commands, start=1):
        step = configured_check_step(
            command, output_dir=output_dir, junit_stem=f"{junit_stem}-configured-{index}"
        )
        steps.append(step.model_copy(update={"name": f"configured-{index}"}))
    return steps


# ----------------------------------------------------------------- the gate


def run_preflight(
    worktree: Path,
    *,
    config: PreflightConfig,
    output_dir: Path,
    log: Callable[[str], None] | None = None,
    declared_files: Sequence[str] = (),
    baseline: "PreflightBaseline | None" = None,
    uv_run_args: Sequence[str] = (),
) -> None:
    """Run Preflight's checks against ``worktree``, in order.

    1. The worktree must be clean (plan R6a) — evaluated by the caller's
       ordering, not here: this function only reads ``is_dirty``, so archiving
       reviewer scratch (plan U6) *before* calling this is what makes a
       scratch-only worktree pass.
    2. Every resolved check step (configured, or detected — plan R7/R8) must
       exit zero within ``config.check_timeout_s``; a still-running command is
       killed and counted as a failure (plan Decisions), never degraded to "no
       check applied". Steps run sequentially and the first failure stops the
       run — a tree that cannot compile has nothing to say about its tests.

    Raises ``PreflightFailure`` on any failure; returns ``None`` on success.

    ``declared_files`` — the group's own declared file list — is *reported*,
    never gated on: any entry missing from the worktree is logged as one
    warning line and nothing else. Folding work into an existing file instead
    of creating the declared one is often the right call (g1 of
    r20260819-crashrec put its worktree coverage in ``test_scheduler.py``
    rather than the declared ``tests/test_worktrees.py``), so a hard gate would
    fail honest work. ``PreflightFailure`` stays reserved for the dirty-tree
    and check-step failures.

    ``baseline`` — what was already red on the launch branch — is the gate's
    reference point, not merely an attribution hint. When a step fails but
    introduced nothing new relative to the baseline, the diff caused no
    regression and the tree is allowed through with a logged note.

    **A failure is excused only against comparable evidence.** For a step that
    writes a JUnit report, that means a report that exists and names failures
    to diff; for a step that writes none (``tsc``, a bare ``npm test``), it
    means the baseline recorded *that same step* failing too, so the exit codes
    are comparable. No evidence is no excuse: an empty failing set is a subset
    of every baseline, so treating "no report" as "nothing new" is exactly how
    a silent pass happens. Only ``regression``-kind failures are eligible at
    all — a dirty tree, a collection error, or a timeout never produced a
    comparable result and still fails hard.
    """
    _log = log or (lambda _text: None)
    missing = [name for name in declared_files if not (worktree / name).exists()]
    if missing:
        _log(
            f"preflight: {len(missing)} declared file(s) not present in the worktree "
            f"(reported, not blocking): {', '.join(sorted(missing))}"
        )
    dirty = _dirty_entries(worktree)
    if dirty:
        paths = [path for _status, path in dirty]
        if all(status == "??" for status, _path in dirty):
            raise PreflightFailure(
                f"worktree {worktree} has untracked files: {', '.join(paths)}",
                kind="untracked",
                paths=paths,
            )
        raise PreflightFailure(
            f"worktree {worktree} is not clean: {', '.join(paths)}", kind="env", paths=paths
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    steps = _resolve_steps(
        worktree,
        config=config,
        output_dir=output_dir,
        junit_stem="preflight-junit",
        uv_run_args=uv_run_args,
    )
    # Before the empty-list early return: a checkout where *every* step went
    # missing is the loudest case of the gate being weaker than it was at
    # launch, and the one most likely to pass a broken tree.
    _warn_on_skipped_baseline_steps(steps, baseline, worktree, _log)
    if not steps:
        _log("preflight: no check command configured or detected — check skipped")
        return
    for step in steps:
        _log(f"preflight: step '{step.name}' resolved to {' '.join(step.argv)}")

    for index, step in enumerate(steps):
        _run_one_step(
            worktree,
            step,
            steps_so_far=steps[: index + 1],
            config=config,
            output_dir=output_dir,
            baseline=baseline,
            log=_log,
        )


def _run_one_step(
    worktree: Path,
    step: CheckStep,
    *,
    steps_so_far: Sequence[CheckStep],
    config: PreflightConfig,
    output_dir: Path,
    baseline: "PreflightBaseline | None",
    log: Callable[[str], None],
) -> None:
    output_path = output_dir / f"preflight-check-{step.name}.log"
    try:
        result = subprocess.run(
            step.argv,
            cwd=worktree / step.subdir,
            capture_output=True,
            text=True,
            timeout=config.check_timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        combined = _decode(exc.stdout) + _decode(exc.stderr)
        _write_step_output(output_path, output_dir, combined)
        raise PreflightFailure(
            f"check command {' '.join(step.argv)} timed out after "
            f"{config.check_timeout_s}s — output at {output_path}",
            kind="timeout",
            output_path=output_path,
            step_name=step.name,
        ) from exc
    combined = (result.stdout or "") + (result.stderr or "")
    _write_step_output(output_path, output_dir, combined)
    if result.returncode == 0:
        return

    kind = _classify_step_failure(step, result.returncode, combined)
    comparison: BaselineComparison | None = None
    if kind == "regression":
        comparison = _excuse_comparison(step, steps_so_far=steps_so_far, baseline=baseline)
        if comparison.verdict == "pre_existing":
            by_failing_set = step.junit_path is not None and bool(
                failing_tests_from_steps(steps_so_far)
            )
            excuse = (
                "every failing test was already red on the launch branch"
                if by_failing_set
                else "it exited nonzero on the launch branch too"
            )
            log(
                f"preflight: step '{step.name}' exited {result.returncode}, but {excuse} "
                "— no regression, allowing merge"
            )
            return
    raise PreflightFailure(
        f"check command {' '.join(step.argv)} (step '{step.name}') exited "
        f"{result.returncode} — output at {output_path}",
        kind=kind,
        output_path=output_path,
        comparison=comparison,
        step_name=step.name,
        exit_code=result.returncode,
    )


def _write_step_output(output_path: Path, output_dir: Path, combined: str) -> None:
    """Write a step's output to its own log, and mirror it onto the canonical
    ``preflight-check.log`` — the path every existing reader (the Observatory,
    ``_short_test_summary``, a post-mortem operator) already knows. The mirror
    always holds the *latest* step's output, which on a failing run is the
    failing step's."""
    output_path.write_text(combined)
    (output_dir / "preflight-check.log").write_text(combined)


def _decode(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else value.decode("utf-8", "replace")


def _excuse_comparison(
    step: CheckStep,
    *,
    steps_so_far: Sequence[CheckStep],
    baseline: "PreflightBaseline | None",
) -> "BaselineComparison":
    """Can this step's failure be excused as pre-existing? (plan A1/A3)

    A JUnit step is compared by failing-test set — the union across every step
    that wrote a report, with each step's ``id_prefix`` applied — when that
    union is non-empty. An empty union with a nonzero exit falls through to
    the exit-code comparison (F2): excused if the launch branch's same step
    also exited nonzero, else ``unattributed_exit``.

    A step with no report is compared by exit code against the baseline's
    record of the same step name; with no such record there is nothing
    comparable and the failure stands.
    """
    if baseline is None or not baseline.captured:
        return BaselineComparison(verdict="no_baseline")
    if step.junit_path is not None:
        failing = failing_tests_from_steps(steps_so_far)
        if failing:
            return compare_to_baseline(baseline, failing)
        # F2 (r20260924): a nonzero exit with an empty failing set is the
        # runner itself breaking (vitest "Unhandled Errors"), not "nothing
        # new" — compare by exit code like a report-less step, and name it
        # `unattributed_exit` when the launch branch's step was clean.
        recorded = baseline.step(step.name)
        if recorded is not None and recorded.exit_code != 0:
            return BaselineComparison(verdict="pre_existing")
        return BaselineComparison(verdict="unattributed_exit")
    recorded = baseline.step(step.name)
    if recorded is None:
        return BaselineComparison(verdict="new_failures")
    if recorded.exit_code != 0:
        return BaselineComparison(verdict="pre_existing")
    return BaselineComparison(verdict="new_failures")


def _warn_on_skipped_baseline_steps(
    steps: Sequence[CheckStep],
    baseline: "PreflightBaseline | None",
    worktree: Path,
    log: Callable[[str], None],
) -> None:
    """Say it out loud when the gate is weaker here than it was at launch.

    The frontend steps are *skipped*, never failed, when its ``node_modules`` is
    absent — an ``env``-kind failure raises ``GroupFailure``, which under
    ``on-failure halt`` kills the whole run, and a machine without npm must not
    be able to do that. The asymmetry is loud instead of silent.
    """
    if baseline is None or not baseline.steps:
        return
    present = {step.name for step in steps}
    for recorded in baseline.steps:
        if recorded.name in present:
            continue
        frontend = next(
            (
                name
                for name in DEFAULT_FRONTEND_DIRS
                if (worktree / name / "package.json").is_file()
                and not (worktree / name / "node_modules").is_dir()
            ),
            None,
        )
        reason = f"no {frontend}/node_modules" if frontend else "not detected in this checkout"
        log(f"preflight: step '{recorded.name}' was in the baseline but is skipped here ({reason})")


def _dirty_entries(worktree: Path) -> list[tuple[str, str]]:
    """``(status, path)`` per ``git status --porcelain`` line; ``??`` is untracked."""
    if not is_dirty(worktree):
        return []
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=worktree, capture_output=True, text=True
    )
    return [(line[:2], line[3:]) for line in result.stdout.splitlines() if line.strip()]


def _dirty_paths(worktree: Path) -> list[str]:
    return [path for _status, path in _dirty_entries(worktree)]


def failing_tests_from_junit(xml_path: Path, *, id_prefix: str = "") -> frozenset[str]:
    """Failing/error test ids from a JUnit XML report (plan U3), for the merge
    gate to compare a group's own preflight run against the launch-branch
    baseline. Empty when ``xml_path`` was never written — a collection-phase
    failure never reaches the point of emitting one."""
    if not xml_path.is_file():
        return frozenset()
    results = _parse_junit_results(xml_path, id_prefix=id_prefix)
    return frozenset(
        test_id for test_id, outcome in results.items() if outcome in ("failed", "error")
    )


def failing_tests_from_steps(steps: Iterable[CheckStep]) -> frozenset[str]:
    """Union of the failing ids across every step that wrote a JUnit report."""
    failing: frozenset[str] = frozenset()
    for step in steps:
        if step.junit_path is None:
            continue
        failing |= failing_tests_from_junit(step.junit_path, id_prefix=step.id_prefix)
    return failing


def _parse_junit_results(xml_path: Path, *, id_prefix: str = "") -> dict[str, str]:
    """Map ``classname::name`` -> outcome (``passed``/``failed``/``error``/
    ``skipped``) from a JUnit XML report written by ``--junitxml`` (plan U2).

    ``vitest --reporter=junit`` emits the same ``<testcase classname="…"
    name="…">`` shape pytest does, so one parser serves both; ``id_prefix``
    (``"ui::"``) is what keeps their id spaces apart."""
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
        results[f"{id_prefix}{test_id}"] = outcome
    return results


#: ``unattributed_exit`` (F2, r20260924): a JUnit step exited nonzero with
#: zero failing tests — the runner itself broke (vitest "Unhandled Errors", a
#: worker crash) — and the launch branch's same step exited 0. Not a
#: pre-existing failure, but not a named test either; the diagnosis says so
#: rather than claiming "new failures" it cannot list.
BaselineVerdict = Literal["new_failures", "pre_existing", "no_baseline", "unattributed_exit"]


class BaselineStep(BaseModel):
    """One check step's result on the launch branch.

    ``exit_code`` is what makes a report-less step (``tsc``) comparable at all:
    a step that failed at launch and fails again introduced nothing.
    """

    name: str
    command: list[str] = Field(default_factory=list)
    exit_code: int = 0
    tests: dict[str, str] = Field(default_factory=dict)

    @property
    def failing_tests(self) -> frozenset[str]:
        return frozenset(
            test_id for test_id, outcome in self.tests.items() if outcome in ("failed", "error")
        )


class PreflightBaseline(BaseModel):
    """What was already red on the launch branch before the run started
    (plan U2), persisted as ``preflight-baseline.json``.

    ``captured`` is ``False`` when no step could be run at all (timed out, or
    the command itself failed to launch) — distinct from a successful run with
    an empty ``tests`` mapping, which means the check ran and every test passed
    (a *clean* baseline, not an absent one).

    ``steps`` is the full record, one entry per check step. The top-level
    ``command``/``exit_code``/``tests`` are the first step's, kept so a run
    whose baseline JSON was written by the single-command gate still loads.
    """

    command: list[str]
    commit_sha: str
    exit_code: int
    captured: bool = True
    tests: dict[str, str] = Field(default_factory=dict)
    steps: list[BaselineStep] = Field(default_factory=list)

    @property
    def failing_tests(self) -> frozenset[str]:
        if self.steps:
            failing: frozenset[str] = frozenset()
            for step in self.steps:
                failing |= step.failing_tests
            return failing
        return frozenset(
            test_id for test_id, outcome in self.tests.items() if outcome in ("failed", "error")
        )

    def step(self, name: str) -> BaselineStep | None:
        for recorded in self.steps:
            if recorded.name == name:
                return recorded
        return None


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
    uv_run_args: Sequence[str] = (),
) -> PreflightBaseline:
    """Run every check step once on the launch branch and record its result
    (plan U2).

    ``repo_root`` is the tree to run in. Since F1 (r20260924) the launch passes
    the *provisioned integration worktree* at the launch commit — provisioned
    by ``IntegrationMerger._provision_once`` exactly like every group worktree
    a gate runs in, so ``detect_frontend_dir``'s ``node_modules`` requirement
    is met by the same ``provision_node_env`` and the baseline is comparable
    to the gate. ``commit_sha`` stays the launch HEAD, which the integration
    branch is cut from. There is no clean-tree gate here: a baseline capture
    is not a merge attempt. Unlike the gate, this does **not** stop at the first failing
    step: a group's ``tsc`` failure can only be excused if the launch branch's
    own ``tsc`` exit code is on record, and a red pytest run must not hide it.
    A step that cannot be run at all is simply absent from ``steps``; when *no*
    step ran, the baseline is recorded ``captured=False`` — never as a false
    "everything passed" or a false "everything failed".
    """
    _log = log or (lambda _text: None)
    output_dir.mkdir(parents=True, exist_ok=True)
    for note in frontend_detection_notes(repo_root, config.frontend_dirs):
        _log(note)
    steps = _resolve_steps(
        repo_root,
        config=config,
        output_dir=output_dir,
        junit_stem="preflight-baseline-junit",
        uv_run_args=uv_run_args,
    )
    if not steps:
        _log("preflight baseline: no check command configured or detected — baseline is empty")
        return PreflightBaseline(command=[], commit_sha=commit_sha, exit_code=0, tests={})

    recorded: list[BaselineStep] = []
    transcript: list[str] = []
    for step in steps:
        try:
            result = subprocess.run(
                step.argv,
                cwd=repo_root / step.subdir,
                capture_output=True,
                text=True,
                timeout=config.check_timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            _log(f"preflight baseline: step '{step.name}' could not be captured ({exc}) — absent")
            continue
        transcript.append(
            f"=== step: {step.name} ({' '.join(step.argv)}) ===\n"
            + (result.stdout or "")
            + (result.stderr or "")
        )
        tests = (
            _parse_junit_results(step.junit_path, id_prefix=step.id_prefix)
            if step.junit_path is not None and step.junit_path.is_file()
            else {}
        )
        recorded.append(
            BaselineStep(
                name=step.name,
                command=list(step.argv),
                exit_code=result.returncode,
                tests=tests,
            )
        )
        _log(
            f"preflight baseline: step '{step.name}' exited {result.returncode} "
            f"with {len(tests)} test outcome(s)"
        )
    (output_dir / "preflight-baseline.log").write_text("\n".join(transcript))
    if not recorded:
        return PreflightBaseline(
            command=list(steps[0].argv),
            commit_sha=commit_sha,
            exit_code=-1,
            captured=False,
            tests={},
        )
    first = recorded[0]
    _log(
        f"preflight baseline: captured {len(recorded)} step(s) at {commit_sha} "
        f"in {repo_root} ({len(first.tests)} test outcome(s) from '{first.name}')"
    )
    return PreflightBaseline(
        command=first.command,
        commit_sha=commit_sha,
        exit_code=first.exit_code,
        captured=True,
        tests=first.tests,
        steps=recorded,
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

    Note this answers only "is this set new?" — it says nothing about whether
    the set is *evidence*. An empty set compares as ``pre_existing`` against
    every baseline, so the gate checks for evidence before asking
    (``_excuse_comparison``).
    """
    if baseline is None or not baseline.captured:
        return BaselineComparison(verdict="no_baseline", new_failures=frozenset())
    new = frozenset(failing_tests) - baseline.failing_tests
    verdict: BaselineVerdict = "new_failures" if new else "pre_existing"
    return BaselineComparison(verdict=verdict, new_failures=new)
