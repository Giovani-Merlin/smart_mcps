"""U9: spend and occupancy are two quantities.

``RoundUsage.from_envelope`` (occupancy) reads the latest turn only and must
stay exactly as it is — two tests in ``test_sessions.py`` pin that reading
against the P0 where a 50x-inflated context retired healthy coders. Spend is
a different question: what did every turn of the round actually bill. These
tests exercise ``RoundSpend`` and ``SessionUsage.add`` directly, against the
same envelope shapes ``test_sessions.py`` uses, rather than re-deriving the
occupancy suite.
"""

from __future__ import annotations

from orchestrator.execution.sessions import RoundSpend, RoundUsage, SessionUsage
from orchestrator.execution.streaming import TurnUsage


def _envelope_with_iterations(count: int, *, per_turn: dict, last: dict | None = None) -> dict:
    """Build an envelope whose top-level ``usage`` is the sum of ``count`` turns
    of ``per_turn`` (the shape a real multi-turn round has), with ``last``
    overriding the final iteration's fields when given."""
    iterations = [dict(per_turn) for _ in range(count)]
    if last is not None:
        iterations[-1] = {**iterations[-1], **last}
    summed = {
        key: sum(it.get(key, 0) for it in iterations)
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    }
    return {"usage": {**summed, "iterations": iterations}}


def test_spend_reads_the_all_turns_sum_not_the_final_iteration():
    """A 190-turn envelope: total_output_tokens must equal the top-level usage
    figure, not the last iteration's alone."""
    envelope = _envelope_with_iterations(
        190,
        per_turn={
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 3,
        },
    )
    spend = RoundSpend.from_envelope(envelope)
    assert spend.output_tokens == 190 * 2
    assert spend.input_tokens == 190 * 1
    assert spend.cache_read_input_tokens == 190 * 100
    assert spend.cache_creation_input_tokens == 190 * 3

    usage = SessionUsage()
    usage.add(RoundUsage.from_envelope(envelope), spend)
    assert usage.total_output_tokens == 190 * 2
    assert usage.total_input_tokens == 190 * 1
    assert usage.total_cache_read_tokens == 190 * 100
    assert usage.total_cache_creation_tokens == 190 * 3


def test_occupancy_still_reads_only_the_final_iteration():
    """Same envelope as above: last_context_tokens must still be the final
    turn's context, exactly as RoundUsage.from_envelope has always computed it
    — the two pinned tests in test_sessions.py exercise this reading directly,
    this test only checks that SessionUsage.add still wires it through
    unchanged now that it also takes a RoundSpend."""
    envelope = _envelope_with_iterations(
        190,
        per_turn={
            "input_tokens": 1,
            "output_tokens": 2,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 3,
        },
        last={
            "input_tokens": 2,
            "output_tokens": 265,
            "cache_read_input_tokens": 161_000,
            "cache_creation_input_tokens": 218,
        },
    )
    usage_round = RoundUsage.from_envelope(envelope)
    assert usage_round.context_tokens == 2 + 265 + 161_000 + 218

    usage = SessionUsage()
    usage.add(usage_round, RoundSpend.from_envelope(envelope))
    assert usage.last_context_tokens == 2 + 265 + 161_000 + 218
    # And it is nowhere near the all-turns spend sum, which is the whole point.
    assert usage.last_context_tokens < usage.total_cache_read_tokens


def test_envelope_without_iterations_key_falls_back_to_top_level_for_both():
    """Older CLIs and the test stub emit no `iterations`; for a single turn the
    top level *is* the round, so both spend and occupancy read it directly and
    agree."""
    envelope = {
        "usage": {
            "input_tokens": 2,
            "output_tokens": 4,
            "cache_read_input_tokens": 7_370,
            "cache_creation_input_tokens": 17_158,
        }
    }
    spend = RoundSpend.from_envelope(envelope)
    assert spend.input_tokens == 2
    assert spend.output_tokens == 4
    assert spend.cache_read_input_tokens == 7_370
    assert spend.cache_creation_input_tokens == 17_158

    usage = SessionUsage()
    usage.add(RoundUsage.from_envelope(envelope), spend)
    assert usage.total_output_tokens == 4
    assert usage.last_context_tokens == 2 + 4 + 7_370 + 17_158


def test_base_context_comes_from_the_streams_first_turn_not_the_envelope():
    """F10: the envelope carries no turn-1 data at all — a probed result
    envelope with ``num_turns: 2`` had a one-element ``iterations`` whose single
    entry was turn 2, so indexing ``iterations[0]`` silently reported the *last*
    turn. The stream's own first-turn observation is authoritative, and the
    figure is turn 1's cache_read + cache_creation together: a fork's first
    genuine call splits its inherited prefix across both fields."""
    # An envelope shaped like the probed defect: one iteration, and it is the
    # last turn's (huge cache read), not turn 1's.
    envelope = {
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_input_tokens": 175_551,
            "cache_creation_input_tokens": 2_000,
            "iterations": [
                {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 175_551,
                    "cache_creation_input_tokens": 2_000,
                }
            ],
        }
    }
    first_turn = TurnUsage(
        input_tokens=1,
        output_tokens=2,
        cache_read_input_tokens=19_968,
        cache_creation_input_tokens=41_538,
    )
    spend = RoundSpend.from_envelope(envelope, first_turn=first_turn)
    # The sum of turn 1's two prefix fields — not the last turn's cache read.
    assert spend.base_context_tokens == 19_968 + 41_538
    assert spend.base_context_tokens != spend.cache_read_input_tokens
    # The all-turns spend figures still come from the envelope's top level.
    assert spend.cache_read_input_tokens == 175_551


def test_base_context_falls_back_to_iterations_when_no_stream_observation():
    """Callers with no stream first-turn (older paths, tests) degrade to the
    envelope's iterations[0] — best-effort, and still cache_read + creation."""
    envelope = _envelope_with_iterations(
        3,
        per_turn={
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
        last={"cache_read_input_tokens": 500},
    )
    spend = RoundSpend.from_envelope(envelope)
    assert spend.base_context_tokens == 100 + 7


def test_base_context_is_recorded_once_not_summed_across_rounds():
    """F10: the session-level figure is the prefix the *session* started from —
    round 1's first turn. A later round's first turn re-reads the whole
    accumulated context, so summing across rounds would conflate the shared
    base with the session's own growth (the old field did exactly that and
    varied per group instead of being the near-constant shared base)."""
    round_one = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 10,
        }
    }
    round_two = _envelope_with_iterations(
        2,
        per_turn={
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 1,
        },
    )
    usage = SessionUsage()
    usage.add(RoundUsage.from_envelope(round_one), RoundSpend.from_envelope(round_one))
    usage.add(RoundUsage.from_envelope(round_two), RoundSpend.from_envelope(round_two))
    # Round 1's first turn (50 + 10), held stable — round 2 does not add to it.
    assert usage.base_context_tokens == 50 + 10
    # Distinct from total cache read, which sums every turn of every round.
    assert usage.total_cache_read_tokens == 50 + 400
    assert usage.base_context_tokens != usage.total_cache_read_tokens


def test_no_dollar_figures_are_computed_here():
    """This unit reports token classes only; RoundSpend carries no cost field."""
    assert not hasattr(RoundSpend(), "cost_usd")
    assert not hasattr(RoundSpend(), "dollars")
