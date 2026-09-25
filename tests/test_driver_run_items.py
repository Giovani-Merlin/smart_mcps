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
    is_sandbox_safe_driver_run,
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
    # r20260924: the coder *may* attempt a sandbox-safe driver item; only a
    # nested `claude` (or a write outside the sandbox) is off limits.
    assert "DRIVER-RUN" in driver_line
    assert "nested `claude`" in driver_line
    assert "`pass`" in driver_line
    assert "`skipped`" in driver_line
    assert "`driver-run`" in driver_line
    assert "do NOT run" not in driver_line


def test_marker_with_a_comma_is_recognised():
    # The plan wrote "Run (driver), optional — …"; the colon-only regex missed it.
    assert is_driver_run("Run (driver), optional — run when the triage path changed")


def test_an_item_built_without_the_flag_derives_it_from_its_description():
    # The mid-run speccer rewrite builds VerificationItems from JSON with no
    # driver_run key (r20260924-134934, g8).
    item = VerificationItem.model_validate({"id": "g8-v5", "description": LIVE_ITEM})
    assert item.driver_run is True
    assert VerificationItem(id="g8-v1", description="unit tests pass").driver_run is False


# ------------------------------------------------ Run (driver, sandbox-safe):
# r20260925-101742: a coder skipped a driver item the plan had already checked
# was sandbox-safe, with a bare `driver-run` note, because the prompt only
# *permitted* the attempt. The marker makes the attempt owed.

SANDBOX_SAFE_ITEM = (
    "The CLI's status output names the phase. "
    "Run (driver, sandbox-safe): `uv run pytest tests/test_cli.py -q` Pass: green."
)


def test_sandbox_safe_marker_sets_both_flags_case_and_space_insensitively():
    for text in (
        SANDBOX_SAFE_ITEM,
        "Run(driver,sandbox-safe):x",
        "run ( DRIVER , Sandbox Safe ) : x",
        "Run (driver, sandbox-safe), optional — x",
    ):
        assert is_driver_run(text), text
        assert is_sandbox_safe_driver_run(text), text
        item = VerificationItem(id="g1-1", description=text)
        assert item.driver_run is True and item.sandbox_safe is True
    plain = VerificationItem(id="g1-2", description=LIVE_ITEM)
    assert plain.driver_run is True and plain.sandbox_safe is False
    assert not is_sandbox_safe_driver_run("Run: uv run pytest -q")
    # The flag alone implies driver_run, so no reader has to check both.
    assert VerificationItem(id="g1-3", description="x", sandbox_safe=True).driver_run is True


def test_the_coder_prompt_says_a_sandbox_safe_driver_item_must_be_attempted():
    group = Group(
        id="g4",
        name="cli-surfaces",
        summary="s",
        spec="do the thing",
        difficulty=0.2,
        intensity=ReviewIntensity.SELF_VERIFY,
        files=["orchestrator/cli.py"],
        verification=[
            VerificationItem(id="g4-5", description=LIVE_ITEM),
            VerificationItem(id="g4-6", description=SANDBOX_SAFE_ITEM),
        ],
    )
    prompt = render_coder_prompt("r1", group)
    safe_line = next(line for line in prompt.splitlines() if "[g4-6]" in line)
    assert "DRIVER-RUN (sandbox-safe)" in safe_line
    assert "MUST attempt" in safe_line
    assert "a bare `driver-run` note is not" in safe_line
    assert "never holds your report back" in safe_line
    # The plain driver item keeps its "may attempt" wording.
    plain_line = next(line for line in prompt.splitlines() if "[g4-5]" in line)
    assert "MUST attempt" not in plain_line
    assert "run it only if" in plain_line


def test_a_sandbox_safe_item_still_never_holds_the_gate():
    items = [
        VerificationItem(id="g4-1", description="unit tests pass"),
        VerificationItem(id="g4-6", description=SANDBOX_SAFE_ITEM),
    ]
    results = [VerificationResult(item_id="g4-1", status="pass", notes="")]
    assert unmet_required_verification(items, results) == []
    skipped = results + [VerificationResult(item_id="g4-6", status="skipped", notes="driver-run")]
    assert unmet_required_verification(items, skipped) == []
    failed = results + [VerificationResult(item_id="g4-6", status="fail", notes="1 failed")]
    assert unmet_required_verification(items, failed) == []
