"""The ``run`` recipe's pure helpers: matching a verification item's ``Run:``
to a declared command, and the synthetic report's shape."""

from __future__ import annotations

from orchestrator.recipes.run import RunCommand, RunVerificationReport, run_command_for_item

COMMANDS = [
    RunCommand(cmd="uv run python bench.py --n 5", wall_clock_min=20),
    RunCommand(cmd="uv run pytest tests/ -q", wall_clock_min=5, cwd="web"),
]


def test_run_command_for_item_matches_backticked_and_bare_run_text():
    assert (
        run_command_for_item(
            "Bench is fast. Run: `uv run python bench.py --n 5` Pass: < 1s.", COMMANDS
        )
        is COMMANDS[0]
    )
    assert (
        run_command_for_item("Tests pass. Run: uv run pytest tests/ -q Pass: green.", COMMANDS)
        is COMMANDS[1]
    )
    # No Pass: clause — the command runs to the end of the text.
    assert run_command_for_item("Run: uv run pytest tests/ -q", COMMANDS) is COMMANDS[1]


def test_run_command_for_item_normalises_whitespace_and_ignores_the_marker_parenthetical():
    assert (
        run_command_for_item("Run (driver):   `uv   run python bench.py  --n 5`", COMMANDS)
        is COMMANDS[0]
    )
    assert run_command_for_item("run: `uv run pytest\n  tests/ -q`", COMMANDS) is COMMANDS[1]


def test_run_command_for_item_is_none_for_a_missing_or_foreign_command():
    assert run_command_for_item("The output exists.", COMMANDS) is None
    assert run_command_for_item("Run: `cd web && uv run pytest tests/ -q`", COMMANDS) is None
    assert run_command_for_item("Run: `uv run python bench.py`", COMMANDS) is None
    assert run_command_for_item("Run: ``", COMMANDS) is None


def test_run_verification_report_round_trips_with_no_extra_keys():
    report = RunVerificationReport(summary="run recipe: 1 command(s)")
    payload = report.model_dump(mode="json")
    assert payload == {
        "status": "completed",
        "source": "run_recipe",
        "summary": "run recipe: 1 command(s)",
        "verification_results": [],
    }
