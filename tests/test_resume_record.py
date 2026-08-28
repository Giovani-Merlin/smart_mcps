"""Plan U19 (resume-refreshes-record): a resumed run must describe the process
actually running, not the one that launched it.

F20 — the manifest kept the *first* launch's escalation/usage-limit config
forever, because the write that produces it sat inside the base-session
branch a resume never enters. F21 — ``released_at`` is written only by the
process that armed a usage-limit pause, so a process that died mid-pause left
an armed-forever record and the Observatory banner rendered a live resumed run
as paused.

These are exercised end to end against the real CLI and the scripted claude
stub, reusing the fixtures/helpers ``test_e2e_stub`` already built.
"""

from __future__ import annotations

import json

from orchestrator.cli import main
from orchestrator.execution.manifest import RunPaths, atomic_write_text
from orchestrator.model import ReviewIntensity
from orchestrator.observatory.runs import build_snapshot
from test_cli import make_group, write_run_artifacts
from test_e2e_stub import (  # noqa: F401 -- fixtures
    coder_entry,
    fake_home,
    manifest_of,
    name_of,
    repo,
    script_session,
    state_of,
    write_config,
)
from test_grouper_pipeline import StubLlm


def run_log(repo, run_id: str) -> str:
    return (repo / ".orchestrator" / "runs" / run_id / "logs" / "run.log").read_text()


def usage_limit_of(repo, run_id: str) -> dict:
    return json.loads((repo / ".orchestrator" / "runs" / run_id / "usage-limit.json").read_text())


def _interrupted_run(repo, fake_home, run_id: str) -> None:
    """A two-group run whose second group is left INTERRUPTED (an envelope
    failure at fork), so there is something for ``resume`` to do."""
    groups = [
        make_group("g1", intensity=ReviewIntensity.SELF_VERIFY),
        make_group("g2", intensity=ReviewIntensity.SELF_VERIFY),
    ]
    write_run_artifacts(repo, groups)
    write_config(repo, fake_home)
    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        coder_entry(files={"g1.out": "one\n"}, commit="g1: work"),
    )
    script_session(
        fake_home, name_of(run_id, "g2", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )
    exit_code = main(["run", "--repo", str(repo), "--run-id", run_id], llm_runner=StubLlm())
    assert exit_code == 2
    assert state_of(repo, run_id)["groups"]["g2"]["state"] == "interrupted"


# ------------------------------------------------------------- F20: manifest


def test_resume_with_hitl_flag_refreshes_manifest_escalation(repo, fake_home):
    run_id = "resc1"
    _interrupted_run(repo, fake_home, run_id)
    assert manifest_of(repo, run_id)["escalation"]["enabled"] is False

    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.out": "two\n"}, commit="g2: work"),
    )
    # `--escalation-timeout` keeps the interrupted group's own group_resolve
    # escalation from blocking this test forever waiting on an operator answer
    # nothing supplies — it falls back to `on_timeout` (autonomous, the
    # default) after a fraction of a second instead.
    exit_code = main(
        [
            "resume",
            run_id,
            "--repo",
            str(repo),
            "--hitl",
            "--intensity",
            "on_stuck",
            "--escalation-timeout",
            "0.2",
        ],
        llm_runner=StubLlm(),
    )
    assert exit_code == 0

    manifest = manifest_of(repo, run_id)
    assert manifest["escalation"]["enabled"] is True
    assert manifest["escalation"]["intensity"] == "on_stuck"

    # [g9-snapshot-matches-log] the manifest-derived snapshot must describe the
    # same config the resumed process's own run.log line reports having started
    # with — not the autonomous config the first launch recorded.
    log = run_log(repo, run_id)
    assert f"run {run_id} started with HITL: intensity=on_stuck" in log

    paths = RunPaths(repo, run_id)
    snapshot = build_snapshot(paths, "proj")
    assert snapshot.escalation is not None
    assert snapshot.escalation["enabled"] is True
    assert snapshot.escalation["intensity"] == "on_stuck"


# --------------------------------------------------------- F21: usage limit


def test_resume_stamps_released_at_on_a_stale_armed_usage_limit(repo, fake_home):
    run_id = "resc2"
    _interrupted_run(repo, fake_home, run_id)

    # Simulate the process that armed this pause having died mid-wait: an
    # armed record with no `released_at`, exactly what `UsageLimitGate` leaves
    # on disk if the process is killed before `_release_locked` runs.
    paths = RunPaths(repo, run_id)
    atomic_write_text(
        paths.usage_limit_path,
        json.dumps(
            {
                "armed_at": "2026-08-20T00:00:00+00:00",
                "detail": "usage limit reached",
                "attempt": 1,
                "reset_at": "2026-08-20T01:00:00+00:00",
                "wake_at": "2026-08-20T01:00:00+00:00",
                "released_at": None,
            }
        )
        + "\n",
    )

    # [g9-banner-hidden] before resume, the banner's own condition
    # (`usageLimit && !usageLimit.released_at`) would render this live run as
    # paused.
    snapshot_before = build_snapshot(paths, "proj")
    assert snapshot_before.usage_limit is not None
    assert snapshot_before.usage_limit.released_at is None

    script_session(
        fake_home,
        name_of(run_id, "g2", "coder"),
        coder_entry(files={"g2.out": "two\n"}, commit="g2: work"),
    )
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0

    # [g9-released-at-stamped]
    record = usage_limit_of(repo, run_id)
    assert record["released_at"] is not None
    # the pre-existing detail is left alone — only released_at is stamped.
    assert record["detail"] == "usage limit reached"

    # [g9-banner-hidden] the banner condition no longer holds for this run.
    snapshot_after = build_snapshot(paths, "proj")
    assert snapshot_after.usage_limit is not None
    assert snapshot_after.usage_limit.released_at is not None


def test_resume_that_hits_a_fresh_usage_limit_arms_a_new_record(repo, fake_home):
    """[g9-fresh-limit-arms] Stamping a stale record on resume must not disable
    the gate itself — a fresh limit encountered *during* the resumed run still
    arms, pauses and eventually releases exactly as it does on a first launch.
    """
    run_id = "resc3"
    groups = [make_group("g1", intensity=ReviewIntensity.SELF_VERIFY)]
    write_run_artifacts(repo, groups)
    write_config(
        repo,
        fake_home,
        extra="\n[session.usage_limit]\nfallback_poll_s = 0.05\nskew_s = 0\n",
    )
    script_session(
        fake_home, name_of(run_id, "g1", "coder"), {"exit_code": 1, "stderr": "worker crashed"}
    )
    exit_code = main(["run", "--repo", str(repo), "--run-id", run_id], llm_runner=StubLlm())
    assert exit_code == 2
    assert state_of(repo, run_id)["groups"]["g1"]["state"] == "interrupted"

    # No usage-limit.json exists yet — this run never hit a limit before now.
    paths = RunPaths(repo, run_id)
    assert not paths.usage_limit_path.exists()

    script_session(
        fake_home,
        name_of(run_id, "g1", "coder"),
        {"exit_code": 1, "stderr": "", "stdout": json.dumps({"result": "usage limit reached"})},
        coder_entry(files={"g1.out": "one\n"}, commit="g1: work"),
    )
    exit_code = main(["resume", run_id, "--repo", str(repo)], llm_runner=StubLlm())
    assert exit_code == 0
    assert state_of(repo, run_id)["groups"]["g1"]["state"] == "completed"

    log = run_log(repo, run_id)
    assert "usage limit: pausing this run" in log
    assert "usage limit: resuming after" in log

    record = usage_limit_of(repo, run_id)
    assert record["released_at"]
    assert record["detail"] == "usage limit reached"
