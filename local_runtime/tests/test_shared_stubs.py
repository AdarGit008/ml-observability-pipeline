"""Tests for the shared.score and shared.drift interface contracts.

History: file originated as "test the stubs return well-shaped values."
The model session (2026-06-01) swapped ``shared.score.score`` from a
deterministic stub to a HistGradientBoostingClassifier-backed
implementation. The drift session (2026-06-01) swapped
``shared.drift.compute_psi`` from a sentinel stub to the real
np.histogram + Laplace-smoothed PSI per PLAN.md s2.7 (locked in
ADR 0007). ADR 0009 (2026-06-03) shrank the PSI surface from 8
features to 4 — synthetic-reference tests below iterate
``PSI_FEATURE_NAMES``.

The three stub-pinning tests that lived here previously --
``test_compute_psi_values_are_floats``,
``test_compute_psi_accepts_empty_window``, and
``test_compute_psi_sentinels_span_warning_and_stable`` -- are removed
by the drift session and replaced with the value-driven tests below.
The keys-match test (``test_compute_psi_returns_dict_with_all_feature_keys``)
survives as the interface invariant, now asserting against
``PSI_FEATURE_NAMES`` per ADR 0009.

What this file does NOT cover:
- Round-trip artifact integrity (``model/tests/test_train.py``).
- Model load + validation errors (``model/tests/test_score_wiring.py``).
- Structural mode-parity / inspect.getfile invariant
  (``local_runtime/tests/test_service.py::test_structural_parity_*``).
- DriftError raise paths on missing/malformed reference (covered by
  ``local_runtime/tests/test_drift_load.py``).
- The PSI surface ⊂ scorer input set asymmetry (covered by
  ``local_runtime/tests/test_features.py``).

What this file pins for the live model:
- ``score(features)`` returns a float in ``[0, 1]``.
- ``score`` is deterministic for the same input.
- A higher rolling vibration mean produces a higher score.

What this file pins for the live drift implementation:
- ``compute_psi`` returns all ``PSI_FEATURE_NAMES`` keys (4 per
  ADR 0009).
- A sample stream matching the reference distribution gives PSI ~ 0
  per feature.
- A shifted sample stream crosses the warning band [0.10, 0.25] and
  the significant band > 0.25 in the expected magnitudes.
- Adjacent-equal bin_edges (the model session's nextafter-nudge case
  for near-constant features) do not produce div-by-zero.
- ``psi_is_armed(window)`` arms at exactly ``PSI_MIN_SAMPLES`` (the
  ADR 0017 warmup boundary; alert-gating predicate, not a PSI value).

The PSI-value tests below are **golden tests** -- the magnitudes are
analytically derived from the formula (PLAN.md s2.7 + Laplace
alpha = 1.0) against the synthetic reference, NOT random draws. A
correct change to the formula (e.g., a deliberate alpha change) will
fail these tests for the right reason, forcing the reviewer to
recompute and update the expected magnitudes. Per Gemini Q6 of the
2026-06-01 review: "for a foundational metric, this forced
re-validation is often a feature, not a bug."
"""

from __future__ import annotations

import math

import pytest

from shared.drift import (
    PSI_MIN_SAMPLES,
    PSI_SIGNIFICANT_THRESHOLD,
    compute_psi,
    psi_alert_should_fire,
    psi_is_armed,
)
from shared.features import FEATURE_NAMES, PSI_FEATURE_NAMES
from shared.score import score


# ---------------------------------------------------------------------------
# Score side -- untouched by the drift session.
# ---------------------------------------------------------------------------


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
    rank the higher-vibration sample higher -- anything else means
    the model didn't learn the main physical signal."""
    low = score(_features(vib_mean=0.3))
    high = score(_features(vib_mean=2.0))
    assert high > low


# ---------------------------------------------------------------------------
# Drift side -- rewritten 2026-06-01 by the drift session; surface
# shrunk 2026-06-03 by ADR 0009.
# ---------------------------------------------------------------------------


# Synthetic-reference helpers. Tests pass these via the ``reference=``
# parameter (required after Gemini Q2 refactor) so they don't depend
# on disk state.


def _synthetic_reference(n_bins: int = 10, ref_count: int = 1000) -> dict:
    """Build a reference with equal-frequency uniform bins for every
    feature on the PSI surface.

    Bin edges 0.0, 1.0, ..., n_bins.0 (one-unit-wide bins for easy
    arithmetic). Each bin holds ``ref_count`` expected observations,
    so the expected distribution is uniform 10% per bin. PSI against a
    uniform actual sample stream should be ~ 0.

    Covers ``PSI_FEATURE_NAMES`` (4 entries per ADR 0009), not
    ``FEATURE_NAMES`` (8). ``compute_psi`` iterates the PSI surface
    only, so the reference's ``features`` map needs exactly those keys.
    """
    edges = [float(i) for i in range(n_bins + 1)]
    counts = [ref_count] * n_bins
    return {
        "model_version": "test-synth",
        "feature_names": list(PSI_FEATURE_NAMES),
        "n_bins": n_bins,
        "features": {
            name: {"bin_edges": list(edges), "bin_counts": list(counts)}
            for name in PSI_FEATURE_NAMES
        },
    }


def _feature_sample(value: float) -> dict[str, float]:
    """Build a feature dict where every (scorer) feature gets the same value.

    Lets us shift ALL features uniformly by varying ``value`` and
    inspect PSI per feature in lockstep -- the test is then about the
    binning math, not about feature-specific magnitudes.

    Keyed by ``FEATURE_NAMES`` (8 entries) because that's what
    ``extract_features`` produces in practice; ``compute_psi`` only
    looks up the 4 ``PSI_FEATURE_NAMES`` keys, but having all 8 keeps
    the test inputs realistic.
    """
    return {name: float(value) for name in FEATURE_NAMES}


def _samples_in_bins(bin_indices: list[int]) -> list[dict[str, float]]:
    """Build sample feature dicts by per-sample target bin index.

    Each sample's value is placed at the midpoint of the corresponding
    bin in the synthetic reference (bin width = 1.0, midpoint = i + 0.5).
    """
    return [_feature_sample(i + 0.5) for i in bin_indices]


def test_compute_psi_returns_dict_with_all_feature_keys():
    """Interface invariant (ADR 0009): every call returns all
    ``PSI_FEATURE_NAMES`` keys so downstream consumers (InfluxDB writer,
    alert payloads) can index by feature without sparse-dict handling.
    Rolling features are NOT in the dict — that's the surface shrink."""
    ref = _synthetic_reference()
    samples = _samples_in_bins([5] * 100)
    psi = compute_psi(samples, reference=ref)
    assert set(psi.keys()) == set(PSI_FEATURE_NAMES)


def test_compute_psi_identical_distribution_is_near_zero():
    """GOLDEN TEST: a sample stream that perfectly mirrors the
    reference's equal-frequency bins (10% per bin) gives PSI = 0 on
    every PSI surface feature (analytically: actual_pct[b] =
    expected_pct[b] for every b, so each summand is 0). With Laplace
    alpha = 1.0 and N = 200 samples (20 per bin), the smoothed
    percentages match expected exactly: 21/210 = 0.1 = 1001/10010, so
    PSI = 0 within floating-point tolerance."""
    ref = _synthetic_reference()
    # 20 samples per bin x 10 bins = 200 samples, perfectly uniform.
    bin_indices = []
    for b in range(10):
        bin_indices.extend([b] * 20)
    samples = _samples_in_bins(bin_indices)

    psi = compute_psi(samples, reference=ref)
    for name in PSI_FEATURE_NAMES:
        assert psi[name] < 0.01, (
            f"{name}: PSI={psi[name]!r} expected near 0 for distribution "
            "identical to reference"
        )


def test_compute_psi_shifted_distribution_crosses_warning_then_significant():
    """GOLDEN TEST: a modest shift (56 in top bin = 28%) lands in the
    warning band [0.10, 0.25] (analytical PSI ~ 0.21). A heavy shift
    (single-bin concentration) crosses the significant band > 0.25
    (analytical PSI ~ 4.5).

    Magnitudes are derived analytically and verified in ADR 0007
    section "Laplace smoothing alpha = 1.0" -- the test is pinning the
    threshold-crossing behaviour the alerts layer depends on. If
    Laplace alpha or the PSI formula changes, this test fires loudly,
    forcing the reviewer to recompute and update both the expected
    band and the implementation."""
    ref = _synthetic_reference()

    # Warning band: 56 samples in top bin (28%), 144 spread evenly
    # across the other 9 bins (16 each). PSI ~ 0.21 with alpha = 1.0
    # -- solidly inside the warning band with margin from both 0.10
    # and 0.25 boundaries.
    bin_indices = [9] * 56
    for b in range(9):
        bin_indices.extend([b] * 16)  # 9 * 16 = 144; total = 200
    samples = _samples_in_bins(bin_indices)

    psi_warning = compute_psi(samples, reference=ref)
    for name in PSI_FEATURE_NAMES:
        assert 0.10 <= psi_warning[name] < 0.25, (
            f"{name}: PSI={psi_warning[name]!r} expected in warning band "
            "[0.10, 0.25) for a 28%-in-top-bin shift"
        )

    # Significant band: all 200 samples in the top bin. PSI > 0.25
    # with wide margin; the exact value is large (~ 4.5 with
    # alpha = 1.0) but the threshold-crossing assertion is what
    # alerts care about.
    samples = _samples_in_bins([9] * 200)
    psi_significant = compute_psi(samples, reference=ref)
    for name in PSI_FEATURE_NAMES:
        assert psi_significant[name] > 0.25, (
            f"{name}: PSI={psi_significant[name]!r} expected > 0.25 "
            "for single-bin concentration"
        )


def test_compute_psi_constant_bin_edges_no_div_by_zero():
    """GOLDEN TEST: adjacent-equal bin_edges -- the model session's
    nextafter-nudge case for near-constant features -- must not
    produce div-by-zero or NaN/Inf PSI.

    Mechanism: np.histogram against zero-width bins returns count = 0
    for the affected bin; Laplace add-alpha smoothing then keeps the
    denominator positive, so the ln() and division terms stay finite.
    A regression here (e.g., switching from count-side Laplace to
    percentage-side epsilon that doesn't cover the count = 0 case)
    would produce NaN PSI and break the assertion.
    """
    ref = _synthetic_reference()
    # Collapse one bin: edges[5] == edges[4] makes bin 4 zero-width.
    for name in PSI_FEATURE_NAMES:
        edges = ref["features"][name]["bin_edges"]
        edges[5] = edges[4]

    samples = _samples_in_bins([2] * 100)  # all in bin 2, away from the collapsed bin
    psi = compute_psi(samples, reference=ref)

    for name in PSI_FEATURE_NAMES:
        assert math.isfinite(psi[name]), (
            f"{name}: PSI is not finite ({psi[name]!r}) -- "
            "zero-width bin produced div-by-zero or log(0)"
        )


def test_psi_is_armed_boundary():
    """ADR 0017 warmup predicate arms at exactly ``PSI_MIN_SAMPLES``.

    The single shared definition of "this window is warm enough to
    trust a PSI breach" (consumed by the lambda_scorer alert-arming
    site; local mode has no alert site). Pinned here -- numpy-free, no
    reference needed -- so the boundary can't move without updating
    ADR 0017. The lambda_scorer structural-parity test pins that the
    handler loads THIS function, not a vendored fork.
    """
    assert PSI_MIN_SAMPLES == 150
    assert psi_is_armed([0] * PSI_MIN_SAMPLES) is True
    assert psi_is_armed([0] * (PSI_MIN_SAMPLES - 1)) is False
    assert psi_is_armed([]) is False
    # Only length is consulted; contents are irrelevant to arming.
    assert psi_is_armed(range(PSI_MIN_SAMPLES)) is True


def test_psi_alert_should_fire_composite():
    """ADR 0017 §1 (DeepSeek-hardened): the shared composite encodes
    BOTH the warmup gate and the significant-shift threshold, so every
    alert site imports one decision rather than re-assembling it. Warm +
    breach -> True; warm + stable -> False; cold + breach -> False;
    empty psi -> False; threshold overridable.
    """
    breach = {"vibration_amp": 0.40, "bearing_temp": 0.02,
              "motor_current": 0.01, "rpm": 0.03}
    stable = {"vibration_amp": 0.02, "bearing_temp": 0.01,
              "motor_current": 0.03, "rpm": 0.02}
    warm = [0] * PSI_MIN_SAMPLES
    cold = [0] * (PSI_MIN_SAMPLES - 1)

    assert PSI_SIGNIFICANT_THRESHOLD == 0.25
    assert psi_alert_should_fire(warm, breach) is True
    assert psi_alert_should_fire(warm, stable) is False   # warm but < 0.25
    assert psi_alert_should_fire(cold, breach) is False    # breaching but cold
    assert psi_alert_should_fire([], breach) is False      # empty window = cold
    assert psi_alert_should_fire(warm, {}) is False        # no PSI surface
    assert psi_alert_should_fire(warm, breach, threshold=0.5) is False
