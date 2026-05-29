"""Tests for the shared.score and shared.drift stubs.

The stubs are interface-locks, not real implementations. These tests
pin:
- Score returns a float in [0, 1].
- Score is deterministic for the same input.
- Drift returns a dict with all 8 FEATURE_NAMES as keys, all floats.
- Drift accepts an iterable of feature dicts (the rolling window form).

When the model + drift sessions land real implementations, these tests
update with the actual semantics but the interface contracts stay.
"""

from __future__ import annotations

import pytest

from shared.drift import compute_psi
from shared.features import FEATURE_NAMES
from shared.score import score


def _features(vib_mean: float = 0.5) -> dict[str, float]:
    return {
        "vibration_amp": 0.3,
        "bearing_temp": 60.0,
        "motor_current": 4.0,
        "rpm": 1800.0,
        "vibration_amp_mean_5m": vib_mean,
        "vibration_amp_std_5m": 0.05,
        "bearing_temp_mean_5m": 60.0,
        "bearing_temp_std_5m": 0.5,
    }


def test_score_returns_float_in_unit_interval():
    s = score(_features())
    assert isinstance(s, float)
    assert 0.0 <= s <= 1.0


def test_score_is_deterministic():
    f = _features()
    assert score(f) == score(f)


def test_score_increases_with_vibration_mean():
    """Stub uses vibration_amp_mean_5m / 3.0 — higher mean → higher score."""
    low = score(_features(vib_mean=0.1))
    high = score(_features(vib_mean=2.0))
    assert high > low


def test_score_clamps_at_one():
    """vibration_amp_mean_5m >= 3.0 → score = 1.0 (clamped)."""
    assert score(_features(vib_mean=5.0)) == 1.0


def test_score_clamps_at_zero():
    """Negative vibration_amp_mean_5m (shouldn't happen physically, but
    test the clamp anyway) → score = 0.0."""
    assert score(_features(vib_mean=-1.0)) == 0.0


def test_compute_psi_returns_dict_with_all_feature_keys():
    psi = compute_psi([_features()], reference=None)
    assert set(psi.keys()) == set(FEATURE_NAMES)


def test_compute_psi_values_are_floats():
    psi = compute_psi([_features()], reference=None)
    for v in psi.values():
        assert isinstance(v, float)


def test_compute_psi_accepts_empty_window():
    """Stub is permissive — empty window doesn't raise."""
    psi = compute_psi([], reference=None)
    assert isinstance(psi, dict)


def test_compute_psi_sentinels_span_warning_and_stable():
    """Stub returns values that exercise both threshold bands:
    vibration_amp in [0.10, 0.25] (warning), others < 0.10 (stable).
    Pinned so downstream alert wiring has a known fixture."""
    psi = compute_psi([_features()], reference=None)
    assert 0.10 <= psi["vibration_amp"] < 0.25  # warning band
    for name in FEATURE_NAMES:
        if name != "vibration_amp":
            assert psi[name] < 0.10  # stable band
