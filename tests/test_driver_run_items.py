"""A verification item the *driver* must run (plan marker `Run (driver):`).

r20260916-113121: g4 carried the live-tier item `uv run pytest -m llm`. A
worker is Landlock-confined to its own worktree and its own
`~/.claude/projects/<slug>`, so the nested `claude` that test spawns cannot
write its transcript — and that confinement is what keeps a worker out of every
other session's `memory/`. The coder spent a retirement and an operator
question discovering this. These tests pin the marker's three effects.
"""

from __future__ import annotations

from orchestrator.execution.prompting import render_coder_prompt
from orchestrator.model import (
    Group,
    ReviewIntensity,
    VerificationItem,
    VerificationResult,
    is_driver_run,
    unmet_required_verification,
)

LIVE_ITEM = (
    "The live test passes against the installed CLI. "
    "Run (driver): `uv run pytest tests/test_liveness_live.py -q -m llm` Pass: green."
)


def test_marker_is_recognised_case_and_space_insensitively():
    assert is_driver_run(LIVE_ITEM)
    assert is_driver_run("Run(driver):x") and is_driver_run("run (DRIVER) : x")
    assert not is_driver_run("Run: uv run pytest -q")
    assert not is_driver_run("the driver runs this")


def test_a_driver_run_item_never_holds_the_verification_gate():
    items = [
        VerificationItem(id="g4-1", description="unit tests pass"),
        VerificationItem(id="g4-5", description=LIVE_ITEM, driver_run=True),
    ]
    results = [
        VerificationResult(item_id="g4-1", status="pass", notes=""),
        VerificationResult(item_id="g4-5", status="skipped", notes="driver-run"),
    ]
    assert unmet_required_verification(items, results) == []
    # Absent entirely is fine too — the coder was told not to run it.
    assert unmet_required_verification(items, results[:1]) == []
    # The ordinary item still gates.
    assert unmet_required_verification(items, results[1:]) == [
        "[g4-1] no verification result reported: unit tests pass"
    ]


def test_the_coder_prompt_tells_the_coder_not_to_run_it():
    group = Group(
        id="g4",
        name="cli-surfaces",
        summary="s",
        spec="do the thing",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["orchestrator/cli.py"],
        verification=[
            VerificationItem(id="g4-1", description="unit tests pass"),
            VerificationItem(id="g4-5", description=LIVE_ITEM, driver_run=True),
        ],
    )
    prompt = render_coder_prompt("r1", group)

    assert "[g4-1] unit tests pass" in prompt
    driver_line = next(line for line in prompt.splitlines() if "[g4-5]" in line)
    assert "DRIVER-RUN: do NOT run this one" in driver_line
    assert "`driver-run`" in driver_line
