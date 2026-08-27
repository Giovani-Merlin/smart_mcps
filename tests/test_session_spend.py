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


def test_inherited_cache_read_is_the_first_turns_own_figure():
    """Turn 1's cache read is context the round inherited, not created — its own
    figure, distinct from the round's total cache read."""
    envelope = _envelope_with_iterations(
        3,
        per_turn={
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 1,
        },
        last={"cache_read_input_tokens": 500},
    )
    # Turns: [100, 100, 500] -> total 700, inherited (turn 1) = 100.
    spend = RoundSpend.from_envelope(envelope)
    assert spend.cache_read_input_tokens == 700
    assert spend.inherited_cache_read_tokens == 100
    assert spend.inherited_cache_read_tokens != spend.cache_read_input_tokens


def test_inherited_cache_read_accumulates_across_rounds_as_its_own_total():
    round_one = {
        "usage": {
            "input_tokens": 1,
            "output_tokens": 1,
            "cache_read_input_tokens": 50,
            "cache_creation_input_tokens": 1,
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
    # Single-turn round contributes its own figure as "inherited" (50); the
    # two-turn round contributes its first turn's figure (200). Total: 250.
    assert usage.total_inherited_cache_read_tokens == 50 + 200
    # Distinct from total cache read, which sums every turn of every round.
    assert usage.total_cache_read_tokens == 50 + 400
    assert usage.total_inherited_cache_read_tokens != usage.total_cache_read_tokens


def test_no_dollar_figures_are_computed_here():
    """This unit reports token classes only; RoundSpend carries no cost field."""
    assert not hasattr(RoundSpend(), "cost_usd")
    assert not hasattr(RoundSpend(), "dollars")
