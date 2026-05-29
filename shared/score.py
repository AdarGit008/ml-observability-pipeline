"""Scoring stub — placeholder for the HistGradientBoostingClassifier.

Interface is locked here so ``local_runtime`` and the future
``lambda_scorer`` both call ``score(features)`` without caring whether
the implementation is a stub or the real classifier. The model
session swaps in ``model.pkl`` + a real ``predict_proba`` call without
touching call sites.

Why a deterministic placeholder rather than ``random.random()``: the
mode-parity invariant tests (``tests/test_mode_parity.py`` in this
session's package) need a stable, reproducible mapping from features
to score so the local_runtime ↔ Lambda parity check is meaningful
before the real model lands. A random stub would pass the structural
test ("same shape returned") but hide a real divergence later.
"""

from __future__ import annotations

from typing import Mapping


def score(features: Mapping[str, float]) -> float:
    """Return a placeholder failure probability in [0, 1].

    Real implementation (model session): load ``model.pkl`` at import
    time, call ``classifier.predict_proba(feature_vector)[0, 1]``.

    Stub implementation: ``clip(vibration_amp_mean_5m / 3.0, 0, 1)``.
    Picks the most degradation-correlated feature from PLAN.md §2.2
    (``vibration_amp = 0.3 + degradation * 2.5 + N(0, 0.05)``) and
    normalizes it into [0, 1]. Roughly tracks what the real model
    should do, so demo screenshots taken with the stub aren't wildly
    different from screenshots taken after the model lands.

    Args:
        features: dict matching the schema in ``shared.features``
            (8 keys, all floats).

    Returns:
        float in [0, 1] — placeholder P(failure within 48h).
    """
    raw = features["vibration_amp_mean_5m"] / 3.0
    return max(0.0, min(1.0, raw))
