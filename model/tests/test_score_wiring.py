"""Tests for the shared.score swap from stub to real predict_proba.

These tests verify the *call-site contract* of ``shared.score.score``
that ``local_runtime.service.ScorerService`` depends on:

  - signature ``(Mapping[str, float]) -> float`` unchanged from the stub;
  - return value in ``[0, 1]``;
  - the live ``shared.score.score`` symbol still physically loads from
    ``shared/`` (the inspect.getfile invariant the parity tests enforce
    on the local_runtime side);
  - missing / mismatched model artifacts raise a precise ``ScoreError``
    rather than crashing with whatever joblib decides to throw.

The parity tests proper live at
``local_runtime/tests/test_service.py::test_structural_parity_*`` and
are not duplicated here — those tests assert via the
``local_runtime.service`` import path, which is the one that matters
for the AWS-vs-local guarantee. The tests in this file are the
model session's own smoke for the swap.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Mapping

import pytest

import shared.score
from shared.features import FEATURE_NAMES
from shared.score import ScoreError, score


# A locked feature vector built from FEATURE_NAMES so a future rename
# of any feature key in the schema will fail this test loudly rather
# than silently producing a bad score.
_HEALTHY_FEATURES: Mapping[str, float] = {
    "vibration_amp": 0.30,
    "bearing_temp": 58.0,
    "motor_current": 4.0,
    "rpm": 1800.0,
    "vibration_amp_mean_5m": 0.30,
    "vibration_amp_std_5m": 0.05,
    "bearing_temp_mean_5m": 58.0,
    "bearing_temp_std_5m": 0.50,
}

_PRE_FAILURE_FEATURES: Mapping[str, float] = {
    "vibration_amp": 1.20,
    "bearing_temp": 70.0,
    "motor_current": 4.6,
    "rpm": 1600.0,
    "vibration_amp_mean_5m": 1.00,
    "vibration_amp_std_5m": 0.20,
    "bearing_temp_mean_5m": 68.0,
    "bearing_temp_std_5m": 1.50,
}


def _clear_cache():
    """Reset the module-level classifier cache between tests so
    monkeypatched paths take effect."""
    shared.score._clf = None


def test_score_signature_locked_to_mapping_to_float():
    """The signature is the mode-parity contract. If this fails the
    local_runtime callers stop compiling, and the structural-parity
    tests in local_runtime/tests/test_service.py break."""
    sig = inspect.signature(score)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "features"


def test_score_returns_float_in_unit_interval():
    """``predict_proba``-derived score is bounded by sklearn; we
    still assert because a future change that swaps the classifier
    family (e.g. to a regressor that returns logits) would break the
    downstream alert thresholds."""
    s = score(_HEALTHY_FEATURES)
    assert isinstance(s, float)
    assert 0.0 <= s <= 1.0


def test_score_orders_healthy_below_pre_failure():
    """The trained classifier must rank a high-vibration, lower-RPM
    sample above a low-vibration, near-setpoint sample. This is a
    minimum-bar sanity check that the model picked up the
    degradation signal at all — the AUC ≥ 0.85 acceptance criterion
    is enforced in the training run itself, not here."""
    s_healthy = score(_HEALTHY_FEATURES)
    s_pre_fail = score(_PRE_FAILURE_FEATURES)
    assert s_pre_fail > s_healthy, (
        f"pre-failure should score higher than healthy "
        f"(got {s_pre_fail} vs {s_healthy})"
    )


def test_score_loads_from_shared_directory():
    """Local enforcement of the inspect.getfile invariant. The
    canonical version of this test lives in
    local_runtime/tests/test_service.py — we duplicate the spirit
    here so the model session's own CI catches a vendor-copy of
    score.py inside model/ as well."""
    src = Path(inspect.getfile(score)).resolve()
    assert src.name == "score.py"
    assert src.parent.name == "shared", (
        f"score must load from shared/, got {src}"
    )


def test_missing_artifact_raises_score_error(tmp_path, monkeypatch):
    """If model.pkl is absent the failure mode is "your environment
    is misconfigured," not a bare FileNotFoundError. ScoreError
    inherits from RuntimeError so callers can catch it specifically
    without swallowing unrelated IO errors."""
    _clear_cache()
    monkeypatch.setattr(shared.score, "_ARTIFACT_PATH", tmp_path / "no-such.pkl")
    with pytest.raises(ScoreError, match="not found"):
        score(_HEALTHY_FEATURES)


def test_mismatched_feature_schema_raises_score_error(tmp_path, monkeypatch):
    """A pickle trained against a different feature schema is the
    classic silent-mode-parity-break. We detect it at load time and
    refuse to score."""
    _clear_cache()
    import joblib
    from sklearn.dummy import DummyClassifier
    import numpy as np

    clf = DummyClassifier(strategy="constant", constant=1)
    clf.fit(np.zeros((2, 8)), np.array([0, 1]))
    bad_bundle = {
        "model_version": "v0.0.0-bogus",
        "feature_names": ["foo", "bar"],  # deliberately wrong
        "auc_held_out": 0.99,
        "classifier": clf,
    }
    bad_path = tmp_path / "bad.pkl"
    joblib.dump(bad_bundle, bad_path)
    monkeypatch.setattr(shared.score, "_ARTIFACT_PATH", bad_path)
    with pytest.raises(ScoreError, match="feature_names"):
        score(_HEALTHY_FEATURES)


def test_missing_feature_key_raises_keyerror():
    """A caller that hands in a dict missing one of FEATURE_NAMES
    gets a KeyError carrying the missing name. The contract docstring
    promises this — surfacing the bug early is the point."""
    truncated = dict(_HEALTHY_FEATURES)
    del truncated["rpm"]
    with pytest.raises(KeyError, match="rpm"):
        score(truncated)


def test_feature_order_independent():
    """``score`` indexes by FEATURE_NAMES, not by dict iteration
    order, so reordering the input must not change the output. This
    is the contract that lets local_runtime hand in dicts directly
    without normalising key order."""
    reordered = {name: _HEALTHY_FEATURES[name] for name in reversed(FEATURE_NAMES)}
    assert score(reordered) == score(_HEALTHY_FEATURES)
