"""U6: don't partition against a moving index — poll the fingerprint until it
repeats across N consecutive reads, or fail loudly naming what was observed.
"""

import json

import pytest

from orchestrator.grouping.graphing import (
    CodegraphClient,
    GraphBuildError,
    await_index_quiescence,
)


def _client_with_query_sequence(tmp_path, responses):
    """A client whose bulk `query ""` result advances one step per call,
    holding at the last response once the sequence is exhausted."""
    state = {"i": 0}

    def runner(args):
        if args[0] == "status":
            return json.dumps({"read": state["i"]})
        if args[0] == "query":
            index = min(state["i"], len(responses) - 1)
            state["i"] += 1
            return json.dumps(responses[index])
        raise AssertionError(f"unexpected call: {args}")

    return CodegraphClient(repo_root=tmp_path, runner=runner)


def _no_sleep(_seconds: float) -> None:
    return None


class _FakeClock:
    """Deterministic ``now`` seam: advances by a fixed step every call so a
    timeout can be exercised without any real wall-clock wait."""

    def __init__(self, step: float = 1.0) -> None:
        self._t = 0.0
        self._step = step

    def __call__(self) -> float:
        self._t += self._step
        return self._t


class TestStableIndexReturnsAfterMinimumReads:
    def test_returns_after_exactly_min_stable_reads(self, tmp_path):
        symbol = [{"node": {"id": "function:a", "kind": "function"}}]
        calls = {"n": 0}

        def runner(args):
            if args[0] == "status":
                return json.dumps({"ok": True})
            if args[0] == "query":
                calls["n"] += 1
                return json.dumps(symbol)
            raise AssertionError(f"unexpected call: {args}")

        client = CodegraphClient(repo_root=tmp_path, runner=runner)
        fingerprint = await_index_quiescence(
            client,
            min_stable_reads=3,
            interval_s=0,
            timeout_s=5.0,
            sleep=_no_sleep,
        )
        assert fingerprint
        assert calls["n"] == 3

    def test_grouping_can_proceed_after_a_stable_read(self, tmp_path):
        """The handshake returns a real fingerprint string usable downstream —
        it does not raise, and it does not block forever."""
        client = _client_with_query_sequence(tmp_path, [[]])
        fingerprint = await_index_quiescence(
            client, min_stable_reads=3, interval_s=0, sleep=_no_sleep
        )
        assert isinstance(fingerprint, str)
        assert len(fingerprint) == 64


class TestMovingIndexTimesOut:
    def test_raises_graph_build_error_naming_distinct_fingerprints(self, tmp_path):
        # A distinct query result on every read: the fingerprint can never repeat.
        responses = [[{"node": {"id": f"function:{i}", "kind": "function"}}] for i in range(50)]
        client = _client_with_query_sequence(tmp_path, responses)
        clock = _FakeClock(step=1.0)
        with pytest.raises(GraphBuildError) as excinfo:
            await_index_quiescence(
                client,
                min_stable_reads=3,
                interval_s=0,
                timeout_s=3.0,
                sleep=_no_sleep,
                now=clock,
            )
        message = str(excinfo.value)
        assert "did not settle" in message
        # At least two distinct fingerprints must be named for the message to be
        # useful for attribution.
        assert message.count("'") >= 4 or "distinct" in message

    def test_never_calls_the_real_time_sleep_when_a_fake_is_given(self, tmp_path):
        """Regression guard: a caller-supplied ``sleep`` must be the only sleep
        seam used — the handshake itself must never fall back to a hardcoded
        wait that would make tests slow."""
        sleeps: list[float] = []
        responses = [[{"node": {"id": f"function:{i}"}}] for i in range(10)]
        client = _client_with_query_sequence(tmp_path, responses)
        clock = _FakeClock(step=1.0)
        with pytest.raises(GraphBuildError):
            await_index_quiescence(
                client,
                min_stable_reads=3,
                interval_s=0.25,
                timeout_s=2.0,
                sleep=sleeps.append,
                now=clock,
            )
        assert sleeps
        assert all(s == 0.25 for s in sleeps)


class TestSettlesAfterTransientChanges:
    def test_returns_the_settled_value_once_it_stops_changing(self, tmp_path):
        # First two reads differ, then it settles on the third value onward.
        responses = [
            [{"node": {"id": "function:a"}}],
            [{"node": {"id": "function:b"}}],
            [{"node": {"id": "function:c"}}],
            [{"node": {"id": "function:c"}}],
            [{"node": {"id": "function:c"}}],
        ]
        client = _client_with_query_sequence(tmp_path, responses)
        fingerprint = await_index_quiescence(
            client,
            min_stable_reads=3,
            interval_s=0,
            timeout_s=5.0,
            sleep=_no_sleep,
        )
        from orchestrator.grouping.graphing import index_fingerprint

        settled_export = client.logical_export()
        # The sequence holds at its last entry once exhausted, so re-reading now
        # reproduces the settled value for comparison.
        assert fingerprint == index_fingerprint(settled_export)


class TestDriftTraceRecordsEveryObservation:
    class _Recorder:
        def __init__(self) -> None:
            self.observations: list[tuple[str, dict]] = []

        def record_index_observation(self, fingerprint: str, status: dict) -> None:
            self.observations.append((fingerprint, status))

    def test_every_read_and_its_status_payload_is_recorded(self, tmp_path):
        responses = [
            [{"node": {"id": "function:a"}}],
            [{"node": {"id": "function:b"}}],
            [{"node": {"id": "function:b"}}],
            [{"node": {"id": "function:b"}}],
        ]
        client = _client_with_query_sequence(tmp_path, responses)
        recorder = self._Recorder()
        await_index_quiescence(
            client,
            min_stable_reads=3,
            interval_s=0,
            timeout_s=5.0,
            sleep=_no_sleep,
            recorder=recorder,
        )
        # 4 reads: a, b, b, b (streak of 3 b's completes on the 4th read).
        assert len(recorder.observations) == 4
        fingerprints = [fp for fp, _status in recorder.observations]
        assert len(set(fingerprints)) == 2  # one for "a", one (repeated) for "b"
        for _fp, status in recorder.observations:
            assert "read" in status  # the full status -j payload, not just a flag

    def test_recorder_is_optional(self, tmp_path):
        client = _client_with_query_sequence(tmp_path, [[]])
        # Must not raise when no recorder is given.
        await_index_quiescence(client, min_stable_reads=1, interval_s=0, sleep=_no_sleep)


class TestMinStableReadsValidation:
    def test_rejects_less_than_one(self, tmp_path):
        client = _client_with_query_sequence(tmp_path, [[]])
        with pytest.raises(ValueError):
            await_index_quiescence(client, min_stable_reads=0)
