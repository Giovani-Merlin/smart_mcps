"""Thinking blocks, per-event usage, and incremental transcript fetching.

These three land together because they are one ask: the operator wants to read
*what the agent thought*, and that is precisely what ``RENDERABLE`` was filtering
out — the transcript viewer showed the tool calls and hid the reasoning that
chose them.

One thing the plan assumed does not hold, and the tests below pin it: no
transcript on this machine persists thinking *prose*. Every ``thinking`` block
is ``{"thinking": "", "signature": "…"}``, so enabling the block type recovers
where the agent thought and for how long, not what it thought. That is still
worth rendering — an omitted block claims the agent went straight from one tool
call to the next — so those events carry an explicit withheld marker instead of
disappearing.

Everything here runs against ``transcript-thinking.jsonl``, a real Claude Code
transcript sliced out of a session on disk rather than hand-written, because the
row shapes that matter (where ``usage`` sits, how a thinking block spells its
text, which rows carry a model) are exactly the details a fabricated fixture
gets subtly wrong. Two rows are appended to it: a ``redacted_thinking`` block,
which no transcript on this machine happened to contain, and a torn final line,
which is what a transcript caught mid-append actually looks like.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from orchestrator.execution.manifest import ManifestStore, RunPaths
from orchestrator.observatory.app import create_app
from orchestrator.observatory.transcripts import (
    REDACTED_PLACEHOLDER,
    RENDERABLE,
    WITHHELD_PLACEHOLDER,
    parse_transcript,
)
from tests.test_observatory_api import install_run, write_registry

REAL = Path(__file__).parent / "fixtures" / "observatory" / "transcript-thinking.jsonl"
RUN = "/api/projects/proj/runs/smoke1"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    repo.mkdir()
    install_run(repo, "smoke1")
    paths = RunPaths(repo, "smoke1")
    store = ManifestStore(paths)
    manifest = store.load()
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    for entry in manifest.groups.values():
        for session in entry.sessions:
            copy = transcripts / f"{session.session_id}.jsonl"
            copy.write_text(REAL.read_text())
            session.transcript_path = str(copy)
    store.save(manifest)
    return repo


@pytest.fixture
def client(tmp_path: Path, repo: Path) -> TestClient:
    registry = write_registry(tmp_path, [("proj", repo)])
    return TestClient(create_app(registry_path=registry, dist_dir=tmp_path / "no-dist"))


@pytest.fixture
def session_id(repo: Path) -> str:
    manifest = ManifestStore(RunPaths(repo, "smoke1")).load()
    return next(s.session_id for e in manifest.groups.values() for s in e.sessions)


def fetch(client: TestClient, session_id: str, **params) -> list[dict]:
    response = client.get(f"{RUN}/sessions/{session_id}/transcript", params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------- thinking


class TestThinkingSurvivesTheFilter:
    def test_the_filter_admits_both_thinking_block_types(self):
        assert {"thinking", "redacted_thinking"} <= RENDERABLE

    def test_thinking_reaches_the_api_response(self, client, session_id):
        thinking = [e for e in fetch(client, session_id) if e["kind"] == "thinking"]
        assert thinking, "the operator's whole ask is that these are visible"
        assert all(e["text"] and e["text"].strip() for e in thinking)
        assert all(e["role"] == "assistant" for e in thinking)

    def test_a_thinking_block_with_prose_renders_it(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "Two paths here; take the second."}
                        ]
                    },
                }
            )
            + "\n"
        )
        (event,) = parse_transcript(path)
        assert event.kind == "thinking"
        assert event.text == "Two paths here; take the second."
        assert event.thinking_withheld is False

    def test_a_signed_but_empty_thinking_block_says_so_instead_of_vanishing(
        self, client, session_id
    ):
        """Every thinking block in every transcript on this machine is signed
        and empty — the prose is not written to the file. Dropping them would
        claim the agent went straight from one tool call to the next, which is
        a different and false statement about what happened."""
        withheld = [e for e in fetch(client, session_id) if e.get("thinking_withheld")]
        assert withheld, "signed-but-empty thinking blocks were dropped"
        assert all(e["kind"] == "thinking" for e in withheld)
        assert all(e["text"] == WITHHELD_PLACEHOLDER for e in withheld)

    def test_redacted_thinking_renders_as_a_visible_gap(self, client, session_id):
        """A redacted block has no readable content. Dropping it would make the
        reasoning look continuous when a piece of it is missing."""
        redacted = [e for e in fetch(client, session_id) if e["kind"] == "redacted_thinking"]
        assert len(redacted) == 1
        assert redacted[0]["text"] == REDACTED_PLACEHOLDER

    def test_the_old_filter_dropped_a_third_of_this_real_transcript(self):
        """Quantifies what enabling the block types recovers. These are moments
        the drill-in used to render nothing at all for."""
        events = parse_transcript(REAL)
        recovered = [e for e in events if e.kind in ("thinking", "redacted_thinking")]
        assert len(recovered) >= 6
        assert len(recovered) > len(events) / 4

    def test_an_unsigned_empty_thinking_block_is_dropped(self, tmp_path):
        """No text and no signature is not evidence that anything was thought."""
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "thinking", "thinking": "   "}]},
                }
            )
            + "\n"
        )
        assert parse_transcript(path) == []

    def test_a_thinking_block_with_no_text_field_is_dropped(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "thinking"}]}}) + "\n"
        )
        assert parse_transcript(path) == []

    def test_the_torn_final_line_is_still_tolerated(self, client, session_id):
        """The fixture ends mid-write on purpose. Tolerance is the parser's
        contract and adding block types must not have cost it."""
        assert fetch(client, session_id)


# ---------------------------------------------------------------------- usage


class TestPerEventUsage:
    def test_assistant_events_carry_the_four_token_classes(self, client, session_id):
        events = fetch(client, session_id)
        with_usage = [e for e in events if e["usage"]]
        assert with_usage, "no assistant row's usage reached the client"
        usage = with_usage[0]["usage"]
        assert set(usage) == {
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }
        assert any(e["usage"]["cache_read_input_tokens"] > 0 for e in with_usage), (
            "cache reads are the class the manifest used to discard entirely"
        )

    def test_usage_is_attached_per_turn_not_summed(self, client, session_id):
        """Every block of one assistant turn carries that turn's usage. Summing
        across events is how a context reading came out 50x inflated once —
        keep the raw per-turn figures and let the client decide."""
        events = parse_transcript(REAL)
        by_ts: dict[str, set[tuple]] = {}
        for event in events:
            if event.usage is None or event.timestamp is None:
                continue
            by_ts.setdefault(event.timestamp, set()).add(
                (event.usage.input_tokens, event.usage.output_tokens)
            )
        assert by_ts, "the fixture carries no timestamped usage at all"
        assert all(len(values) == 1 for values in by_ts.values())

    def test_the_model_that_produced_the_turn_is_reported(self, client, session_id):
        events = fetch(client, session_id)
        assert any(e["model"] for e in events)

    def test_rows_without_usage_read_as_absent_rather_than_zero(self, client, session_id):
        """A transcript written before usage was recorded is normal, not broken.
        None and "all four are zero" are different claims."""
        events = fetch(client, session_id)
        assert any(e["usage"] is None for e in events)

    def test_a_malformed_usage_object_does_not_break_the_row(self, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [{"type": "text", "text": "hi"}],
                        "usage": {"input_tokens": "lots", "output_tokens": None},
                    },
                }
            )
            + "\n"
        )
        events = parse_transcript(path)
        assert len(events) == 1
        assert events[0].usage.input_tokens == 0
        assert events[0].usage.output_tokens == 0


# ----------------------------------------------------------------- after_seq


class TestIncrementalFetch:
    def test_after_seq_returns_only_what_is_new(self, client, session_id):
        everything = fetch(client, session_id)
        assert len(everything) > 5
        cut = everything[len(everything) // 2]["seq"]
        tail = fetch(client, session_id, after_seq=cut)
        assert tail, "the tail should not be empty for a mid-transcript cut"
        assert all(e["seq"] > cut for e in tail)
        assert len(tail) == len(everything) - cut

    def test_seq_values_are_identical_across_full_and_incremental_fetches(self, client, session_id):
        """The deep-link guarantee. ``?seq=`` has to keep pointing at the same
        turn whether the client loaded the whole transcript or polled its way
        there, so ``seq`` counts from the start of the file, not the response.
        """
        everything = {e["seq"]: e for e in fetch(client, session_id)}
        cut = sorted(everything)[3]
        for event in fetch(client, session_id, after_seq=cut):
            assert event == everything[event["seq"]]

    def test_a_seq_past_the_end_is_an_empty_list_not_an_error(self, client, session_id):
        assert fetch(client, session_id, after_seq=100_000) == []

    def test_zero_and_a_negative_seq_both_mean_everything(self, client, session_id):
        everything = fetch(client, session_id)
        assert fetch(client, session_id, after_seq=0) == everything
        assert fetch(client, session_id, after_seq=-1) == everything

    def test_polling_an_appending_transcript_never_repeats_or_skips(self, repo, session_id):
        """What the 3s poll actually does: fetch the tail, note the highest seq,
        fetch again after more turns land. The union must be the whole file with
        nothing duplicated."""
        manifest = ManifestStore(RunPaths(repo, "smoke1")).load()
        path = Path(
            next(
                s.transcript_path
                for e in manifest.groups.values()
                for s in e.sessions
                if s.session_id == session_id
            )
        )
        lines = path.read_text().splitlines(keepends=True)
        half = len(lines) // 2

        path.write_text("".join(lines[:half]))
        first = parse_transcript(path)
        highest = first[-1].seq

        path.write_text("".join(lines))
        second = parse_transcript(path, after_seq=highest)

        seqs = [e.seq for e in first] + [e.seq for e in second]
        assert seqs == sorted(seqs)
        assert len(seqs) == len(set(seqs)), "an event was sent twice"
        assert seqs == [e.seq for e in parse_transcript(path)], "an event was skipped"
