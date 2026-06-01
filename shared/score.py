"""Scoring — wraps the trained HistGradientBoostingClassifier.

Locked at the function signature level: both ``local_runtime`` and the
future ``lambda_scorer`` call ``score(features) -> float in [0, 1]``
without caring how the score is produced. The model session
(2026-06-01) swapped this from a deterministic stub to a real
``predict_proba`` call against ``model/artifacts/model.pkl``; the
mode-parity tests (``test_structural_parity_score_loads_from_shared``)
verify the function physically loads from ``shared/`` and continue to
pass — the substitution happens inside this file, not at the import
boundary.

Cold-start cost: the classifier is loaded once via a module-level
cache the first time ``score()`` is called. Lazy (not eager at import
time) so:

  1. Tests that don't exercise scoring can run without the pickle on
     disk — important during dev when the artifact may legitimately
     be absent.
  2. Lambda cold-start cost lands on the first invocation, not on
     ``import shared.score`` — same total time, more visible in
     CloudWatch's reported init duration.

The bundle written by ``model.train.write_artifacts`` is a small dict
{model_version, feature_names, auc_held_out, classifier}. We assert
the feature names embedded in the bundle exactly match
``FEATURE_NAMES`` at load time — a mismatch means the model was
trained against a different feature set than the live scorer
extracts, which is a silent-divergence trap we want to catch loudly.

Feature ordering: ``HistGradientBoostingClassifier.predict_proba``
expects a 2-D array whose columns line up with the feature order at
``fit`` time. We materialise the feature vector by iterating
``FEATURE_NAMES`` in order (same source the training script uses),
so as long as both sides import ``shared.features.FEATURE_NAMES``
the columns line up.
"""

from __future__ import annotations

from pathlib import Path
from threading import Lock
from typing import Mapping

import joblib
import numpy as np

from shared.features import FEATURE_NAMES


_ARTIFACT_PATH: Path = Path(__file__).resolve().parent.parent / "model" / "artifacts" / "model.pkl"

# Module-level cache. Loaded lazily on first ``score()`` call. The
# Lock guards against the (admittedly rare) case where two threads in
# the same Lambda container race the first invocation; without it
# we'd risk loading the pickle twice or — worse — serving from a
# half-initialised cache. The single-load fast path after warmup is
# branch-free.
_clf = None  # type: ignore[var-annotated]
_load_lock = Lock()


class ScoreError(RuntimeError):
    """Raised when the model artifact is missing, malformed, or its
    embedded feature names don't match ``FEATURE_NAMES``.

    Subclass of ``RuntimeError`` rather than ``ValueError`` because
    the failure mode is "the environment is wrong" (no pickle, wrong
    pickle), not "the caller passed bad data."
    """


def _load_classifier():
    """Load and cache the bundled classifier from disk.

    Validates the artifact's embedded ``feature_names`` against the
    live ``FEATURE_NAMES`` tuple. A mismatch means the model was
    trained against a different feature schema and using it would
    silently break mode parity — raise loudly instead.
    """
    global _clf
    if _clf is not None:
        return _clf
    with _load_lock:
        if _clf is not None:  # double-checked under the lock
            return _clf
        if not _ARTIFACT_PATH.exists():
            raise ScoreError(
                f"model artifact not found at {_ARTIFACT_PATH}. "
                "Run `python -m model.train` to produce it."
            )
        bundle = joblib.load(_ARTIFACT_PATH)
        if not isinstance(bundle, dict) or "classifier" not in bundle:
            raise ScoreError(
                f"model artifact at {_ARTIFACT_PATH} is not a "
                "bundle dict — re-run `python -m model.train`."
            )
        embedded = tuple(bundle.get("feature_names", ()))
        if embedded != FEATURE_NAMES:
            raise ScoreError(
                "model artifact feature_names do not match "
                f"shared.features.FEATURE_NAMES.\n"
                f"  artifact: {embedded}\n"
                f"  live:     {FEATURE_NAMES}\n"
                "Re-run `python -m model.train` against the current "
                "feature schema."
            )
        _clf = bundle["classifier"]
        return _clf


def score(features: Mapping[str, float]) -> float:
    """Return ``P(failure within 48h)`` in ``[0, 1]``.

    Args:
        features: dict matching the schema in ``shared.features`` —
            8 keys named per ``FEATURE_NAMES``. Order in the dict
            doesn't matter; the keys do.

    Returns:
        float in ``[0, 1]``. Bounded by sklearn's own ``predict_proba``
        contract — no extra clamping needed because the classifier
        guarantees probabilities sum to 1 over classes and each
        probability lies in [0, 1].

    Raises:
        KeyError: a key in ``FEATURE_NAMES`` is missing from
            ``features`` (raised by the dict lookup, surfaces as a
            standard KeyError with the missing name).
        ScoreError: the model artifact is missing or its embedded
            feature schema doesn't match ``FEATURE_NAMES``.
    """
    clf = _load_classifier()
    # FEATURE_NAMES order matches the training-time column order
    # because model.train.py and shared.features both import from
    # this module. A KeyError here means the caller's dict is
    # missing a feature — surfaces with the missing name.
    X = np.array([[features[name] for name in FEATURE_NAMES]], dtype=np.float64)
    proba = clf.predict_proba(X)
    # Binary classifier: column 1 is "positive class" = failure
    # within 48h. ``HistGradientBoostingClassifier.predict_proba``
    # always returns shape (n_samples, n_classes); we take row 0,
    # column 1.
    return float(proba[0, 1])
