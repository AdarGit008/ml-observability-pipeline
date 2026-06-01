"""Unit tests for the training pipeline.

These tests use tiny pump counts so the suite runs in seconds. The
acceptance criterion (AUC ≥ 0.85 on held-out pumps from the real
30-pump corpus) is verified at training time inside ``main()`` —
this file proves the harness is wired correctly and produces
artifacts of the right shape.

ADR 0009 (2026-06-03) shrank the PSI surface: the reference
distribution now carries ``PSI_FEATURE_NAMES`` (4 entries), and the
model bundle still carries ``FEATURE_NAMES`` (8 entries — the scorer
input contract). ``write_artifacts`` lost its ``feature_names``
parameter because the two artifacts now diverge structurally;
``compute_reference_distribution`` slices the 8-column X matrix down
to the 4-column PSI surface before binning.
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
    OPERATIONAL_REFERENCE_PUMPS,
    OPERATIONAL_REFERENCE_TICKS_PER_PUMP,
    PSI_BIN_COUNT,
    PSI_FEATURE_NAMES,
    WINDOW_TICKS,
    _generate_operational_samples,
    _generate_pump_samples,
    _healthy_dwells,
    _operational_profiles,
    _training_profiles,
    compute_reference_distribution,
    fit_model,
    generate_training_data,
    write_artifacts,
)
from simulator.pump import DEFAULT_PROFILES, PumpState, StateProfile


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


def test_operational_profiles_returns_default_profiles_verbatim():
    """ADR 0008: the operational reference baseline must use
    ``DEFAULT_PROFILES`` exactly, with no overrides. A future "let me
    tweak HEALTHY ceiling for the reference" change has to update this
    test AND amend ADR 0008 — the asymmetry between training and
    reference profile dicts is load-bearing for the < 0.10 PSI
    acceptance."""
    profiles = _operational_profiles()
    for state, profile in DEFAULT_PROFILES.items():
        assert profiles[state] == profile, (
            f"_operational_profiles diverges from DEFAULT_PROFILES on "
            f"state {state} — ADR 0008 says no overrides for the "
            f"operational reference baseline."
        )


def test_operational_profiles_returns_fresh_dict():
    """Caller mutations must not leak into the module-level
    DEFAULT_PROFILES. The defensive copy is important because
    ``Pump.__init__`` does ``dict(DEFAULT_PROFILES)`` itself; if both
    layers were defensive on the same shared dict it'd be obvious, but
    a future refactor could remove one layer — pinning here makes the
    contract explicit at the helper boundary."""
    profiles = _operational_profiles()
    profiles[PumpState.HEALTHY] = StateProfile(
        rate_per_tick=99.0, ceiling=99.0, dwell_ticks=99
    )
    fresh = _operational_profiles()
    assert fresh[PumpState.HEALTHY] == DEFAULT_PROFILES[PumpState.HEALTHY]


def test_generate_operational_samples_shape_and_healthy_only():
    """ADR 0008 contract: n_pumps × ticks_per_pump rows × 8 features,
    every row HEALTHY. Uses small n=2 × ticks=300 for test speed
    (full-size 5 × 1800 takes ~13 s; this clears in <1 s).

    Note: the X matrix shape is (N, 8) — all 8 ``FEATURE_NAMES``
    columns — because the same matrix feeds both the scorer training
    corpus (when ``--reference-source=training``) and the PSI surface
    slicing. ``compute_reference_distribution`` slices to 4 columns
    at binning time (ADR 0009)."""
    n_pumps = 2
    ticks_per_pump = 300
    X = _generate_operational_samples(
        n_pumps=n_pumps, ticks_per_pump=ticks_per_pump, seed=0,
    )
    assert X.shape == (n_pumps * ticks_per_pump, len(FEATURE_NAMES))
    # FEATURE_NAMES order: vibration_amp, bearing_temp, motor_current, rpm.
    # ADR 0002 envelopes at d=0:
    #   vibration_amp = 0.3 + N(0, 0.05)            → mean ~ 0.3
    #   bearing_temp = ambient(22) + 0.02*rpm + ... → mean ~ 22 + 36 = 58
    #   motor_current = 4.0 + N(0, 0.1)             → mean ~ 4.0
    #   rpm = setpoint(1800) * (1-0) + N(0, 5)       → mean ~ 1800
    # Loose ±10 % envelopes — pinning the operational reference's
    # value range so a future simulator physics change shows up here.
    assert 0.25 < X[:, 0].mean() < 0.35
    assert 50.0 < X[:, 1].mean() < 65.0
    assert 3.9 < X[:, 2].mean() < 4.1
    assert 1780.0 < X[:, 3].mean() < 1820.0


def test_generate_operational_samples_deterministic():
    """Same seed → identical X. The session log's PSI-stability claim
    rests on this — a future debugger needs to reproduce the
    operational reference byte-for-byte from the seed."""
    X1 = _generate_operational_samples(n_pumps=2, ticks_per_pump=100, seed=0)
    X2 = _generate_operational_samples(n_pumps=2, ticks_per_pump=100, seed=0)
    np.testing.assert_array_equal(X1, X2)


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
    """Reference distribution covers exactly ``PSI_FEATURE_NAMES``
    (ADR 0009; was ``FEATURE_NAMES`` pre-ADR-0009), each key has 11
    bin edges + 10 bin counts (n_bins=10). Bin counts must sum to the
    training row count (no samples lost)."""
    X = np.random.default_rng(0).normal(size=(500, 8))
    ref = compute_reference_distribution(X, n_bins=PSI_BIN_COUNT)
    assert set(ref.keys()) == set(PSI_FEATURE_NAMES)
    # Rolling features explicitly NOT in the output (the asymmetry pin).
    rolling = {
        "vibration_amp_mean_5m",
        "vibration_amp_std_5m",
        "bearing_temp_mean_5m",
        "bearing_temp_std_5m",
    }
    assert rolling.isdisjoint(ref.keys()), (
        f"rolling features leaked into reference: {rolling & set(ref.keys())}"
    )
    for name, hist in ref.items():
        assert len(hist["bin_edges"]) == PSI_BIN_COUNT + 1
        assert len(hist["bin_counts"]) == PSI_BIN_COUNT
        assert sum(hist["bin_counts"]) == 500


def test_compute_reference_distribution_handles_constant_feature():
    """A constant column would normally produce zero-width bins
    (every quantile equal). The nextafter nudge must keep edges
    monotonically increasing so np.histogram + downstream PSI both
    survive.

    Sets ``vibration_amp`` (FEATURE_NAMES[0]) constant — that name IS
    in PSI_FEATURE_NAMES, so the constant-feature path is exercised.
    """
    X = np.zeros((200, 8))
    X[:, 0] = 4.2  # vibration_amp constant
    ref = compute_reference_distribution(X, n_bins=PSI_BIN_COUNT)
    edges = ref["vibration_amp"]["bin_edges"]
    # Strictly increasing (no duplicates after the nextafter pass)
    for i in range(1, len(edges)):
        assert edges[i] > edges[i - 1]


def test_write_artifacts_round_trip(tmp_path: Path, monkeypatch):
    """Artifacts land at the expected paths, can be re-read, and the
    reference JSON carries the version + feature_names metadata.

    Uses monkeypatch to redirect ARTIFACTS_DIR + MODEL_PATH into
    tmp_path so we don't clobber the committed artifacts. ``ref_path``
    is now an explicit parameter on ``write_artifacts`` (ADR 0008), so
    we just pass it instead of monkeypatching a module-level constant
    — cleaner than the pre-ADR-0008 setup.

    Feature-name asymmetry (ADR 0009): bundle.feature_names is
    ``FEATURE_NAMES`` (8) — scorer input. ref_doc.feature_names is
    ``PSI_FEATURE_NAMES`` (4) — drift surface."""
    import model.train as t

    monkeypatch.setattr(t, "ARTIFACTS_DIR", tmp_path)
    monkeypatch.setattr(t, "MODEL_PATH", tmp_path / "model.pkl")
    ref_path = tmp_path / "operational_reference_distribution.json"

    X = np.random.default_rng(0).normal(size=(200, 8))
    y = np.random.default_rng(0).integers(0, 2, size=200)
    clf = fit_model(X, y, seed=0)
    ref = compute_reference_distribution(X)
    write_artifacts(clf, ref, seed=0, auc=0.99, ref_path=ref_path)

    assert (tmp_path / "model.pkl").exists()
    assert ref_path.exists()

    # Reference JSON is human-readable + carries metadata.
    # ADR 0009: feature_names is the 4-element PSI surface.
    ref_doc = json.loads(ref_path.read_text())
    assert ref_doc["model_version"] == "v0.1.0-seed-0"
    assert ref_doc["feature_names"] == list(PSI_FEATURE_NAMES)
    assert ref_doc["n_bins"] == PSI_BIN_COUNT
    assert set(ref_doc["features"].keys()) == set(PSI_FEATURE_NAMES)

    # Model bundle has the 8-element scorer feature_names + classifier
    import joblib
    bundle = joblib.load(tmp_path / "model.pkl")
    assert bundle["model_version"] == "v0.1.0-seed-0"
    assert bundle["feature_names"] == list(FEATURE_NAMES)
    assert bundle["auc_held_out"] == pytest.approx(0.99)
    assert isinstance(bundle["classifier"], HistGradientBoostingClassifier)


def test_write_artifacts_default_ref_path_is_operational():
    """ADR 0008: the default ``ref_path`` on ``write_artifacts`` is
    ``OPERATIONAL_REFERENCE_PATH`` (filename
    ``operational_reference_distribution.json``). Pin the default so a
    future "let me make training the default again" regression has to
    update this test."""
    import inspect
    from model.train import OPERATIONAL_REFERENCE_PATH
    sig = inspect.signature(write_artifacts)
    default = sig.parameters["ref_path"].default
    assert default == OPERATIONAL_REFERENCE_PATH
    assert default.name == "operational_reference_distribution.json"
