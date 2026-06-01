"""Unit tests for the training pipeline.

These tests use tiny pump counts so the suite runs in seconds. The
acceptance criterion (AUC ≥ 0.85 on held-out pumps from the real
30-pump corpus) is verified at training time inside ``main()`` —
this file proves the harness is wired correctly and produces
artifacts of the right shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
from sklearn.ensemble import HistGradientBoostingClassifier

from model.train import (
    FEATURE_NAMES,
    HORIZON_TICKS,
    PSI_BIN_COUNT,
    WINDOW_TICKS,
    _generate_pump_samples,
    _healthy_dwells,
    _training_profiles,
    compute_reference_distribution,
    fit_model,
    generate_training_data,
    write_artifacts,
)
from simulator.pump import DEFAULT_PROFILES, PumpState


def test_training_profiles_overrides_only_healthy_dwell_and_degrading():
    """The training override touches HEALTHY.dwell + DEGRADING.{rate,dwell}
    only — everything else stays at DEFAULT_PROFILES so the failure
    cascade still pins to 1.0 at the right moment."""
    profiles = _training_profiles(healthy_dwell_ticks=100_000)
    assert profiles[PumpState.HEALTHY].dwell_ticks == 100_000
    # rate_per_tick + ceiling unchanged on HEALTHY
    assert profiles[PumpState.HEALTHY].rate_per_tick == DEFAULT_PROFILES[PumpState.HEALTHY].rate_per_tick
    assert profiles[PumpState.HEALTHY].ceiling == DEFAULT_PROFILES[PumpState.HEALTHY].ceiling
    # DEGRADING ceiling preserved, dwell stretched, rate scaled
    assert profiles[PumpState.DEGRADING].ceiling == DEFAULT_PROFILES[PumpState.DEGRADING].ceiling
    assert profiles[PumpState.DEGRADING].dwell_ticks == HORIZON_TICKS
    expected_rate = (0.30 - 0.05) / HORIZON_TICKS
    assert profiles[PumpState.DEGRADING].rate_per_tick == pytest.approx(expected_rate)
    # FAILING + FAILED untouched
    assert profiles[PumpState.FAILING] == DEFAULT_PROFILES[PumpState.FAILING]
    assert profiles[PumpState.FAILED] == DEFAULT_PROFILES[PumpState.FAILED]


def test_healthy_dwells_within_designed_range():
    """Dwell sampler must produce values in the
    [HORIZON_TICKS + WINDOW_TICKS, 200_000) range, deterministically."""
    rng = np.random.default_rng(0)
    dwells = _healthy_dwells(30, rng)
    assert len(dwells) == 30
    for d in dwells:
        assert HORIZON_TICKS + WINDOW_TICKS <= d < 200_000


def test_pump_samples_shape_and_labels():
    """For one pump we should get an (n, 8) feature matrix in
    FEATURE_NAMES order and labels in {0, 1} with at least one of
    each (low healthy_dwell that just clears the negative-sample
    floor)."""
    X, y = _generate_pump_samples(
        "P-00",
        healthy_dwell=HORIZON_TICKS + WINDOW_TICKS + 60,  # 1-2 negative samples
        seed=0,
    )
    assert X.ndim == 2 and X.shape[1] == 8
    assert X.shape[0] == y.shape[0]
    assert set(y.tolist()).issubset({0, 1})
    # The lowest-dwell pump should give us at least 1 positive.
    assert y.sum() >= 1


def test_generate_training_data_by_pump_split():
    """First n_test_pumps held out; train/test are disjoint by pump.
    We use a tiny 4-pump corpus to keep this test fast (<10s)."""
    X_tr, y_tr, X_te, y_te = generate_training_data(
        n_pumps=4, n_test_pumps=2, seed=0,
    )
    # Both sides non-empty, 8-feature columns.
    assert X_tr.shape[1] == X_te.shape[1] == 8
    assert X_tr.shape[0] > 0 and X_te.shape[0] > 0
    # Label balance: with 4 pumps split 2/2 we don't pin exact ratios,
    # but both sides must have at least one positive (sanity).
    assert y_tr.sum() > 0 and y_te.sum() > 0


def test_fit_model_returns_histgbt_with_locked_hyperparams():
    """PLAN.md §2.3 pins max_depth=5, max_iter=100. Lock these so a
    future "let me just tune real quick" PR has to update this test
    AND write the ADR amendment that justifies it."""
    X = np.array([[0.3, 60.0, 4.0, 1800.0, 0.3, 0.05, 60.0, 0.5]] * 100
                 + [[1.5, 70.0, 5.0, 1500.0, 1.5, 0.3, 70.0, 1.5]] * 100)
    y = np.array([0] * 100 + [1] * 100)
    clf = fit_model(X, y, seed=0)
    assert isinstance(clf, HistGradientBoostingClassifier)
    assert clf.max_depth == 5
    assert clf.max_iter == 100
    # Sklearn keeps the random_state on the estimator
    assert clf.random_state == 0


def test_compute_reference_distribution_per_feature_shape():
    """Reference distribution must have all 8 feature keys, each with
    11 bin edges + 10 bin counts (n_bins=10). Bin counts must sum to
    the training row count (no samples lost)."""
    X = np.random.default_rng(0).normal(size=(500, 8))
    ref = compute_reference_distribution(X, n_bins=PSI_BIN_COUNT)
    assert set(ref.keys()) == set(FEATURE_NAMES)
    for name, hist in ref.items():
        assert len(hist["bin_edges"]) == PSI_BIN_COUNT + 1
        assert len(hist["bin_counts"]) == PSI_BIN_COUNT
        assert sum(hist["bin_counts"]) == 500


def test_compute_reference_distribution_handles_constant_feature():
    """A constant column would normally produce zero-width bins
    (every quantile equal). The nextafter nudge must keep edges
    monotonically increasing so np.histogram + downstream PSI both
    survive."""
    X = np.zeros((200, 8))
    X[:, 0] = 4.2  # constant column
    ref = compute_reference_distribution(X, n_bins=PSI_BIN_COUNT)
    edges = ref[FEATURE_NAMES[0]]["bin_edges"]
    # Strictly increasing (no duplicates after the nextafter pass)
    for i in range(1, len(edges)):
        assert edges[i] > edges[i - 1]


def test_write_artifacts_round_trip(tmp_path: Path, monkeypatch):
    """Artifacts land at the expected paths, can be re-read, and the
    reference JSON carries the version + feature_names metadata.
    Uses monkeypatch to redirect ARTIFACTS_DIR into tmp_path so we
    don't clobber the committed artifacts."""
    import model.train as t

    monkeypatch.setattr(t, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(t, "MODEL_PATH", tmp_path / "model.pkl")
    monkeypatch.setattr(t, "REFERENCE_PATH", tmp_path / "reference_distribution.json")

    X = np.random.default_rng(0).normal(size=(200, 8))
    y = np.random.default_rng(0).integers(0, 2, size=200)
    clf = fit_model(X, y, seed=0)
    ref = compute_reference_distribution(X)
    write_artifacts(clf, ref, seed=0, auc=0.99)

    assert (tmp_path / "model.pkl").exists()
    assert (tmp_path / "reference_distribution.json").exists()

    # Reference JSON is human-readable + carries metadata
    ref_doc = json.loads((tmp_path / "reference_distribution.json").read_text())
    assert ref_doc["model_version"] == "v0.1.0-seed-0"
    assert ref_doc["feature_names"] == list(FEATURE_NAMES)
    assert ref_doc["n_bins"] == PSI_BIN_COUNT
    assert set(ref_doc["features"].keys()) == set(FEATURE_NAMES)

    # Model bundle has the same metadata + classifier
    import joblib
    bundle = joblib.load(tmp_path / "model.pkl")
    assert bundle["model_version"] == "v0.1.0-seed-0"
    assert bundle["feature_names"] == list(FEATURE_NAMES)
    assert bundle["auc_held_out"] == pytest.approx(0.99)
    assert isinstance(bundle["classifier"], HistGradientBoostingClassifier)
