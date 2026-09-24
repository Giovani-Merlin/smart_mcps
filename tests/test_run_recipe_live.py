"""A whole run driving a real `run` recipe group into a real `code` group,
against the real `claude` CLI and a real Landlock ruleset (plan U9, g8).

**This tier spends real tokens.** Excluded from a plain `pytest` by
`addopts = -m "not llm"`; opt in with `uv run pytest -m llm`.

Why it exists: U1-U8 shipped the registry, the task-map field, pricing, the
partitioner's singleton isolation, the executor dispatcher, the Artifact
Manifest, the bundle export and the `run` executor itself — every one of them
against `tests/fake_claude.py` or in-process fixtures. Nothing had yet driven
a `run` group's confined Run Child to completion, fed its Artifact Manifest
entry into a real downstream coder's first prompt, and exported both through
the real Run Bundle. This file is that proof, in the spirit of
`tests/test_e2e_live.py`.

Like that file, these tests assert almost nothing about *content* — only that
a `run` group actually executes its declared commands as confined
subprocesses, registers what it produced, and that a dependent `code` group's
prompt actually carried the upstream entry through to a real transcript.
"""

from __future__ import annotations

import gzip
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestrator.cli import main
from orchestrator.execution.manifest import RunPaths
from orchestrator.model import (
    Group,
    GroupingResult,
    ReviewIntensity,
    VerificationItem,
)
from orchestrator.grouping.pipeline import serialize_grouping

pytestmark = [
    pytest.mark.llm,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]

#: A whole two-group run. Generous relative to a healthy run (the `run` group's
#: own commands are seconds; the `code` group is one small coder round) but far
#: below a wedged-forever failure.
RUN_TIMEOUT_S = 900.0

_CONFIG_TOML = """\
[session]
confine = true

[escalation]
enabled = false

[recipes]
enabled = ["run"]
"""


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)}: {done.stderr}"
    return done.stdout


def _run_group(*, commands_exit: str = "0") -> Group:
    """A `run` group whose one command writes an output file and a
    measurements file, then optionally exits non-zero (the triage fixture)."""
    cmd = "printf hello > run-output.txt && printf '{\"frames\": 3}' > run-measurements.json"
    if commands_exit != "0":
        cmd += f" && exit {commands_exit}"
    return Group(
        id="g1",
        name="render",
        summary="Render a tiny output file as a run recipe unit.",
        spec="(run recipe — no coder prompt)",
        difficulty=0.0,
        intensity=ReviewIntensity.SELF_VERIFY,
        recipe="run",
        recipe_args={
            "commands": [{"cmd": cmd, "wall_clock_min": 2.0}],
            "outputs": ["run-output.txt"],
            "measurements": "run-measurements.json",
            "commit_paths": ["run-output.txt", "run-measurements.json"],
        },
        verification=[VerificationItem(id="v1", description="run-output.txt exists after the run")],
    )


def _code_group() -> Group:
    return Group(
        id="g2",
        name="farewell",
        summary="Add a farewell function beside greet().",
        spec=(
            "In greeting.py, add a function `farewell()` returning the string "
            '"goodbye", right after `greet()`. Then commit the change. '
            "Do not change anything else."
        ),
        difficulty=0.1,
        intensity=ReviewIntensity.SELF_VERIFY,
        dependencies=["g1"],
        files=["greeting.py"],
        verification=[
            VerificationItem(id="v2", description="farewell() exists and returns goodbye")
        ],
    )


def _build_repo(tmp_path_factory, *, commands_exit: str = "0") -> Path:
    repo = tmp_path_factory.mktemp("live-repo-run-recipe")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "live@test")
    _git(repo, "config", "user.name", "live")
    (repo / "README.md").write_text("# live run-recipe fixture\n")
    (repo / "greeting.py").write_text('def greet():\n    return "hello"\n')
    (repo / "plan.md").write_text(
        "# live run-recipe plan\n\n## Tasks\n\n"
        "- u1-render (recipe: run): write run-output.txt\n"
        "- u2-farewell: add farewell() beside greet()\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    (repo / ".orchestrator").mkdir()
    (repo / ".orchestrator" / "config.toml").write_text(_CONFIG_TOML)

    groups = [_run_group(commands_exit=commands_exit)]
    if commands_exit == "0":
        groups.append(_code_group())

    grouping_dir = repo / ".orchestrator" / "groupings" / "plan"
    grouping_dir.mkdir(parents=True)
    (grouping_dir / "groups.json").write_text(
        serialize_grouping(GroupingResult(plan_path="plan.md", groups=groups))
    )
    (grouping_dir / "base-context.md").write_text(
        "This is a tiny scratch repository used to verify a `run` recipe unit "
        "feeding a `code` unit end to end. Keep every change minimal.\n"
    )
    return repo


def _run_main(repo: Path, run_id: str, *, expected_exit: int = 0) -> str:
    log_path = repo / "run.log"
    with log_path.open("w") as sink:
        saved, sys.stdout = sys.stdout, sink
        try:
            exit_code = main(
                ["run", "--repo", str(repo), "--run-id", run_id, "--intensity", "autonomous"]
            )
        finally:
            sys.stdout = saved
    output = log_path.read_text()
    assert exit_code == expected_exit, f"run exited {exit_code}\n{output}"
    return output


@pytest.fixture(scope="session")
def live_repo(tmp_path_factory) -> Path:
    return _build_repo(tmp_path_factory)


@pytest.fixture(scope="session")
def completed_run(live_repo: Path) -> tuple[Path, str, str]:
    run_id = "run-recipe-live1"
    started = time.time()
    output = _run_main(live_repo, run_id)
    elapsed = time.time() - started
    assert elapsed < RUN_TIMEOUT_S, f"the run did not terminate ({elapsed:.0f}s)\n{output}"
    return live_repo, run_id, output


def test_both_groups_completed(completed_run):
    repo, run_id, output = completed_run
    state = json.loads(RunPaths(repo, run_id).state_path.read_text())
    assert state["groups"]["g1"]["state"] == "completed", output
    assert state["groups"]["g2"]["state"] == "completed", output


def test_run_group_committed_its_declared_output(completed_run):
    repo, run_id, _output = completed_run
    log = _git(repo, "log", "--oneline", f"orchestrator/run-{run_id}")
    assert f"merge({run_id}): g1" in log
    assert "hello" in _git(repo, "show", f"orchestrator/run-{run_id}:run-output.txt")


def test_code_group_committed_its_change(completed_run):
    repo, run_id, _output = completed_run
    assert "farewell" in _git(repo, "show", f"orchestrator/run-{run_id}:greeting.py")


def test_artifact_manifest_has_one_run_and_one_code_entry(completed_run):
    repo, run_id, output = completed_run
    manifest = json.loads(RunPaths(repo, run_id).artifact_manifest_path.read_text())
    by_group = {e["group_id"]: e for e in manifest["entries"].values()}
    assert by_group["g1"]["recipe"] == "run", output
    assert by_group["g2"]["recipe"] == "code", output


def test_code_groups_first_prompt_carried_the_run_entry(completed_run):
    """Reads the actual exported transcript — not the executor's internal
    prompt-building function — so this is a property of what the real coder
    session received, not of the code that assembled it."""
    repo, run_id, output = completed_run

    export_dir = repo / ".orchestrator" / "export-out"
    exit_code = main(["export", "--repo", str(repo), run_id, "--out", str(export_dir)])
    assert exit_code == 0

    ingest = json.loads((export_dir / "ingest.json").read_text())
    g2 = next(g for g in ingest["groups"] if g["id"] == "g2")
    coder_sessions = [s for s in g2["sessions"] if s["role"] == "coder"]
    assert coder_sessions, output
    first_coder = min(coder_sessions, key=lambda s: s.get("generation", 1))
    events_path = export_dir / first_coder["events_path"]
    with gzip.open(events_path, "rt") as fh:
        events = [json.loads(line) for line in fh]
    first_user = next(e for e in events if e["role"] == "user")
    assert "## Upstream artifacts" in first_user["text"], first_user["text"][:2000]
    assert "group g1" in first_user["text"], first_user["text"][:2000]


def test_ingest_json_carries_both_artifact_manifest_entries(completed_run):
    repo, run_id, _output = completed_run
    export_dir = repo / ".orchestrator" / "export-out"
    if not (export_dir / "ingest.json").is_file():
        exit_code = main(["export", "--repo", str(repo), run_id, "--out", str(export_dir)])
        assert exit_code == 0
    ingest = json.loads((export_dir / "ingest.json").read_text())
    assert ingest.get("artifact_manifest") is not None
    recipes = {e["recipe"] for e in ingest["artifact_manifest"]}
    assert recipes == {"run", "code"}


# --------------------------------------------------------------------------
# Optional: the triage path. Run only when the triage path changed or looks
# hard, since it is a second whole run. Structural checks only — never assert
# on diagnosis wording (the model's own words are not this test's business).
# --------------------------------------------------------------------------


@pytest.fixture(scope="session")
def triage_repo(tmp_path_factory) -> Path:
    return _build_repo(tmp_path_factory, commands_exit="3")


def test_triage_runs_once_and_group_fails(triage_repo):
    run_id = "run-recipe-triage1"
    started = time.time()
    # A run whose only group FAILED exits 1.
    output = _run_main(triage_repo, run_id, expected_exit=1)
    elapsed = time.time() - started
    assert elapsed < RUN_TIMEOUT_S, f"the run did not terminate ({elapsed:.0f}s)\n{output}"

    paths = RunPaths(triage_repo, run_id)
    state = json.loads(paths.state_path.read_text())
    assert state["groups"]["g1"]["state"] == "failed", output

    calls_path = paths.run_dir / "llm" / "calls.json"
    assert calls_path.is_file(), output
    calls = json.loads(calls_path.read_text())["calls"]
    triage_calls = [c for c in calls if c.get("gen_ai.operation.name") == "run_triage"]
    assert len(triage_calls) == 1, triage_calls

    call = triage_calls[0]
    assert call["status"]["code"] == "ok", call
    raw_file = paths.run_dir / "llm" / call["raw_file"]
    response = json.loads(raw_file.read_text())
    assert response["verdict"] in ("work_failure", "needs_decision"), response
    assert isinstance(response["diagnosis"], str) and response["diagnosis"], response

    failure = state["groups"]["g1"]["failure"] or ""
    assert response["diagnosis"] in failure, failure
