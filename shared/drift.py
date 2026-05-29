"""Drift-detection stub — placeholder for the PSI implementation.

Interface is locked here so ``local_runtime`` and the future
``lambda_scorer`` both call ``compute_psi(window_features, reference)``
without caring whether the implementation is a stub or the real
binned PSI. The drift session swaps in the real implementation
without touching call sites.

PSI signature (per PLAN.md §2.7 and context/_interfaces.md):

    PSI = Σ (actual_pct - expected_pct) * ln(actual_pct / expected_pct)

Thresholds (used downstream by alerts):
- < 0.10 stable
- 0.10–0.25 warning
- > 0.25 significant shift → SNS alert

The real implementation needs the reference distribution
(``model/artifacts/reference_distribution.json`` per
context/_interfaces.md) and bin-by-bin actual percentages computed
from the rolling 1-hour window. The stub returns sentinel values that
exercise both thresholds without crossing them, so the integration
test for "score + PSI both land in InfluxDB" can run without the real
reference data.
"""

from __future__ import annotations

from typing import Iterable, Mapping


# Per-feature PSI values returned by the stub. Chosen so:
# - vibration_amp lands in the "warning" band (0.10–0.25) — shows up
#   in Grafana but doesn't fire an alert.
# - all other features land in "stable" (<0.10).
# Downstream consumers (alert wiring, dashboards) can verify their
# threshold logic against a known set of sentinels without needing
# the real distribution data.
_STUB_PSI: dict[str, float] = {
    "vibration_amp": 0.15,
    "bearing_temp": 0.02,
    "motor_current": 0.03,
    "rpm": 0.01,
    "vibration_amp_mean_5m": 0.04,
    "vibration_amp_std_5m": 0.05,
    "bearing_temp_mean_5m": 0.02,
    "bearing_temp_std_5m": 0.03,
}


def compute_psi(
    window_features: Iterable[Mapping[str, float]],
    reference: Mapping[str, object] | None = None,
) -> dict[str, float]:
    """Return per-feature PSI for the given rolling window.

    Real implementation (drift session): bin each feature's window
    values against the reference distribution's bin edges, compute
    actual/expected percentages with Laplace smoothing, sum the
    contributions per the PSI formula above.

    Stub implementation: returns ``_STUB_PSI`` verbatim. Inputs are
    accepted but ignored; the call site is locked so the drift session
    can drop in the real implementation with no other code changes.

    Args:
        window_features: ordered list of feature dicts (the rolling
            1-hour window per ``context/_interfaces.md`` §"PSI
            parameters"). Each dict matches ``shared.features``.
        reference: deserialized ``model/artifacts/reference_distribution.json``.
            ``None`` is acceptable for the stub and during dev runs
            before the model session has trained the baseline.

    Returns:
        Dict mapping feature name → PSI value. Keys match
        ``shared.features.FEATURE_NAMES`` so downstream consumers
        (InfluxDB writer, alert payloads) can index by feature.
    """
    # Materialize so passing in a generator is safe even though we
    # don't use it.
    _ = list(window_features)
    return dict(_STUB_PSI)
