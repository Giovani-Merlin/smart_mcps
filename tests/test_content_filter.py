"""U10 tests: a content-filter error is its own failure kind (plan U10).

``-k classify``/``-k phrase`` exercise ``SessionRunner._invoke`` against
``tests/fake_claude.py`` (zero live CLI calls, zero tokens, plan R24) —
matching ``tests/test_sessions.py``'s convention. ``-k hitl``/``-k
autonomous`` exercise the review loop's routing in-process against
``StubRunner`` (see ``tests/test_review_loop.py``) — matching
``tests/test_suspend_cure.py``'s convention for a `_worker_call`-routed
exception.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from orchestrator.execution.escalation import EscalationPolicy
from orchestrator.execution.sessions import (
    ContentFiltered,
    SessionRunner,
    UsageLimit,
    _CONTENT_FILTER_RE,
    is_usage_limit,
)

from test_review_loop import (
    Harness,
    StubBroker,
    StubRunner,
    answer,
    coder_report,
    make_group,
    retry,
    verdict,
)
from orchestrator.execution.scheduler import GroupState
from orchestrator.model import EscalationKind

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"

#: The wording actually seen on a real run (learning_podcast run
#: r20260907-123644 g1) — copied verbatim, not paraphrased, so the phrase
#: oracle test is evidence rather than a guess about the CLI's wording.
_REAL_CONTENT_FILTER_TEXT = "API Error: Output blocked by content filtering policy"


# ------------------------------------------------------------ classify (sessions.py)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path, **kwargs) -> SessionRunner:
    env = {"FAKE_CLAUDE_HOME": str(fake_home), **kwargs.pop("env", {})}
    kwargs.setdefault("transcript_root", fake_home / "projects")
    return SessionRunner(claude_bin=[sys.executable, str(FAKE_CLAUDE)], env=env, **kwargs)


def script(fake_home: Path, *entries: dict) -> None:
    with (fake_home / "script.jsonl").open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def test_classify_is_error_envelope_raises_content_filtered_with_last_assistant_text(
    fake_home, tmp_path
):
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "is_error": True,
            "result": "API Error: Output blocked by content filtering policy",
            "turns": [{"text": "here is the diff I was about to apply"}],
        },
    )
    with pytest.raises(ContentFiltered) as caught:
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert caught.value.last_assistant_text == "here is the diff I was about to apply"


def test_classify_nonzero_exit_also_raises_content_filtered(fake_home, tmp_path):
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps(
                {"result": "API Error: Output blocked by content filtering policy"}
            ),
            "turns": [{"text": "partial output before the filter fired"}],
        },
    )
    with pytest.raises(ContentFiltered) as caught:
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert caught.value.last_assistant_text == "partial output before the filter fired"


def test_classify_usage_limit_reached_still_raises_usage_limit_not_content_filtered(
    fake_home, tmp_path
):
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "usage limit reached|1700000000"}),
        },
    )
    with pytest.raises(UsageLimit):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


# ------------------------------------------------------------ phrase (sessions.py)


def test_content_filter_phrase_matches_the_real_run_text_not_usage_limit_text():
    assert _CONTENT_FILTER_RE.search(_REAL_CONTENT_FILTER_TEXT)
    assert not is_usage_limit(_REAL_CONTENT_FILTER_TEXT)
    assert not _CONTENT_FILTER_RE.search("Claude AI usage limit reached|1700000000")
    assert not _CONTENT_FILTER_RE.search("You've hit your session limit · resets 1pm")


# ------------------------------------------------------------ review-loop routing


class ContentFilteringStubRunner(StubRunner):
    """A ``StubRunner`` whose *Nth* launch or resume call raises
    ``ContentFiltered`` instead of returning — modelling the API's content
    filter having blocked that round — then behaves exactly like
    ``StubRunner`` on every other call. Mirrors
    ``test_suspend_cure.CuringStubRunner``'s shape for the same reason: this
    is a `_worker_call`-routed exception too, just one that must never be
    warm-resumed.
    """

    def __init__(
        self,
        fork_scripts,
        *,
        filter_on_launch_call: int | None = None,
        filter_on_resume_call: int | None = None,
        last_assistant_text: str = "here is what I was drafting",
    ):
        super().__init__(fork_scripts)
        self._filter_on_launch_call = filter_on_launch_call
        self._filter_on_resume_call = filter_on_resume_call
        self._last_assistant_text = last_assistant_text
        self._launch_calls = 0
        self._resume_calls = 0

    def _launch(self, prompt, name, session_id, on_turn):
        self._launch_calls += 1
        if self._launch_calls == self._filter_on_launch_call:
            raise ContentFiltered(
                "claude reported an error result: API Error: Output blocked by "
                "content filtering policy",
                self._last_assistant_text,
            )
        return super()._launch(prompt, name, session_id, on_turn)

    def resume(self, *, session_id, prompt, cwd, json_schema=None, on_turn=None):
        self._resume_calls += 1
        if self._resume_calls == self._filter_on_resume_call:
            raise ContentFiltered(
                "claude reported an error result: API Error: Output blocked by "
                "content filtering policy",
                self._last_assistant_text,
            )
        return super().resume(
            session_id=session_id, prompt=prompt, cwd=cwd, json_schema=json_schema, on_turn=on_turn
        )


def on_stuck(source: str = "workers_via_orchestrator") -> EscalationPolicy:
    return EscalationPolicy("on_stuck", source)


@pytest.mark.asyncio
async def test_content_filter_hitl_escalates_coder_blocked_quoting_last_message(tmp_path):
    """[g2-2] The first coder round raises ``ContentFiltered``: exactly one
    ``coder_blocked`` escalation is raised whose prompt quotes the last
    assistant text, and no ``resume`` call is ever made — a content-filtered
    session is never warm-resumed, unlike a ``SuspendCured`` one."""
    runner = ContentFilteringStubRunner(
        {
            "r1-g1-coder-g1": [],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        },
        filter_on_launch_call=1,
        last_assistant_text="drafting the auth patch now",
    )
    broker = StubBroker({EscalationKind.CODER_BLOCKED: retry("account issue resolved")})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    state = await harness.run(make_group())

    assert state == GroupState.COMPLETED
    assert [req.kind for req in broker.raised] == [EscalationKind.CODER_BLOCKED]
    prompt = broker.raised[0].prompt
    assert "content filter" in prompt
    assert "drafting the auth patch now" in prompt
    assert runner._resume_calls == 0  # never warm-resumed

    log = harness.store.paths.event_log_path.read_text()
    assert "ended (content filter)" in log


@pytest.mark.asyncio
async def test_content_filter_hitl_retry_relaunches_the_same_spec(tmp_path):
    """[g2-2] Answering ``retry`` relaunches on the same spec — no rewrite spent."""
    runner = ContentFilteringStubRunner(
        {
            "r1-g1-coder-g1": [],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        },
        filter_on_launch_call=1,
    )
    broker = StubBroker({EscalationKind.CODER_BLOCKED: retry("fixed: rephrased the prompt")})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    state = await harness.run(make_group())

    assert state == GroupState.COMPLETED
    assert harness.rewritten == []  # rewrite_spec never called
    assert harness.generations == [2]  # a fresh generation, same spec
    second_prompt = runner.prompts[runner.session_ids["r1-g1-coder-g2"]][0]
    assert "fixed: rephrased the prompt" in second_prompt
    assert "v2" not in second_prompt  # unchanged spec


@pytest.mark.asyncio
async def test_content_filter_hitl_answer_rewrites_with_the_operator_text(tmp_path):
    """[g2-2] Answering ``answer`` performs one rewrite whose surprises include
    both the content-filter context and the operator's own text."""
    runner = ContentFilteringStubRunner(
        {
            "r1-g1-coder-g1": [],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        },
        filter_on_launch_call=1,
        last_assistant_text="here is the plan I was drafting",
    )
    broker = StubBroker(
        {EscalationKind.CODER_BLOCKED: answer("avoid quoting the flagged phrase verbatim")}
    )
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    state = await harness.run(make_group())

    assert state == GroupState.COMPLETED
    assert len(harness.rewritten) == 1
    descriptions = [s.description for s in harness.rewritten[0]]
    assert any(
        "content filter" in d.lower() and "here is the plan I was drafting" in d
        for d in descriptions
    )
    assert any("avoid quoting the flagged phrase verbatim" in d for d in descriptions)


@pytest.mark.asyncio
async def test_content_filter_hitl_reviewer_round_takes_the_same_route(tmp_path):
    """[g2-2] A reviewer ``ContentFiltered`` (its first resume/re-review call)
    is escalated the same way as a coder one — never warm-resumed."""
    runner = ContentFilteringStubRunner(
        {
            "r1-g1-coder-g1": [coder_report(), coder_report()],
            "r1-g1-reviewer-g1": [verdict("changes_required", ["fix x"])],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        },
        filter_on_resume_call=2,  # 1st resume = coder revision (succeeds); 2nd = re-review
    )
    broker = StubBroker({EscalationKind.CODER_BLOCKED: retry("fixed")})
    harness = Harness(tmp_path, runner, broker=broker, policy=on_stuck())
    state = await harness.run(make_group())

    assert state == GroupState.COMPLETED
    assert [req.kind for req in broker.raised] == [EscalationKind.CODER_BLOCKED]
    assert harness.generations == [2]


@pytest.mark.asyncio
async def test_content_filter_autonomous_rewrites_and_never_interrupts(tmp_path):
    """[g2-3] HITL off (no broker/policy configured): the same failure performs
    one rewrite whose surprises name the content filter and quote the
    message — the group is never left interrupted, it completes."""
    runner = ContentFilteringStubRunner(
        {
            "r1-g1-coder-g1": [],
            "r1-g1-coder-g2": [coder_report()],
            "r1-g1-reviewer-g2": [verdict("approved")],
        },
        filter_on_launch_call=1,
        last_assistant_text="autonomous run's last assistant text",
    )
    harness = Harness(tmp_path, runner)  # no broker, no policy — autonomous
    state = await harness.run(make_group())

    assert state == GroupState.COMPLETED
    assert len(harness.rewritten) == 1
    descriptions = [s.description for s in harness.rewritten[0]]
    assert any(
        "content filter" in d.lower() and "autonomous run's last assistant text" in d
        for d in descriptions
    )
    assert harness.generations == [2]
