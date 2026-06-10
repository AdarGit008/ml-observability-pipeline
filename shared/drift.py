"""Real PSI implementation -- the mode-parity drift contract.

Drift session 2026-06-01 swapped the stub for the binned, Laplace-
smoothed PSI computation specified in PLAN.md s2.7. Both
``local_runtime`` and ``lambda_scorer`` import
``compute_psi`` from here as peers (ADR 0005); the structural-parity
test ``test_structural_parity_compute_psi_loads_from_shared`` continues
to pin the load path.

PSI feature surface (ADR 0009, 2026-06-03): ``compute_psi`` iterates
``shared.features.PSI_FEATURE_NAMES`` — a STRICT SUBSET of
``FEATURE_NAMES`` containing only the four raw sensor signals
(``vibration_amp``, ``bearing_temp``, ``motor_current``, ``rpm``).
The four rolling features (``*_mean_5m``, ``*_std_5m``) are scorer
inputs only; they're excluded from PSI because consecutive
rolling-window samples share 149 of 150 underlying readings,
violating PSI's IID assumption and producing 0.10–0.40
autocorrelation noise on healthy fleets (ADR 0008 §Negative
measurement; ADR 0009 §Decision). The on-disk reference
distribution's ``feature_names`` list and ``features`` map cover
exactly ``PSI_FEATURE_NAMES``; ``load_reference`` validates against
it on load.

Formula (per PLAN.md s2.7):

    PSI = sum_b (actual_pct[b] - expected_pct[b]) * ln(actual_pct[b] / expected_pct[b])

with Laplace add-alpha smoothing on both sides:

    actual_pct[b]   = (actual_count[b]   + alpha) / sum_i (actual_count[i]   + alpha)
    expected_pct[b] = (expected_count[b] + alpha) / sum_i (expected_count[i] + alpha)

alpha is a module constant (``LAPLACE_ALPHA``, justified in ADR 0007
section "Laplace smoothing alpha = 1.0").

Thresholds (per ``context/_interfaces.md`` section "PSI parameters" and
PLAN.md s2.7): < 0.10 stable; 0.10-0.25 warning; > 0.25 significant.
Thresholds aren't enforced here -- that's the alerts layer's call --
but the formula is calibrated to produce values in those bands for the
distribution shifts the project's three scenarios inject.

API split (Gemini Q2 of the 2026-06-01 review):

- ``load_reference(ref_path, model_path)`` is the I/O entry point.
  Called once during service initialisation (local) or per Lambda
  cold start. Returns the deserialised reference dict; the caller
  stores it.
- ``compute_psi(window_features, reference)`` is a pure function.
  The reference is now an explicit, required argument. No side
  effects, no module-level state, no implicit disk read. Tests pass
  synthetic references directly; the live service passes the dict
  returned by ``load_reference``.

The pre-Gemini design had ``compute_psi`` lazy-loading the reference
on first call when ``reference=None``, with a module-level cache and
a ``_reset_reference_cache()`` test helper. Gemini flagged the
cache-and-test-helper combination as a debt signal; this refactor
makes the I/O explicit and removes the shared module-level state.
ADR 0007 section Addendum 2026-06-01 Q2 carries the long form.

Model/reference version match (ADR 0007 section "Model/reference
version match"): inside ``load_reference``, if ``model.pkl`` is also
on disk, the ``model_version`` fields from both artifacts are
compared. A mismatch raises ``DriftError`` so the lambda_scorer
cold-start fails fast rather than silently scoring against one
version and PSI-ing against another. If ``model.pkl`` is absent
(dev environment where only the reference is shipped -- e.g., a
drift-only Lambda layer for the fleet-PSI EventBridge job), the
check is skipped so drift can still load.

Dependency ceiling: standard library + ``numpy``. ``joblib`` is
lazy-imported inside the version-check branch so a "PSI without
sklearn" environment stays importable. Per ``context/drift.md``
invariants.
"""

from __future__ import annotations

import json
import math
import pickle
from pathlib import Path
from typing import Iterable, Mapping, Optional, Sized

import numpy as np

from shared.features import PSI_FEATURE_NAMES


# Repo-relative default artifact paths. Same convention as
# ``shared/score.py::_ARTIFACT_PATH`` -- resolved once at import.
# Callers can override via ``load_reference(ref_path=..., model_path=...)``
# for testing or for non-default deployment layouts.
_DEFAULT_REF_PATH: Path = Path(__file__).resolve().parent.parent / "model" / "artifacts" / "operational_reference_distribution.json"
_DEFAULT_MODEL_PATH: Path = Path(__file__).resolve().parent.parent / "model" / "artifacts" / "model.pkl"

# Laplace add-alpha smoothing constant. alpha = 1.0 is the standard
# Laplace prior -- equivalent to assuming one phantom observation per
# bin. On a 1800-sample window (1 hour at 2 s/tick), an empty actual
# bin against a 10%-expected bin contributes ~ 0.52 to PSI --
# meaningful but not alarmist. See ADR 0007 for alternatives.
LAPLACE_ALPHA: float = 1.0


# Minimum window depth before PSI is allowed to ARM an alert (ADR 0017).
# PSI on a sub-warmup window is a small-sample artifact, not drift: with
# 10 equal-frequency reference bins and Laplace alpha = 1.0 (ADR 0007),
# a handful of readings concentrate all actual mass in one or two bins
# and the add-alpha prior dominates the ratio -- pushing max-PSI past
# 0.25 even on a HEALTHY fleet (observed 2026-06-07 first live apply:
# 9 of 14 pumps fired alert_flag within minute 1; scores all <= 0.02,
# far below the 0.7 line). 150 samples = the 5-minute scoring window
# (lambda_scorer.handler.WINDOW_SAMPLES at the 2 s tick) -- ~15
# observations per bin, so the Laplace prior becomes a ~6% correction
# rather than the dominant term. On a FULL window a healthy demo fleet
# already reads STABLE (ADR 0008 operational reference), so this gate
# suppresses ONLY the cold-start storm and never real drift. Consumed
# by ``psi_is_armed``; see ADR 0017 for the threshold derivation.
PSI_MIN_SAMPLES: int = 150


# PSI "significant shift" threshold (ADR 0007 bands: <0.10 stable /
# 0.10-0.25 warning / >0.25 significant). Lives here, in the drift
# surface, so the alert THRESHOLD travels with the warmup gate as a
# single shared definition -- a future fleet-PSI Lambda imports one
# function (``psi_alert_should_fire``) and cannot diverge on either the
# boundary or the threshold (DeepSeek review 2026-06-10 §1; north star
# #6). ``lambda_scorer.handler.PSI_ALERT_THRESHOLD`` aliases this.
PSI_SIGNIFICANT_THRESHOLD: float = 0.25


class DriftError(RuntimeError):
    """Raised when the reference distribution can't be loaded, is
    malformed, or its ``model_version`` doesn't match ``model.pkl``.

    Sibling of ``shared.score.ScoreError`` -- same posture: "the
    environment is wrong" (no reference file, malformed JSON,
    model/reference desync), not "the caller passed bad data."
    ``RuntimeError`` subclass so caller code that catches
    ``ValueError`` doesn't accidentally swallow it.
    """


def load_reference(
    ref_path: Optional[Path] = None,
    model_path: Optional[Path] = None,
) -> dict:
    """Read, validate, and return the reference distribution dict.

    Single I/O entry point for the drift module. Called once per
    process lifetime by ``local_runtime/service.py``'s ``ScorerService.
    __init__`` (and ``lambda_scorer/handler.py``'s cold-start path);
    the caller stores the result and passes it explicitly to
    ``compute_psi`` on each invocation.

    Validation steps:

    1. File exists at ``ref_path``. Loud ``DriftError`` otherwise.
    2. JSON parses. Loud ``DriftError`` on syntax error.
    3. Top-level shape: dict with ``features`` key.
    4. ``feature_names`` tuple equals ``PSI_FEATURE_NAMES`` (ADR 0009;
       a STRICT SUBSET of ``FEATURE_NAMES``). A mismatch means the
       reference was built against a different PSI surface --
       typically a pre-ADR-0009 reference that still embeds all 8
       feature names -- and loading it would silently re-introduce
       the autocorrelation noise problem ADR 0009 closes. The error
       message points at the rebuild command.
    5. If ``model_path`` exists on disk: ``model_version`` fields
       match between the two artifacts. A mismatch means the model
       and reference are out of sync (one was rebuilt without the
       other) and PSI would compare actuals from one model against
       quantiles from another. Loud ``DriftError`` -- re-run
       ``python -m model.train``.

    Args:
        ref_path: path to the reference JSON. Defaults to
            ``model/artifacts/operational_reference_distribution.json`` relative
            to the repo root.
        model_path: path to ``model.pkl`` for the version-match
            check. Defaults to ``model/artifacts/model.pkl``. If the
            file is absent, the version check is skipped.

    Returns:
        The deserialised reference dict (with ``features``,
        ``feature_names``, ``model_version``, ``n_bins`` keys).

    Raises:
        DriftError: any of the validation steps above fail.
    """
    ref_path = ref_path or _DEFAULT_REF_PATH
    model_path = model_path or _DEFAULT_MODEL_PATH

    if not ref_path.exists():
        raise DriftError(
            f"reference distribution not found at {ref_path}. "
            "Run `python -m model.train` to produce it."
        )
    try:
        ref = json.loads(ref_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise DriftError(
            f"reference distribution at {ref_path} is not valid JSON: {e}"
        ) from e
    if not isinstance(ref, dict) or "features" not in ref:
        raise DriftError(
            f"reference distribution at {ref_path} is missing the "
            "'features' top-level key -- re-run `python -m model.train`."
        )
    ref_feature_names = tuple(ref.get("feature_names", ()))
    if ref_feature_names != PSI_FEATURE_NAMES:
        raise DriftError(
            "reference distribution feature_names don't match "
            f"shared.features.PSI_FEATURE_NAMES (ADR 0009).\n"
            f"  reference: {ref_feature_names}\n"
            f"  live:      {PSI_FEATURE_NAMES}\n"
            "A pre-ADR-0009 reference embeds the 8-element scorer "
            "feature list; rebuild via `python -m model.train` to "
            "produce the 4-feature PSI surface."
        )
    _check_model_version_match(ref, model_path)
    return ref


def _check_model_version_match(ref: dict, model_path: Path) -> None:
    """Compare ``model_version`` between reference JSON and ``model.pkl``.

    **Why we skip when ``model.pkl`` is absent:** drift can legitimately
    be deployed without the scorer -- e.g., the fleet-PSI EventBridge
    Lambda only needs the reference distribution to compute aggregate
    drift across pumps; it never calls ``shared.score.score``. A
    "drift without sklearn / without the model" deployment is a
    supported configuration. Refusing to load PSI just because
    ``model.pkl`` happens to be absent would block that deployment
    pattern for no safety benefit.

    When ``model.pkl`` IS present, the cross-check protects against
    a partial redeploy where one of the two artifacts was rebuilt
    without the other -- in that case scoring would use one
    model_version and PSI would baseline against another, which is a
    silent parity violation.

    ``joblib`` is lazy-imported inside this branch so the absent-
    model case keeps the drift module's import surface narrow
    (numpy + stdlib only, per ``context/drift.md``).
    """
    if not model_path.exists():
        return
    ref_version = ref.get("model_version")
    try:
        import joblib  # noqa: PLC0415 -- lazy: keep narrow dep surface
        bundle = joblib.load(model_path)
    except (OSError, EOFError, pickle.UnpicklingError, ValueError, KeyError, AttributeError) as e:
        # Specific failure modes for "model.pkl is present but
        # unreadable" (Gemini Q4 of the 2026-06-01 review -- narrowed
        # from a previous catch-Exception). Each of these means the
        # artifact is corrupt / partial / wrong-pickle-version:
        #   - OSError: file IO failure (truncated file, permission)
        #   - EOFError: pickle stream ended early
        #   - pickle.UnpicklingError: pickle stream malformed
        #   - ValueError: joblib raises this on some format mismatches
        #   - KeyError / AttributeError: pickle references missing
        #     classes (sklearn-version mismatch, missing module)
        # Convert to DriftError so the operator gets a single
        # actionable error class regardless of which corruption mode
        # was hit. RuntimeError-family is intentionally NOT caught --
        # something like KeyboardInterrupt or MemoryError should
        # propagate.
        raise DriftError(
            f"reference is at {ref.get('model_version', '<unknown>')} but "
            f"model.pkl at {model_path} failed to load for the "
            f"version-match check: {type(e).__name__}: {e}. "
            "Re-run `python -m model.train`."
        ) from e
    model_version = bundle.get("model_version") if isinstance(bundle, dict) else None
    if model_version is None:
        # Model bundle is shaped wrong -- shared.score's own validation
        # will catch it at score time. Don't second-guess here.
        return
    if ref_version != model_version:
        raise DriftError(
            "model/reference version mismatch -- "
            f"model.pkl model_version={model_version!r}, "
            f"operational_reference_distribution.json model_version={ref_version!r}. "
            "Re-run `python -m model.train` so both artifacts share a version."
        )


def psi_is_armed(window: Sized) -> bool:
    """Return True when ``window`` holds enough samples for a PSI breach
    to ARM an alert (``len(window) >= PSI_MIN_SAMPLES``), per ADR 0017.

    Pure, dependency-free predicate -- the single shared definition of
    "this window is warm enough to trust a PSI breach." This is the
    parity-correct home for the warmup threshold (ADR 0005): the
    per-pump ``lambda_scorer`` hot path consults it before arming an
    SNS alert, and the future fleet-PSI EventBridge Lambda will consult
    the SAME constant so the two AWS alert sites cannot diverge on the
    warmup boundary (north star #6 -- local/AWS divergence is a bug or
    an ADR). ``local_runtime`` has no alert site -- it writes PSI to
    InfluxDB for visualisation only -- so it does not call this; there
    is therefore no local/AWS PSI-value divergence to reconcile.

    The gate is on the ALERT, not the computation. ``compute_psi``
    still returns real PSI for every window, cold or warm, so the
    dashboard shows the metric warming up; only whether a breach is
    allowed to page someone is gated. Decoupling "PSI is computed"
    from "PSI is trustworthy enough to alert" is the whole point
    (ADR 0017 Decision).

    Args:
        window: the same PSI window the caller passes to
            ``compute_psi`` (any sized iterable -- the lambda hot path
            passes the DynamoDB-reconstructed reading list). Only its
            length is consulted; contents are irrelevant to arming.

    Returns:
        ``True`` iff ``len(window) >= PSI_MIN_SAMPLES``.
    """
    return len(window) >= PSI_MIN_SAMPLES


def psi_alert_should_fire(
    window: Sized,
    psi: Mapping[str, float],
    threshold: float = PSI_SIGNIFICANT_THRESHOLD,
) -> bool:
    """Composite PSI alert-arming decision: window warm AND breaching.

    The single shared definition of "should a PSI breach arm an alert."
    It encodes BOTH the warmup gate (``psi_is_armed``) AND the
    significant-shift threshold, so every alert site -- ``lambda_scorer``
    today, the future fleet-PSI EventBridge Lambda tomorrow -- imports
    ONE function and cannot diverge on either the warmup boundary or the
    threshold (ADR 0017; DeepSeek review 2026-06-10 §1 hardened this
    from a bare predicate to the full decision). Keeping just
    ``PSI_MIN_SAMPLES`` shared but the threshold in ``lambda_scorer``
    would have left a future Lambda free to pick a different threshold
    or skip the gate -- a parity hole closed by colocating the whole
    decision (north star #6).

    Pure and side-effect free. Gates the ALERT, not the computation:
    ``compute_psi`` still runs for every window and ``latest_psi`` is
    still written, so the dashboard shows PSI warming up regardless of
    this return value.

    Args:
        window: the PSI window the caller passed to ``compute_psi``
            (any sized iterable). Only its length is consulted.
        psi: the per-feature PSI dict ``compute_psi`` returned.
        threshold: significant-shift cutoff; defaults to
            ``PSI_SIGNIFICANT_THRESHOLD`` (0.25, ADR 0007).

    Returns:
        ``True`` iff the window is warm (``psi_is_armed``) AND the max
        per-feature PSI exceeds ``threshold``. An empty ``psi`` dict
        returns ``False`` (no surface to breach).
    """
    if not psi:
        return False
    return psi_is_armed(window) and max(psi.values()) > threshold


def compute_psi(
    window_features: Iterable[Mapping[str, float]],
    reference: Mapping[str, object],
) -> dict[str, float]:
    """Return per-feature PSI for the rolling window of feature dicts.

    Pure function -- no I/O, no module-level state. The reference is
    a required argument; the caller is responsible for loading it via
    ``load_reference()`` and passing it on each call (or injecting a
    synthetic reference in tests).

    Per-feature pipeline:

    1. Extract the feature column from the window (list of floats).
    2. Clip each value to the reference's outermost bin edges.
       ``np.histogram`` would silently drop out-of-range values
       otherwise, which would shrink the actual total and skew the
       percentages. Clipping treats out-of-range as "lands in the
       closest bin" -- the right behaviour for streaming data where
       the reference's span comes from training-time min/max.
    3. Bin via ``np.histogram(values, bins=bin_edges)``.
    4. Laplace add-alpha smooth both the actual and reference counts
       (alpha = ``LAPLACE_ALPHA``).
    5. Normalize to percentages.
    6. Sum the per-bin PSI contributions per the formula above.

    Args:
        window_features: ordered list of feature dicts (the rolling
            1-hour PSI window -- service.py owns the maintenance of
            this window; the function does no time-based filtering).
            Each dict matches ``shared.features.FEATURE_NAMES`` (the
            scorer surface). PSI iterates ``PSI_FEATURE_NAMES`` (the
            drift surface, a subset) and ignores the rolling-feature
            keys -- they're scorer inputs only per ADR 0009.
        reference: the reference distribution dict returned by
            ``load_reference``. Required. Its ``features`` map covers
            ``PSI_FEATURE_NAMES`` (validated by ``load_reference``).

    Returns:
        Dict keyed by ``PSI_FEATURE_NAMES`` (4 floats per ADR 0009;
        was 8 pre-ADR-0009). Empty window returns a dict of zeros
        (defensive -- service.py's warm-up appends a feature dict
        before calling, so this branch is unreachable in practice
        but tolerated rather than raising).

    Raises:
        KeyError: a window entry is missing one of the
            ``PSI_FEATURE_NAMES`` keys, or the ``reference`` dict is
            missing the expected per-feature shape. Same posture as
            ``extract_features``: surfaces with the missing name.
    """
    # The reference's "features" map: feature_name -> {bin_edges, bin_counts}.
    ref_features = reference["features"]  # type: ignore[index]

    readings = list(window_features)
    if not readings:
        # Defensive zero-return. ``service.py`` warm-up never actually
        # hits this (it appends a feature dict before calling), but
        # the function tolerates it rather than raising -- keeps the
        # interface contract symmetric with the score side, where a
        # 1-element window is meaningful.
        return {name: 0.0 for name in PSI_FEATURE_NAMES}

    result: dict[str, float] = {}
    for name in PSI_FEATURE_NAMES:
        feat_ref = ref_features[name]  # type: ignore[index]
        bin_edges = np.asarray(feat_ref["bin_edges"], dtype=np.float64)
        ref_counts = np.asarray(feat_ref["bin_counts"], dtype=np.float64)

        values = np.fromiter(
            (float(r[name]) for r in readings),
            dtype=np.float64,
            count=len(readings),
        )

        # Clip out-of-range to the outermost bins so np.histogram
        # doesn't silently drop them. See docstring step 2.
        clipped = np.clip(values, bin_edges[0], bin_edges[-1])
        actual_counts, _ = np.histogram(clipped, bins=bin_edges)
        actual_counts = actual_counts.astype(np.float64)

        # Laplace add-alpha smoothing on both sides. With alpha = 1.0
        # and 10 bins, the smoothed totals are (N_actual + 10) and
        # (N_ref + 10). Zero-width bins (adjacent-equal bin_edges, the
        # near-constant-feature case from the model session) get
        # actual_count = 0 from np.histogram; after smoothing the
        # ratio is alpha/(N+10alpha) which is finite -- no div-by-zero.
        actual_smoothed = actual_counts + LAPLACE_ALPHA
        ref_smoothed = ref_counts + LAPLACE_ALPHA

        actual_pct = actual_smoothed / actual_smoothed.sum()
        expected_pct = ref_smoothed / ref_smoothed.sum()

        psi = float(
            np.sum(
                (actual_pct - expected_pct) * np.log(actual_pct / expected_pct)
            )
        )
        # Guard against numerical pathologies (shouldn't happen with
        # Laplace > 0 but math.isnan is cheap to check).
        if not math.isfinite(psi):
            psi = 0.0
        result[name] = psi

    return result
