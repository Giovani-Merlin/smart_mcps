"""The auth-refresh ladder (plan U4): rungs (a)+(b) in `AuthLadder`, rung (c) on
`UsageLimitGate` (reused with a health `probe` instead of a fixed deadline).

Rungs (a)/(b) are pure — a fabricated `.credentials.json`, an injected
`refresh` callable, an injected clock — so nothing here touches the network or
a real token. The sessions-level tests confirm a 401 never escapes as a bare
`SessionError`: it is caught, ladder-recovered or paused, and the call is
replayed, exactly mirroring how `UsageLimit` already behaves.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestrator.config import UsageLimitConfig
from orchestrator.execution.auth import (
    AuthLadder,
    CredentialStatus,
    is_auth_error,
    read_credential_status,
)
from orchestrator.execution.ratelimit import UsageLimitGate
from orchestrator.execution.sessions import AuthExpired, SessionError

from tests.test_ratelimit import FakeClock
from tests.test_sessions import calls, make_runner, script

START = datetime(2026, 8, 27, 9, tzinfo=UTC)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "fake-claude"
    (home / "sessions").mkdir(parents=True)
    return home


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _write_creds(
    path: Path,
    *,
    expires_at_ms: int | None,
    refresh_token_expires_at_ms: int | None,
) -> None:
    oauth: dict = {}
    if expires_at_ms is not None:
        oauth["expiresAt"] = expires_at_ms
    if refresh_token_expires_at_ms is not None:
        oauth["refreshTokenExpiresAt"] = refresh_token_expires_at_ms
    path.write_text(json.dumps({"claudeAiOauth": oauth, "organizationUuid": "org-1"}))


# --------------------------------------------------------------- is_auth_error


def test_is_auth_error_matches_the_observed_wording():
    assert is_auth_error("401 Unauthorized: Re-authenticate to continue")
    assert is_auth_error("claude exited 1: 401 · please re-authenticate to continue")


def test_is_auth_error_does_not_match_a_usage_limit_or_a_plain_failure():
    assert not is_auth_error("Claude AI usage limit reached|1700000000")
    assert not is_auth_error("claude exited 1: Segmentation fault")
    assert not is_auth_error("")


# ------------------------------------------------------------ credential reads


def test_read_credential_status_is_absent_for_a_missing_file(tmp_path):
    assert read_credential_status(tmp_path / "nope.json") is None


def test_read_credential_status_is_absent_for_malformed_json(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text("not json")
    assert read_credential_status(path) is None


def test_read_credential_status_reads_both_expiry_fields(tmp_path):
    path = tmp_path / "credentials.json"
    _write_creds(path, expires_at_ms=1000, refresh_token_expires_at_ms=2000)
    status = read_credential_status(path)
    assert status == CredentialStatus(expires_at_ms=1000, refresh_token_expires_at_ms=2000)


# ------------------------------------------------- [g25-stale-no-network] / [g25-healthy-no-refresh]


def test_a_past_expires_at_is_reported_stale_with_no_network_call(tmp_path):
    """[g25-stale-no-network]"""
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START - timedelta(hours=1)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )
    calls_made = {"refresh": 0}

    def refresh() -> bool:
        calls_made["refresh"] += 1
        return True

    ladder = AuthLadder(credentials_path=path, refresh=refresh, now_ms=lambda: _ms(START))
    assert ladder.is_stale() is True
    # `is_stale` alone (rung a) never touches the refresh callable.
    assert calls_made["refresh"] == 0


def test_a_future_expires_at_is_reported_healthy_and_no_refresh_is_attempted(tmp_path):
    """[g25-healthy-no-refresh]"""
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START + timedelta(hours=1)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )
    calls_made = {"refresh": 0}

    def refresh() -> bool:
        calls_made["refresh"] += 1
        return True

    ladder = AuthLadder(credentials_path=path, refresh=refresh, now_ms=lambda: _ms(START))
    assert ladder.is_stale() is False
    assert ladder.recover() is True
    assert calls_made["refresh"] == 0


# --------------------------------------------------------------- [g25-refresh-advances]


def test_recover_confirms_expires_at_advanced_after_a_successful_refresh(tmp_path):
    """[g25-refresh-advances]"""
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START - timedelta(minutes=5)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )

    def refresh() -> bool:
        # The real refresh path: the CLI call succeeds and rewrites the file
        # with a later expiresAt.
        _write_creds(
            path,
            expires_at_ms=_ms(START + timedelta(hours=8)),
            refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
        )
        return True

    lines: list[str] = []
    ladder = AuthLadder(
        credentials_path=path, refresh=refresh, now_ms=lambda: _ms(START), log=lines.append
    )
    assert ladder.recover() is True
    assert any("expiresAt advanced" in line for line in lines)


def test_recover_is_false_when_the_refresh_call_does_not_succeed(tmp_path):
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START - timedelta(minutes=5)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )
    ladder = AuthLadder(credentials_path=path, refresh=lambda: False, now_ms=lambda: _ms(START))
    assert ladder.recover() is False


def test_recover_is_false_when_the_refresh_runs_but_does_not_advance_expiry(tmp_path):
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START - timedelta(minutes=5)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )

    def refresh() -> bool:
        # Ran "successfully" but the file was not actually rewritten — the
        # honest failure mode this rung exists to catch.
        return True

    ladder = AuthLadder(credentials_path=path, refresh=refresh, now_ms=lambda: _ms(START))
    assert ladder.recover() is False


# ---------------------------------------------------------- [g25-refresh-token-expired]


def test_recover_skips_the_refresh_attempt_when_the_refresh_token_has_also_expired(tmp_path):
    """[g25-refresh-token-expired]"""
    path = tmp_path / "credentials.json"
    _write_creds(
        path,
        expires_at_ms=_ms(START - timedelta(hours=1)),
        refresh_token_expires_at_ms=_ms(START - timedelta(minutes=1)),
    )
    calls_made = {"refresh": 0}

    def refresh() -> bool:
        calls_made["refresh"] += 1
        return True

    lines: list[str] = []
    ladder = AuthLadder(
        credentials_path=path, refresh=refresh, now_ms=lambda: _ms(START), log=lines.append
    )
    assert ladder.recover() is False
    assert calls_made["refresh"] == 0
    assert any("refresh token has also expired" in line for line in lines)


# --------------------------------------------------------------------- gate (c)


def test_gate_arms_a_pause_directly_when_the_ladder_cannot_recover(tmp_path):
    """A `recover`-returning-False probe still lets the gate arm and hold —
    rung (c) taking over from a ladder that gave up at rung (b)."""
    clock = FakeClock(START)
    gate = UsageLimitGate(
        UsageLimitConfig(fallback_poll_s=30, skew_s=0, max_wait_s=90),
        now=clock.now,
        sleep=clock.sleep,
        probe=lambda: False,
        label="credential",
    )
    released = gate.pause("401 Unauthorized: Re-authenticate to continue")
    # max_wait_s bounds it, so the pause still ends, but never because the
    # probe reported healthy.
    assert released is True
    assert gate.pauses == 1
    assert gate.state() is not None
    assert gate.state().released_at is not None


# ------------------------------------------------- [g25-401-pauses-not-halts]


def test_a_401_pauses_via_the_gate_rather_than_raising_a_bare_session_error(fake_home, tmp_path):
    """[g25-401-pauses-not-halts]

    Scripted to 401 once, then succeed — the gate's probe reports healthy on
    its very first check, so the pause is short but still real: `pauses == 1`
    and no exception reaches the caller. A caller (the scheduler) that only
    ever sees `_call_with_retry` return normally never sets RUN_HALTED for
    this group.
    """
    creds = tmp_path / "credentials.json"
    _write_creds(
        creds,
        expires_at_ms=_ms(START - timedelta(minutes=1)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )
    # The ladder itself cannot fix it (refresh always fails) — every recovery
    # in this test happens through the gate's probe, so the pause path is
    # exercised, not the no-pause fast path.
    ladder = AuthLadder(credentials_path=creds, refresh=lambda: False, now_ms=lambda: _ms(START))

    clock = FakeClock(START)

    def probe() -> bool:
        # Simulate the operator's credential healing itself out of band
        # (a background refresh, a re-login) before the first probe fires.
        _write_creds(
            creds,
            expires_at_ms=_ms(START + timedelta(hours=8)),
            refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
        )
        return ladder.recover()

    auth_gate = UsageLimitGate(
        UsageLimitConfig(fallback_poll_s=30, skew_s=0),
        now=clock.now,
        sleep=clock.sleep,
        probe=probe,
        label="credential",
    )
    runner = make_runner(fake_home, auth_ladder=ladder, auth_gate=auth_gate)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "401 Unauthorized: Re-authenticate to continue"}),
        },
        {"result": "OK"},
    )
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "OK"
    assert auth_gate.pauses == 1
    spawns = [call for call in calls(fake_home) if "--print" in call["argv"]]
    assert len(spawns) == 2


def test_a_401_recovered_by_the_ladder_alone_never_arms_a_pause(fake_home, tmp_path):
    """When rungs (a)+(b) fix the token by themselves, rung (c) is never
    reached — no pause is armed at all."""
    creds = tmp_path / "credentials.json"
    _write_creds(
        creds,
        expires_at_ms=_ms(START - timedelta(minutes=1)),
        refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
    )

    def refresh() -> bool:
        _write_creds(
            creds,
            expires_at_ms=_ms(START + timedelta(hours=8)),
            refresh_token_expires_at_ms=_ms(START + timedelta(days=90)),
        )
        return True

    ladder = AuthLadder(credentials_path=creds, refresh=refresh, now_ms=lambda: _ms(START))
    auth_gate = UsageLimitGate(UsageLimitConfig(), probe=ladder.recover, label="credential")
    runner = make_runner(fake_home, auth_ladder=ladder, auth_gate=auth_gate)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "401 Unauthorized: Re-authenticate to continue"}),
        },
        {"result": "OK"},
    )
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "OK"
    assert auth_gate.pauses == 0


def test_no_auth_ladder_or_gate_configured_raises_the_pre_u4_bare_session_error(
    fake_home, tmp_path
):
    """Restores exactly today's behaviour when the auth ladder is not wired
    in: a 401 is still a `SessionError` (typed as `AuthExpired`), not silently
    swallowed."""
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "401 Unauthorized: Re-authenticate to continue"}),
        },
    )
    with pytest.raises(SessionError):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


def test_auth_expired_is_raised_typed_not_as_a_bare_session_error(fake_home, tmp_path):
    runner = make_runner(fake_home)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "401 Unauthorized: Re-authenticate to continue"}),
        },
    )
    with pytest.raises(AuthExpired):
        runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)


# ----------------------------------------------------------- [g25-pause-self-release]


def test_the_armed_pause_self_releases_once_a_later_probe_reports_healthy():
    """[g25-pause-self-release]

    The probe fails twice — the credential is still broken — and only
    succeeds on the third check, simulating a fix that lands while the run is
    sitting there. The gate must not release early and must not need a fresh
    401 to notice the fix: the very next scheduled probe clears it.
    """
    clock = FakeClock(START)
    probe_calls = {"n": 0}

    def probe() -> bool:
        probe_calls["n"] += 1
        return probe_calls["n"] >= 3

    gate = UsageLimitGate(
        UsageLimitConfig(fallback_poll_s=30, skew_s=0),
        now=clock.now,
        sleep=clock.sleep,
        probe=probe,
        label="credential",
    )
    released = gate.pause("401 Unauthorized: Re-authenticate to continue")
    assert released is True
    assert probe_calls["n"] == 3
    assert gate.pauses == 1
    state = gate.state()
    assert state is not None and state.released_at is not None


def test_paused_groups_resume_once_healthy_the_call_is_replayed(fake_home, tmp_path):
    """The sessions-level analogue of the gate test above: once the probe
    reports healthy, `_call_with_retry` replays the original call rather than
    surfacing anything to the caller — this is what "paused groups resume"
    means in practice, since a group's own retry loop lives here."""
    clock = FakeClock(START)
    probe_calls = {"n": 0}

    def probe() -> bool:
        probe_calls["n"] += 1
        return probe_calls["n"] >= 2

    ladder = AuthLadder(
        credentials_path=tmp_path / "missing.json",  # unreadable -> recover() is always False
        refresh=lambda: False,
        now_ms=lambda: _ms(START),
    )
    auth_gate = UsageLimitGate(
        UsageLimitConfig(fallback_poll_s=15, skew_s=0),
        now=clock.now,
        sleep=clock.sleep,
        probe=probe,
        label="credential",
    )
    runner = make_runner(fake_home, auth_ladder=ladder, auth_gate=auth_gate)
    script(
        fake_home,
        {
            "exit_code": 1,
            "stderr": "",
            "stdout": json.dumps({"result": "401 Unauthorized: Re-authenticate to continue"}),
        },
        {"result": "OK"},
    )
    result = runner.start_base(run_id="r1", base_context="ctx", cwd=tmp_path)
    assert result.text == "OK"
    assert probe_calls["n"] == 2
    assert auth_gate.pauses == 1
