"""Unit tests for simulator.pump.

Covers the four-state lifecycle (HEALTHY → DEGRADING → FAILING → FAILED),
telemetry-dict shape against context/_interfaces.md, reproducibility,
timestamp formatting, and the ADR-0002 RPM/degradation coupling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import mean

import pytest

from simulator.pump import (
    DEFAULT_PROFILES,
    TELEMETRY_KEYS,
    Pump,
    PumpState,
    StateProfile,
)

# A fast dwell profile so auto-transition tests don't iterate thousands of
# times. Keeps per-state rates / ceilings at defaults; only shortens dwell.
FAST_DWELL = {
    state: StateProfile(
        rate_per_tick=DEFAULT_PROFILES[state].rate_per_tick,
        ceiling=DEFAULT_PROFILES[state].ceiling,
        dwell_ticks=(2 if DEFAULT_PROFILES[state].dwell_ticks is not None else None),
    )
    for state in PumpState
}


# -- pump_id validation ----------------------------------------------------


@pytest.mark.parametrize("bad_id", ["P-7", "p-07", "PUMP-07", "P_07", "", "P-007"])
def test_pump_id_format_enforced(bad_id):
    with pytest.raises(ValueError):
        Pump(bad_id)


def test_pump_id_accepts_zero_padded():
    p = Pump("P-07")
    assert p.pump_id == "P-07"


# -- Telemetry dict shape (against _interfaces.md) -------------------------


def test_step_returns_all_expected_keys():
    p = Pump("P-01", seed=0)
    reading = p.step()
    assert set(reading.keys()) == set(TELEMETRY_KEYS)


def test_step_returns_correct_types():
    p = Pump("P-01", seed=0)
    reading = p.step()
    assert isinstance(reading["pump_id"], str)
    assert isinstance(reading["ts"], str)
    for k in ("vibration_amp", "bearing_temp", "motor_current", "rpm"):
        assert isinstance(reading[k], float), f"{k} should be float, got {type(reading[k])}"


def test_step_pump_id_field_matches_constructor():
    p = Pump("P-07", seed=0)
    reading = p.step()
    assert reading["pump_id"] == "P-07"


# -- Timestamp formatting --------------------------------------------------


def test_timestamp_iso8601_ms_format():
    p = Pump("P-01", seed=0)
    fixed = datetime(2026, 5, 24, 14, 32, 1, 123_456, tzinfo=timezone.utc)
    reading = p.step(now=fixed)
    assert reading["ts"] == "2026-05-24T14:32:01.123Z"


def test_timestamp_naive_datetime_treated_as_utc():
    p = Pump("P-01", seed=0)
    naive = datetime(2026, 5, 24, 14, 32, 1, 0)
    assert p.step(now=naive)["ts"] == "2026-05-24T14:32:01.000Z"


def test_timestamp_default_is_recent_utc():
    p = Pump("P-01", seed=0)
    before = datetime.now(timezone.utc)
    ts_str = p.step()["ts"]
    after = datetime.now(timezone.utc)
    parsed = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    assert before.replace(microsecond=0) <= parsed <= after.replace(microsecond=999_999)


# -- HEALTHY state ---------------------------------------------------------


def test_healthy_state_initial_degradation_near_zero():
    p = Pump("P-01", seed=42)
    assert p.state is PumpState.HEALTHY
    assert p.degradation == 0.0


def test_healthy_state_degradation_stays_under_ceiling():
    p = Pump("P-01", seed=42)
    for _ in range(500):
        p.step()
    assert p.state is PumpState.HEALTHY
    assert p.degradation <= DEFAULT_PROFILES[PumpState.HEALTHY].ceiling


def test_healthy_telemetry_in_expected_band():
    p = Pump("P-01", seed=42, ambient=22.0, setpoint=1800.0)
    readings = [p.step() for _ in range(50)]
    # At d≈0, RPM ≈ setpoint, bearing ≈ ambient + 0.02*1800 = 58.
    avg_bearing = mean(r["bearing_temp"] for r in readings)
    assert 56.0 < avg_bearing < 60.0
    avg_vib = mean(r["vibration_amp"] for r in readings)
    assert 0.2 < avg_vib < 0.6


# -- DEGRADING state -------------------------------------------------------


def test_degrading_state_monotonic_rise_on_average():
    p = Pump("P-01", seed=42)
    p.force_state(PumpState.DEGRADING)
    samples = []
    for _ in range(100):
        p.step()
        samples.append(p.degradation)
    first_q = mean(samples[:25])
    last_q = mean(samples[-25:])
    assert last_q > first_q + 0.05


def test_degrading_caps_at_ceiling():
    ceiling = DEFAULT_PROFILES[PumpState.DEGRADING].ceiling
    p = Pump(
        "P-01",
        seed=42,
        initial_state=PumpState.DEGRADING,
        profiles={
            PumpState.DEGRADING: StateProfile(
                rate_per_tick=0.0015, ceiling=ceiling, dwell_ticks=10_000
            )
        },
    )
    for _ in range(1000):
        p.step()
    assert p.degradation == pytest.approx(ceiling, abs=1e-9)


# -- FAILING state ---------------------------------------------------------


def test_failing_has_higher_vibration_than_degrading_at_ceiling():
    """Per ADR 0002, bearing_temp is no longer monotonic in degradation
    (RPM drops faster than the +15*d term rises), so vibration is the
    cleanest 'wear' signal: linear in d, no RPM coupling."""

    def avg_vibration(state: PumpState) -> float:
        p = Pump(
            "P-01",
            seed=7,
            initial_state=state,
            profiles={
                state: StateProfile(
                    rate_per_tick=DEFAULT_PROFILES[state].rate_per_tick,
                    ceiling=DEFAULT_PROFILES[state].ceiling,
                    dwell_ticks=10_000,
                )
            },
            initial_degradation=DEFAULT_PROFILES[state].ceiling,
        )
        readings = [p.step() for _ in range(100)]
        return mean(r["vibration_amp"] for r in readings)

    assert avg_vibration(PumpState.FAILING) > avg_vibration(PumpState.DEGRADING)


def test_failing_vibration_rises_faster_than_degrading_from_zero():
    """Derivative-fairness (Gemini review #4): from-zero rampup proves
    FAILING accumulates wear faster than DEGRADING. Catches bugs that an
    at-ceiling comparison can't (e.g. FAILING rate set to 0)."""

    def vibration_after_50_ticks(state: PumpState) -> float:
        p = Pump(
            "P-01",
            seed=7,
            initial_state=state,
            initial_degradation=0.0,
            profiles={
                state: StateProfile(
                    rate_per_tick=DEFAULT_PROFILES[state].rate_per_tick,
                    ceiling=DEFAULT_PROFILES[state].ceiling,
                    dwell_ticks=10_000,
                )
            },
        )
        readings = [p.step() for _ in range(50)]
        return mean(r["vibration_amp"] for r in readings)

    failing_vib = vibration_after_50_ticks(PumpState.FAILING)
    degrading_vib = vibration_after_50_ticks(PumpState.DEGRADING)
    assert failing_vib > degrading_vib + 0.05


# -- FAILED state ----------------------------------------------------------


def test_failed_state_pins_degradation_to_one_immediately():
    p = Pump("P-01", seed=0)
    p.force_state(PumpState.FAILED)
    assert p.degradation == 1.0
    for _ in range(20):
        p.step()
        assert p.degradation == 1.0


def test_failed_state_keeps_emitting_telemetry():
    """Per ADR 0002: FAILED-state envelope is near-zero RPM with high
    stutter, bearing temp dominated by ambient + degradation term (RPM
    contribution vanishes), vibration at its maximum."""
    p = Pump("P-01", seed=0, initial_state=PumpState.FAILED)
    readings = [p.step() for _ in range(50)]
    for r in readings:
        assert set(r.keys()) == set(TELEMETRY_KEYS)
    assert mean(r["vibration_amp"] for r in readings) > 2.5
    avg_rpm = mean(r["rpm"] for r in readings)
    assert abs(avg_rpm) < 50  # σ_mean ≈ 20/√50 ≈ 2.8
    avg_bearing = mean(r["bearing_temp"] for r in readings)
    assert 33 < avg_bearing < 42


def test_failed_state_does_not_auto_advance():
    p = Pump("P-01", seed=0, initial_state=PumpState.FAILED)
    for _ in range(50):
        p.step()
    assert p.state is PumpState.FAILED


# -- Automatic and manual transitions --------------------------------------


def test_auto_transitions_through_all_four_states():
    p = Pump("P-01", seed=0, profiles=FAST_DWELL)
    states_seen = [p.state]
    for _ in range(20):
        p.step()
        if p.state is not states_seen[-1]:
            states_seen.append(p.state)
    assert states_seen == [
        PumpState.HEALTHY,
        PumpState.DEGRADING,
        PumpState.FAILING,
        PumpState.FAILED,
    ]


def test_force_state_resets_tick_counter():
    p = Pump("P-01", seed=0)
    for _ in range(5):
        p.step()
    assert p.ticks_in_state == 5
    p.force_state(PumpState.FAILING)
    assert p.state is PumpState.FAILING
    assert p.ticks_in_state == 0


def test_force_state_rejects_non_pumpstate():
    p = Pump("P-01", seed=0)
    with pytest.raises(TypeError):
        p.force_state("DEGRADING")  # type: ignore[arg-type]


# -- Reproducibility -------------------------------------------------------


def test_seeded_reproducibility():
    a = Pump("P-01", seed=12345)
    b = Pump("P-01", seed=12345)
    fixed = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    for _ in range(50):
        assert a.step(now=fixed) == b.step(now=fixed)


def test_different_seeds_diverge():
    a = Pump("P-01", seed=1)
    b = Pump("P-01", seed=2)
    fixed = datetime(2026, 5, 24, 0, 0, 0, tzinfo=timezone.utc)
    diffs = sum(a.step(now=fixed) != b.step(now=fixed) for _ in range(10))
    assert diffs > 0


# -- Constructor validation ------------------------------------------------


def test_initial_degradation_out_of_range_rejected():
    with pytest.raises(ValueError):
        Pump("P-01", initial_degradation=-0.1)
    with pytest.raises(ValueError):
        Pump("P-01", initial_degradation=1.5)


def test_initial_failed_state_snaps_degradation_to_one():
    p = Pump("P-01", initial_state=PumpState.FAILED)
    assert p.degradation == 1.0
