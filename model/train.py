"""Train HistGradientBoostingClassifier on simulator output in fast-forward.

PLAN.md §2.3 spec: 4 raw + 4 rolling features, 48h-failure-horizon label,
``HistGradientBoostingClassifier(max_depth=5, max_iter=100)``, AUC ≥ 0.85
on held-out pumps. ADR 0006 is the long-form rationale for the choices
captured here.

Run: ``python -m model.train``  →  writes both artifacts in one command:

  - ``model/artifacts/model.pkl``                  (~200 KB, joblib)
  - ``model/artifacts/reference_distribution.json`` (PSI baseline,
    consumed by ``shared.drift.compute_psi`` once the drift session
    ships)

Both artifacts are deterministic given the same ``--seed`` (default 0).

Design notes (see ADR 0006 for the long-form):

- Fast-forward without MQTT/asyncio: training-data generation has no
  network-mode parity story to honour, so the asyncio+publisher layer
  in ``simulator.runner`` is dead weight here. We drive ``Pump.step()``
  directly in a synchronous loop. The *physical model* is identical
  (``simulator.pump.Pump``), which is the only parity that matters for
  training.

- Reuse ``shared.features.extract_features`` verbatim: see the module
  docstring of ``shared/features.py`` and ADR 0005. The training
  features MUST come from the same code as the live scorer or mode
  parity is theatre.

- Training-time DEGRADING dwell is overridden from the simulator's
  DEFAULT_PROFILES (200 ticks ≈ 13 min) to ``HORIZON_TICKS`` (86,400
  ≈ 48h). At default dwell the entire DEGRADING+FAILING cascade
  fits inside 13 minutes — the 48h prediction horizon then labels
  ~99 % of HEALTHY samples as "positive" without any feature signal
  to back it up. AUC collapses to ~0.5 on the smoke test that
  surfaced this. Slowing DEGRADING for *training data only* (the
  demo still uses DEFAULT_PROFILES) gives the rolling features a
  monotonic 48h ramp to learn from. The trade-off is recorded in
  ADR 0006 and the 2026-06-01 model session log.

- Per-pump variable HEALTHY dwell: with a fixed dwell every pump
  reaches FAILED at the same tick offset from start and the label
  is decidable from the tick index alone. Randomising staggers the
  failure times and gives label diversity. Range is bounded below
  by ``HORIZON_TICKS + WINDOW_TICKS`` to guarantee at least one
  negative sample per pump (the warmup window) and above by
  ``200_000`` so the total trajectory fits inside
  ``MAX_TICKS_PER_PUMP``.

- Short-circuit at failure_tick: once a pump is FAILED, every
  subsequent reading is constant-noise on degradation=1.0; sampling
  there produces trivially-positive labels that add no signal.
  Stopping the per-pump loop at the FAILED transition is both
  faster and cleaner.

- Train/test split by pump, not by time. Acceptance is "AUC ≥ 0.85
  on held-out pumps" so the held-out unit has to be a pump.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import deque
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import roc_auc_score

from shared.features import FEATURE_NAMES, extract_features
from simulator.pump import DEFAULT_PROFILES, Pump, PumpState, StateProfile


log = logging.getLogger(__name__)


# -- Constants ---------------------------------------------------------------

# 2-second tick (PLAN.md §2.2) means:
#   - 5-minute rolling window = 150 ticks  (matches local_runtime window_samples)
#   - 48h failure horizon     = 86_400 ticks
WINDOW_TICKS: int = 150
HORIZON_TICKS: int = 48 * 60 * 60 // 2  # 86_400

# Upper bound on per-pump tick count. ~6.9 days at 2s/tick — chosen so
# even the longest HEALTHY dwell (200k ticks) + DEGRADING (HORIZON_TICKS
# = 86_400) + FAILING (200) fits with margin.
MAX_TICKS_PER_PUMP: int = 300_000

# Training-data DEGRADING dwell. Override of DEFAULT_PROFILES — see
# module docstring + ADR 0006 for the rationale.
TRAINING_DEGRADING_DWELL_TICKS: int = HORIZON_TICKS  # 86_400

# Sample feature windows every N ticks. 30 ticks ≈ 1 row/min. Keeps the
# matrix at a manageable size and avoids overfitting to near-identical
# windows that neighbouring ticks would produce.
SAMPLE_EVERY_TICKS: int = 30

# Bins for the reference distribution per feature. PSI's standard
# practice is 10 equal-frequency bins (PLAN.md §2.7 + _interfaces.md).
PSI_BIN_COUNT: int = 10

# Artifact paths — resolved relative to the repo root so the script
# doesn't care about CWD.
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent
ARTIFACTS_DIR: Path = _REPO_ROOT / "model" / "artifacts"
MODEL_PATH: Path = ARTIFACTS_DIR / "model.pkl"
REFERENCE_PATH: Path = ARTIFACTS_DIR / "reference_distribution.json"


def _model_version(seed: int) -> str:
    """Tag stamped into both artifacts so a desync is detectable.

    Open question flagged for the Gemini packet (model.md "Feature
    versioning"): replace with a git-short-sha + date + seed scheme
    once we have a clean way to read git inside training.
    """
    return f"v0.1.0-seed-{seed}"


# -- Training-data generation -----------------------------------------------


def _training_profiles(healthy_dwell_ticks: int) -> dict[PumpState, StateProfile]:
    """Per-pump profiles for training-data generation.

    Differs from ``DEFAULT_PROFILES`` in two places:

    1. ``HEALTHY.dwell_ticks`` is set to ``healthy_dwell_ticks`` (the
       per-pump randomised value).
    2. ``DEGRADING.dwell_ticks`` is set to ``TRAINING_DEGRADING_DWELL_TICKS``
       and its ``rate_per_tick`` is scaled to preserve the same
       DEGRADING ceiling (0.30) across the stretched dwell. The
       smoke-test showed the DEFAULT_PROFILES 13-minute DEGRADING
       window doesn't span the 48h horizon, so the classifier had no
       learnable signal — AUC ≈ 0.5. Stretching DEGRADING to 48h
       aligns the physical signal with the label horizon. See
       ADR 0006.

    FAILING and FAILED keep ``DEFAULT_PROFILES``: 200 ticks of rapid
    accelerating wear is what we want as the hardest-positive sample
    regime.
    """
    profiles: dict[PumpState, StateProfile] = dict(DEFAULT_PROFILES)
    healthy_default = DEFAULT_PROFILES[PumpState.HEALTHY]
    profiles[PumpState.HEALTHY] = StateProfile(
        rate_per_tick=healthy_default.rate_per_tick,
        ceiling=healthy_default.ceiling,
        dwell_ticks=healthy_dwell_ticks,
    )
    degrading_default = DEFAULT_PROFILES[PumpState.DEGRADING]
    # Preserve total degradation rise (ceiling - healthy_ceiling = 0.25)
    # across the longer dwell. Original: 0.0015/tick × 200 ticks = 0.30.
    # Scaled: 0.25 / TRAINING_DEGRADING_DWELL_TICKS per tick.
    new_rate = (
        degrading_default.ceiling - healthy_default.ceiling
    ) / TRAINING_DEGRADING_DWELL_TICKS
    profiles[PumpState.DEGRADING] = StateProfile(
        rate_per_tick=new_rate,
        ceiling=degrading_default.ceiling,
        dwell_ticks=TRAINING_DEGRADING_DWELL_TICKS,
    )
    return profiles


def _healthy_dwells(n_pumps: int, rng: np.random.Generator) -> list[int]:
    """Sample per-pump HEALTHY dwell times spanning the label spectrum.

    With training DEGRADING dwell expanded to 48h, the per-pump label
    balance is governed by HEALTHY dwell: low dwell → mostly positives
    (failure horizon overlaps the trajectory from the start); high
    dwell → roughly even mix (healthy time exceeds the horizon,
    producing negatives).

    Range ``[HORIZON_TICKS + WINDOW_TICKS, 200_000]`` guarantees every
    pump has at least one negative sample (the warmup window) and
    every trajectory fits inside ``MAX_TICKS_PER_PUMP``.
    """
    low = HORIZON_TICKS + WINDOW_TICKS
    high = 200_000
    return [int(d) for d in rng.integers(low=low, high=high, size=n_pumps)]


def _generate_pump_samples(
    pump_id: str,
    *,
    healthy_dwell: int,
    seed: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Simulate one pump to failure and return (X, y) for its samples.

    ``X`` has shape (n_samples, 8) in ``FEATURE_NAMES`` column order.
    ``y`` has shape (n_samples,) with int labels in {0, 1}.

    Sampling cadence: one window every ``SAMPLE_EVERY_TICKS`` ticks,
    starting at ``WINDOW_TICKS`` (warmup) and stopping at the tick
    the pump transitions into ``PumpState.FAILED``. Labels are
    assigned in a single post-pass once the failure tick is known:
    ``y[i] = 1`` iff ``failure_tick - tick[i] <= HORIZON_TICKS``.
    """
    pump = Pump(
        pump_id,
        seed=seed,
        profiles=_training_profiles(healthy_dwell),
    )
    window: deque = deque(maxlen=WINDOW_TICKS)
    sample_ticks: list[int] = []
    sample_features: list[np.ndarray] = []
    failure_tick: int | None = None

    for t in range(MAX_TICKS_PER_PUMP):
        reading = pump.step()
        window.append(reading)
        if t >= WINDOW_TICKS and (t - WINDOW_TICKS) % SAMPLE_EVERY_TICKS == 0:
            feat_dict = extract_features(list(window))
            sample_ticks.append(t)
            sample_features.append(
                np.array(
                    [feat_dict[name] for name in FEATURE_NAMES],
                    dtype=np.float64,
                )
            )
        if pump.state is PumpState.FAILED:
            failure_tick = t
            break

    if failure_tick is None:
        raise RuntimeError(
            f"pump {pump_id!r} did not reach FAILED within "
            f"{MAX_TICKS_PER_PUMP} ticks (healthy_dwell={healthy_dwell})"
        )

    # Drop samples taken at or after failure — they're trivially positive.
    keep = [i for i, t in enumerate(sample_ticks) if t < failure_tick]
    if not keep:
        raise RuntimeError(
            f"pump {pump_id!r} produced no pre-failure samples "
            f"(failure_tick={failure_tick}, healthy_dwell={healthy_dwell})"
        )

    X = np.vstack([sample_features[i] for i in keep])
    y = np.array(
        [
            1 if (failure_tick - sample_ticks[i]) <= HORIZON_TICKS else 0
            for i in keep
        ],
        dtype=np.int64,
    )
    return X, y


def generate_training_data(
    n_pumps: int = 30,
    *,
    n_test_pumps: int = 6,
    seed: int = 0,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Generate ``(X_train, y_train, X_test, y_test)`` by-pump.

    The first ``n_test_pumps`` pumps (by sorted index) are held out;
    the remaining ``n_pumps - n_test_pumps`` train. Deterministic
    given the same seed.
    """
    if n_test_pumps <= 0 or n_test_pumps >= n_pumps:
        raise ValueError(
            f"n_test_pumps must be in (0, {n_pumps}), got {n_test_pumps}"
        )
    rng = np.random.default_rng(seed)
    dwells = _healthy_dwells(n_pumps, rng)

    train_X: list[np.ndarray] = []
    train_y: list[np.ndarray] = []
    test_X: list[np.ndarray] = []
    test_y: list[np.ndarray] = []
    for idx in range(n_pumps):
        pump_id = f"P-{idx:02d}"
        # Per-pump seed derived deterministically from the training
        # seed so a rerun reproduces the exact trajectories.
        per_pump_seed = seed * 10_000 + idx
        X, y = _generate_pump_samples(
            pump_id,
            healthy_dwell=dwells[idx],
            seed=per_pump_seed,
        )
        log.info(
            "pump %s: n=%d, pos=%d (%.0f%%), dwell=%d",
            pump_id, len(y), int(y.sum()), 100 * y.mean(), dwells[idx],
        )
        if idx < n_test_pumps:
            test_X.append(X)
            test_y.append(y)
        else:
            train_X.append(X)
            train_y.append(y)

    return (
        np.vstack(train_X),
        np.concatenate(train_y),
        np.vstack(test_X),
        np.concatenate(test_y),
    )


# -- Model fit + artifact emission ------------------------------------------


def fit_model(
    X_train: np.ndarray,
    y_train: np.ndarray,
    *,
    seed: int = 0,
) -> HistGradientBoostingClassifier:
    """Fit the HistGBT exactly per PLAN.md §2.3.

    No hyperparameter tuning. If AUC misses, the right move is an
    ADR amendment + a focused tuning pass, not a silent expansion
    of the search space here.
    """
    clf = HistGradientBoostingClassifier(
        max_depth=5,
        max_iter=100,
        random_state=seed,
    )
    clf.fit(X_train, y_train)
    return clf


def compute_reference_distribution(
    X_train: np.ndarray,
    *,
    n_bins: int = PSI_BIN_COUNT,
) -> dict[str, dict[str, list]]:
    """Per-feature 10-bin equal-frequency histograms from the train set.

    Equal-frequency (quantile) bins are the standard PSI shape: each
    bin holds ~10 % of the reference mass, so PSI's log(actual/expected)
    term is well-conditioned everywhere. The drift session's
    ``compute_psi`` consumes ``bin_edges`` directly and applies its
    own Laplace smoothing at compute time — keeping reference counts
    as raw frequencies here lets that decision live in one place.

    Bin edge handling: ``np.quantile`` can produce duplicate edges
    when a feature is near-constant (e.g., ``rpm`` for pumps that
    spend most of their time at setpoint). Duplicate edges make
    ``np.histogram`` assign all mass to one bin. We nudge duplicates
    forward via ``np.nextafter`` so every bin keeps a non-zero
    width — the drift session will treat a near-degenerate bin as a
    low-mass bin (which gets smoothed) rather than crash.

    Output (per feature):
        {"bin_edges": [n_bins + 1 floats], "bin_counts": [n_bins ints]}
    """
    reference: dict[str, dict[str, list]] = {}
    quantile_levels = np.linspace(0.0, 1.0, n_bins + 1)
    for i, name in enumerate(FEATURE_NAMES):
        column = X_train[:, i]
        edges = np.quantile(column, quantile_levels)
        for j in range(1, len(edges)):
            if edges[j] <= edges[j - 1]:
                edges[j] = np.nextafter(edges[j - 1], np.inf)
        counts, _ = np.histogram(column, bins=edges)
        reference[name] = {
            "bin_edges": [float(e) for e in edges],
            "bin_counts": [int(c) for c in counts],
        }
    return reference


def write_artifacts(
    clf: HistGradientBoostingClassifier,
    reference: dict,
    *,
    seed: int,
    auc: float,
    feature_names: Iterable[str] = FEATURE_NAMES,
) -> None:
    """Persist ``model.pkl`` and ``reference_distribution.json``.

    The model file bundles the classifier plus a small metadata
    header (version + feature names + held-out AUC) so the
    lambda_scorer's cold-start check can validate the artifact
    without re-deriving anything from training. Reference file
    carries the same ``model_version`` so a mismatch is detectable
    at drift compute time.
    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    version = _model_version(seed)
    bundle = {
        "model_version": version,
        "feature_names": list(feature_names),
        "auc_held_out": float(auc),
        "classifier": clf,
    }
    joblib.dump(bundle, MODEL_PATH)
    ref_with_meta = {
        "model_version": version,
        "feature_names": list(feature_names),
        "n_bins": PSI_BIN_COUNT,
        "features": reference,
    }
    REFERENCE_PATH.write_text(json.dumps(ref_with_meta, indent=2))
    log.info(
        "wrote %s (%.1f KB) + %s (%.1f KB) [version=%s, auc=%.3f]",
        MODEL_PATH,
        MODEL_PATH.stat().st_size / 1024,
        REFERENCE_PATH,
        REFERENCE_PATH.stat().st_size / 1024,
        version,
        auc,
    )


# -- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Train the pump-failure HistGBT + emit PSI reference."
    )
    parser.add_argument("--n-pumps", type=int, default=30)
    parser.add_argument("--n-test-pumps", type=int, default=6)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--min-auc",
        type=float,
        default=0.85,
        help="Fail with non-zero exit if held-out AUC is below this.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    log.info("generating training data (%d pumps, seed=%d)", args.n_pumps, args.seed)
    X_train, y_train, X_test, y_test = generate_training_data(
        n_pumps=args.n_pumps,
        n_test_pumps=args.n_test_pumps,
        seed=args.seed,
    )
    log.info(
        "train: %d rows, %.1f%% positive; test: %d rows, %.1f%% positive",
        len(y_train), 100 * y_train.mean(),
        len(y_test), 100 * y_test.mean(),
    )

    log.info("fitting HistGradientBoostingClassifier")
    clf = fit_model(X_train, y_train, seed=args.seed)

    y_score = clf.predict_proba(X_test)[:, 1]
    auc = float(roc_auc_score(y_test, y_score))
    log.info("held-out AUC: %.4f", auc)

    log.info("computing reference distribution (%d bins)", PSI_BIN_COUNT)
    reference = compute_reference_distribution(X_train, n_bins=PSI_BIN_COUNT)

    write_artifacts(clf, reference, seed=args.seed, auc=auc)

    if auc < args.min_auc:
        log.error("AUC %.4f below acceptance threshold %.2f", auc, args.min_auc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
