"""U4: every `SessionRunner` call can carry a recipe's extra allowed tools.

Zero live CLI calls — runs against `tests/fake_claude.py`, same harness as
`tests/test_sessions.py`.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from orchestrator.execution.scheduler import GroupState
from orchestrator.recipes.code import CODE_RECIPE
from tests.test_review_loop import Harness, StubRunner, coder_report, make_group, verdict
from tests.test_sessions import calls, fake_home, make_runner  # noqa: F401


def test_start_worker_appends_extra_allowed_tools_exactly_once(fake_home, tmp_path):
    runner = make_runner(fake_home, allowed_tools=("Read", "Grep"))
    runner.start_worker(
        base_context="",
        prompt="go",
        name="r1-g1-coder-g1",
        cwd=tmp_path,
        extra_allowed_tools=("WebSearch",),
    )
    call = calls(fake_home)[-1]
    argv = call["argv"]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed.count("WebSearch") == 1
    assert "WebSearch" in allowed.split(",")


def test_start_worker_with_empty_extra_allowed_matches_today(fake_home, tmp_path):
    """`code` declares no extra tools — the argv must be byte-identical to a
    call made with no `extra_allowed_tools` argument at all."""
    runner = make_runner(fake_home, allowed_tools=("Read", "Grep"))
    runner.start_worker(base_context="", prompt="go", name="r1-g1-coder-g1", cwd=tmp_path)
    baseline = calls(fake_home)[-1]["argv"]

    runner.start_worker(
        base_context="",
        prompt="go",
        name="r1-g1-coder-g1",
        cwd=tmp_path,
        extra_allowed_tools=(),
    )
    with_empty = calls(fake_home)[-1]["argv"]

    # Session ids differ (fresh uuid each call) — compare everything else.
    def strip_session_id(argv: list[str]) -> list[str]:
        out = list(argv)
        out[out.index("--session-id") + 1] = "<sid>"
        return out

    assert strip_session_id(baseline) == strip_session_id(with_empty)


def test_start_worker_honours_extra_allowed_tools_with_no_base_allowlist(fake_home, tmp_path):
    """A recipe's tools must be honoured even under a config with no allowlist
    at all — the base allowlist being empty must not swallow the recipe's own
    grant."""
    runner = make_runner(fake_home)  # no allowed_tools configured
    runner.start_worker(
        base_context="",
        prompt="go",
        name="r1-g1-coder-g1",
        cwd=tmp_path,
        extra_allowed_tools=("WebSearch",),
    )
    call = calls(fake_home)[-1]
    argv = call["argv"]
    assert "--allowedTools" in argv
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "WebSearch" in allowed.split(",")


def test_resume_appends_extra_allowed_tools(fake_home, tmp_path):
    runner = make_runner(fake_home, allowed_tools=("Read",))
    base = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    runner.resume(
        session_id=base.session_id,
        prompt="continue",
        cwd=tmp_path,
        extra_allowed_tools=("WebFetch",),
    )
    call = calls(fake_home)[-1]
    argv = call["argv"]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "WebFetch" in allowed.split(",")


def test_start_fork_appends_extra_allowed_tools(fake_home, tmp_path):
    runner = make_runner(fake_home, allowed_tools=("Read",))
    base = runner.start_base(run_id="run1", base_context="ctx", cwd=tmp_path)
    runner.start_fork(
        base_id=base.session_id,
        prompt="go",
        name="r1-g1-coder-g1",
        cwd=tmp_path,
        extra_allowed_tools=("Bash(smart-mcps-perplexity *)",),
    )
    call = calls(fake_home)[-1]
    argv = call["argv"]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert "Bash(smart-mcps-perplexity *)" in allowed.split(",")


def test_extra_allowed_tools_are_deduplicated_against_base(fake_home, tmp_path):
    runner = make_runner(fake_home, allowed_tools=("Read", "WebSearch"))
    runner.start_worker(
        base_context="",
        prompt="go",
        name="r1-g1-coder-g1",
        cwd=tmp_path,
        extra_allowed_tools=("WebSearch",),
    )
    call = calls(fake_home)[-1]
    argv = call["argv"]
    allowed = argv[argv.index("--allowedTools") + 1]
    assert allowed.split(",").count("WebSearch") == 1


@pytest.mark.asyncio
async def test_coder_launch_carries_the_groups_recipe_extra_allowed_tools(tmp_path, monkeypatch):
    """A group whose recipe declares extra tools must have every coder and
    reviewer session call (launch, resume) carry them — verified through the
    real `GenerationLoop`/`ReviewerRound`, not just `SessionRunner` directly."""
    recipe_with_tools = replace(CODE_RECIPE, extra_allowed_tools=("WebSearch",))
    monkeypatch.setattr("orchestrator.recipes.get_recipe", lambda name: recipe_with_tools)
    runner = StubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix x"]), verdict("approved")],
        }
    )
    harness = Harness(tmp_path, runner)
    state = await harness.run(make_group())
    assert state == GroupState.COMPLETED

    coder_sid = runner.session_ids["r1-g1-coder-g1"]
    reviewer_sid = runner.session_ids["r1-g1-reviewer-g1"]
    assert runner.extra_allowed_tools["r1-g1-coder-g1"] == ("WebSearch",)
    assert runner.extra_allowed_tools["r1-g1-reviewer-g1"] == ("WebSearch",)
    # the changes_required round warm-resumes both the coder (revision prompt)
    # and the reviewer (re-review prompt) — both resumes must carry it too.
    assert runner.extra_allowed_tools[coder_sid] == ("WebSearch",)
    assert runner.extra_allowed_tools[reviewer_sid] == ("WebSearch",)
