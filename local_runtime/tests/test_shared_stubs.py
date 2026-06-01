"""Tests for the shared.score and shared.drift interface contracts.

History: file originated as "test the stubs return well-shaped values."
The model session (2026-06-01) swapped ``shared.score.score`` from a
deterministic stub to a HistGradientBoostingClassifier-backed
implementation; the stub-specific clamp tests
(``test_score_clamps_at_one``/``..._at_zero``) were removed since
they pinned the ``clip(vibration_amp_mean_5m / 3.0, ...)`` math that
no longer exists. The interface-contract tests below survive the
swap — they're the per-call invariants the
``local_runtime.service.ScorerService`` depends on.

What this file does NOT cover:
- Round-trip artifact integrity (``model/tests/test_train.py``).
- Model load + validation errors (``model/tests/test_score_wiring.py``).
- Structural mode-parity / inspect.getfile invariant
  (``local_runtime/tests/test_service.py::test_structural_parity_*``).

What this file pins for the live model:
- ``score(features)`` returns a float in ``[0, 1]``.
- ``score`` is deterministic for the same input (sklearn classifiers
  are; if a future session swaps in a model with non-deterministic
  scoring, this test fires).
- A higher rolling vibration mean produces a higher score (the
  minimum-signal sanity check — the real AUC ≥ 0.85 acceptance
  criterion is enforced in ``model.train.main`` and re-checked in
  ``model/tests/test_train.py``).

PSI is still a stub; the drift session will rewrite this file's
drift-side tests when it lands.
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
    """sklearn's predict_proba on a fitted classifier is deterministic;
    a future session that swaps in a non-deterministic family (e.g.,
    Monte-Carlo dropout) needs to either update this test with a
    seeded variant or write the ADR justifying the change."""
    f = _features()
    assert score(f) == score(f)


def test_score_increases_with_vibration_mean():
    """A pre-failure pump has a noticeably higher rolling vibration
    mean (~1.0+) than a healthy one (~0.3). The classifier MUST
    rank the higher-vibration sample higher — anything else means
    the model didn't learn the main physical signal."""
    low = score(_features(vib_mean=0.3))
    high = score(_features(vib_mean=2.0))
    assert high > low


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
    Pinned so downstream alert wiring has a known fixture. This test
    goes away when the drift session lands real PSI."""
    psi = compute_psi([_features()], reference=None)
    assert 0.10 <= psi["vibration_amp"] < 0.25  # warning band
    for name in FEATURE_NAMES:
        if name != "vibration_amp":
            assert psi[name] < 0.10  # stable band
