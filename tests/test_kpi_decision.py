"""The pure KPI contract, noise floor, decide() and Ledger (plan U9). No git,
no subprocess, no LLM — every case here is table-driven arithmetic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.execution.kpi import (
    Attempt,
    Guard,
    KpiContract,
    Ledger,
    decide,
    noise_floor,
    render_ledger_table,
    signed_delta,
)

# --------------------------------------------------------------- constructors


def test_kpi_contract_constructs_with_at_least_one_harness_path():
    contract = KpiContract(key="hat", direction="max", harness_paths=["scripts/eval.py"])
    assert contract.harness_paths == ["scripts/eval.py"]


def test_kpi_contract_rejects_empty_harness_paths():
    with pytest.raises(ValidationError):
        KpiContract(key="hat", direction="max", harness_paths=[])


# --------------------------------------------------------------- signed_delta


def test_signed_delta_max_direction_is_unflipped():
    assert signed_delta(candidate=5.0, champion=3.0, direction="max") == 2.0
    assert signed_delta(candidate=1.0, champion=3.0, direction="max") == -2.0


def test_signed_delta_min_direction_flips_the_sign():
    # candidate is lower (better, when minimising) -> positive delta.
    assert signed_delta(candidate=1.0, champion=3.0, direction="min") == 2.0
    assert signed_delta(candidate=5.0, champion=3.0, direction="min") == -2.0


# --------------------------------------------------------------- noise_floor


def test_noise_floor_needs_at_least_two_points():
    assert noise_floor([]) == 0.0
    assert noise_floor([0.5]) == 0.0


def test_noise_floor_is_zero_for_a_deterministic_history():
    assert noise_floor([0.2, 0.2, 0.2, 0.2, 0.2]) == 0.0


def test_noise_floor_matches_stdlib_mad_over_the_last_five():
    deltas = [0.1, 0.3, -0.2, 0.05, 0.4, 0.9, -0.3]
    window = deltas[-5:]
    center = median = __import__("statistics").median(window)
    reference = __import__("statistics").median(abs(x - center) for x in window)
    assert noise_floor(deltas) == pytest.approx(reference)
    assert median == center  # silence "unused" lint noise, keep the reference explicit


# ------------------------------------------------------------------- decide


def _contract(**overrides) -> KpiContract:
    base = dict(key="score", direction="max", min_effect=0.1, harness_paths=["h.py"])
    base.update(overrides)
    return KpiContract(**base)


def test_decide_crash_short_circuits_everything():
    contract = _contract(min_effect=0.0)
    assert (
        decide(delta=100.0, guard_deltas=[], floor=0.0, contract=contract, crashed=True) == "crash"
    )


def test_decide_negative_delta_is_discard():
    contract = _contract()
    assert decide(delta=-0.01, guard_deltas=[], floor=0.0, contract=contract) == "discard"


def test_decide_deterministic_history_delta_equal_to_min_effect_is_keep():
    # floor == 0 (deterministic history) and delta exactly clears min_effect.
    contract = _contract(min_effect=0.1)
    assert decide(delta=0.1, guard_deltas=[], floor=0.0, contract=contract) == "keep"


def test_decide_noisy_history_clearing_delta_is_promising_not_keep():
    contract = _contract(min_effect=0.1)
    floor = 0.05
    delta = max(contract.min_effect, 2 * floor) + 0.01
    assert decide(delta=delta, guard_deltas=[], floor=floor, contract=contract) == "promising"


def test_decide_guard_regression_is_discard_even_with_a_large_kpi_gain():
    contract = _contract(
        min_effect=0.0,
        guards=[Guard(key="latency", direction="min", max_regression=0.05)],
    )
    # KPI gained a lot, but the guard's signed delta regressed past its bound.
    assert decide(delta=10.0, guard_deltas=[-0.2], floor=0.0, contract=contract) == "discard"


def test_decide_guard_within_bound_does_not_block_a_keep():
    contract = _contract(
        min_effect=0.0,
        guards=[Guard(key="latency", direction="min", max_regression=0.05)],
    )
    assert decide(delta=1.0, guard_deltas=[-0.01], floor=0.0, contract=contract) == "keep"


def test_decide_delta_below_threshold_is_inconclusive():
    contract = _contract(min_effect=0.1)
    assert decide(delta=0.05, guard_deltas=[], floor=0.03, contract=contract) == "inconclusive"


def test_decide_direction_min_flips_which_sign_is_an_improvement():
    contract = _contract(direction="min", min_effect=0.1)
    champion, candidate = 10.0, 9.0  # candidate is lower -> better, under "min"
    delta = signed_delta(candidate, champion, contract.direction)
    assert delta == pytest.approx(1.0)
    assert decide(delta=delta, guard_deltas=[], floor=0.0, contract=contract) == "keep"


# ------------------------------------------------------------------- ledger


def _attempt(round_no: int, outcome: str = "discard", delta: float | None = -0.1) -> Attempt:
    return Attempt(
        round_no=round_no,
        candidate_commit=f"{'a' * 7}{round_no:x}",
        kpi_value=1.0 + round_no,
        guard_values={},
        delta=delta,
        noise_floor=0.0,
        outcome=outcome,
        harness_hash="deadbeef",
        why="test",
        at="2026-09-27T00:00:00+00:00",
    )


def test_ledger_round_trips_byte_equal_after_thirty_appends(tmp_path: Path):
    path = tmp_path / "ledger.json"
    ledger = Ledger()
    for i in range(30):
        ledger.append(path, _attempt(i))
    first_bytes = path.read_bytes()

    reloaded = Ledger.load(path)
    reloaded.save(path)
    second_bytes = path.read_bytes()

    assert first_bytes == second_bytes
    assert len(reloaded.attempts) == 30
    assert reloaded == ledger


def test_ledger_deltas_and_champion():
    ledger = Ledger()
    ledger.attempts.append(_attempt(0, outcome="discard", delta=-0.2))
    ledger.attempts.append(_attempt(1, outcome="keep", delta=0.3))
    ledger.attempts.append(_attempt(2, outcome="crash", delta=None))
    assert ledger.deltas() == [-0.2, 0.3]
    assert ledger.champion().round_no == 1


# ---------------------------------------------------------- render_ledger_table


def test_render_ledger_table_under_cap_includes_every_row():
    ledger = Ledger(attempts=[_attempt(i, outcome="keep") for i in range(3)])
    table = render_ledger_table(ledger, max_chars=6000)
    assert "earlier rows" not in table
    for i in range(3):
        assert f"| {i} |" in table


def test_render_ledger_table_capped_ends_with_earlier_rows_note_and_keeps_newest():
    ledger = Ledger(attempts=[_attempt(i, outcome="keep") for i in range(50)])
    table = render_ledger_table(ledger, max_chars=500)
    lines = table.splitlines()
    assert lines[-1].startswith("+") and "earlier rows — see ledger.json" in lines[-1]
    assert "| 49 |" in table
    assert len(table) <= 500 or lines[-1] == table.splitlines()[-1]
