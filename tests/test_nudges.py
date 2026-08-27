"""U16 tests: nudge escalation puts all recovery cost on the bad path.

Nudge 1 quotes the report contract verbatim plus the verification ids and the
parse error; nudge 2 strips the task away and hands back a filled-in skeleton.
A round that reports cleanly the first time pays for none of it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from orchestrator.execution.prompting import (
    render_coder_nudge_contract,
    render_coder_nudge_skeleton,
    render_reviewer_nudge_contract,
    render_reviewer_nudge_skeleton,
)
from orchestrator.execution.sessions import ReportError, SessionRunner, nudge_until_report
from orchestrator.model import CoderReport, ReviewerVerdict

FAKE_CLAUDE = Path(__file__).parent / "fake_claude.py"


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def make_runner(fake_home: Path) -> SessionRunner:
    return SessionRunner(
        claude_bin=[sys.executable, str(FAKE_CLAUDE)],
        env={"FAKE_CLAUDE_HOME": str(fake_home)},
        transcript_root=fake_home / "projects",
    )


def script(fake_home: Path, *entries: dict) -> None:
    import json

    with (fake_home / "script.jsonl").open("a") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


def calls(fake_home: Path) -> list[dict]:
    import json

    path = fake_home / "calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def report_block(status: str = "completed", **overrides) -> str:
    import json

    body = {
        "status": status,
        "summary": "did the work",
        "verification_results": [],
        "surprises": [],
        **overrides,
    }
    return f'prose first\n\n<run-report status="{status}">\n{json.dumps(body)}\n</run-report>'


# ---------------------------------------------------------------- happy path


def test_happy_path_sends_no_nudge_and_adds_no_turn(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"result": report_block("completed")})
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    report, final = nudge_until_report(
        runner, first, CoderReport, cwd=tmp_path, verification_ids=["v1", "v2"]
    )
    assert report.status == "completed"
    # base + fork only — no resume calls were made
    assert len(calls(fake_home)) == 2
    assert final.session_id == first.session_id


# ---------------------------------------------------------------- nudge 1


def test_nudge_one_carries_verbatim_contract_and_parse_error():
    text = render_coder_nudge_contract("no <run-report> block in the final message", ["v1", "v2"])
    assert "End your final message with EXACTLY ONE report block" in text
    assert "no <run-report> block in the final message" in text


def test_nudge_one_lists_every_verification_id():
    text = render_coder_nudge_contract("bad json", ["v1", "v2", "v3"])
    assert "- v1" in text
    assert "- v2" in text
    assert "- v3" in text


def test_first_nudge_sent_carries_contract_and_ids(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"result": "forgot the block"}, {"result": report_block("completed")})
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    nudge_until_report(runner, first, CoderReport, cwd=tmp_path, verification_ids=["v1", "v2"])
    resumes = [
        c for c in calls(fake_home) if "--resume" in c["argv"] and "--fork-session" not in c["argv"]
    ]
    assert len(resumes) == 1
    prompt = resumes[0]["prompt"]
    assert "End your final message with EXACTLY ONE report block" in prompt
    assert "no <run-report> block" in prompt
    assert "- v1" in prompt and "- v2" in prompt


# ---------------------------------------------------------------- nudge 2


def test_nudge_two_differs_from_nudge_one_and_has_skeleton():
    contract = render_coder_nudge_contract("bad json", ["v1", "v2"])
    skeleton = render_coder_nudge_skeleton(["v1", "v2"])
    assert skeleton != contract
    assert 'status="completed"' in skeleton
    assert '"item_id": "v1"' in skeleton
    assert '"item_id": "v2"' in skeleton


def test_second_nudge_sent_is_a_filled_in_skeleton(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(
        fake_home,
        {"result": "forgot the block"},
        {"result": "still forgot it"},
        {"result": report_block("completed")},
    )
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    report, _ = nudge_until_report(
        runner, first, CoderReport, cwd=tmp_path, max_nudges=2, verification_ids=["v1", "v2"]
    )
    assert report.status == "completed"
    resumes = [
        c for c in calls(fake_home) if "--resume" in c["argv"] and "--fork-session" not in c["argv"]
    ]
    assert len(resumes) == 2
    first_nudge, second_nudge = (r["prompt"] for r in resumes)
    assert second_nudge != first_nudge
    assert 'status="completed"' in second_nudge
    assert '"item_id": "v1"' in second_nudge
    assert '"item_id": "v2"' in second_nudge


def test_worker_completing_skeleton_recovers_the_round(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(
        fake_home,
        {"result": "no block at all"},
        {"result": "still nothing"},
        {
            "result": report_block(
                "completed", verification_results=[{"item_id": "v1", "status": "pass", "notes": ""}]
            )
        },
    )
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    report, final = nudge_until_report(
        runner, first, CoderReport, cwd=tmp_path, max_nudges=2, verification_ids=["v1"]
    )
    assert report.status == "completed"
    assert final.session_id == first.session_id


# ---------------------------------------------------------------- three strikes


def test_three_consecutive_failures_still_fail_the_round(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(fake_home, {"result": "no block 1"}, {"result": "no block 2"}, {"result": "no block 3"})
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    with pytest.raises(ReportError, match="after 2 re-nudges"):
        nudge_until_report(
            runner, first, CoderReport, cwd=tmp_path, max_nudges=2, verification_ids=["v1"]
        )
    # base + fork + exactly 2 nudge resumes — no more, no fewer
    assert len(calls(fake_home)) == 4


# ---------------------------------------------------------------- reviewer rounds


def test_reviewer_nudge_one_and_two_differ():
    contract = render_reviewer_nudge_contract("bad json")
    skeleton = render_reviewer_nudge_skeleton()
    assert contract != skeleton
    assert "EXACTLY ONE verdict block" in contract
    assert 'status="approved"' in skeleton


def test_reviewer_round_nudges_and_recovers(fake_home, tmp_path):
    runner = make_runner(fake_home)
    base = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    script(
        fake_home,
        {"result": "no verdict"},
        {"result": '<run-report status="approved">{"status": "approved"}</run-report>'},
    )
    first = runner.start_fork(base_id=base.session_id, prompt="go", name="n", cwd=tmp_path)
    verdict, _ = nudge_until_report(runner, first, ReviewerVerdict, cwd=tmp_path, max_nudges=2)
    assert verdict.status == "approved"
    resumes = [
        c for c in calls(fake_home) if "--resume" in c["argv"] and "--fork-session" not in c["argv"]
    ]
    assert len(resumes) == 1
    assert "EXACTLY ONE verdict block" in resumes[0]["prompt"]
