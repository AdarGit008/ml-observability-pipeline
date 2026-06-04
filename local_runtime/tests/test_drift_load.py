"""Tests for the shared.drift reference-loading path.

Separate from ``test_shared_stubs.py`` because these tests pass paths
to ``load_reference`` to simulate failure modes. ``test_shared_stubs.py``
keeps to the business-logic surface (PSI math against synthetic
references).

Gemini Q2 of the 2026-06-01 review reshaped the load API: the
previous design had ``compute_psi(reference=None)`` lazy-load from a
module-level cache, plus a ``_reset_reference_cache`` test helper.
The refactored design (this file's API surface) makes
``load_reference(ref_path, model_path)`` the single I/O entry point;
tests pass explicit paths so there's no shared module state to reset.

ADR 0009 (2026-06-03) shrank the PSI surface from 8 features to 4
(rolling features are scorer inputs only, never PSI surface members).
``load_reference`` now validates the on-disk reference's
``feature_names`` against ``PSI_FEATURE_NAMES`` (4) rather than
``FEATURE_NAMES`` (8); the helper below writes references that
satisfy the new contract.

What this file pins:
- Missing reference file -> DriftError with the path in the message.
- Malformed JSON -> DriftError wrapping the parse error.
- Top-level dict missing ``features`` -> DriftError.
- feature_names mismatch (incl. pre-ADR-0009 8-element shape) ->
  DriftError with both lists in the message.
- model_version mismatch between model.pkl and reference -> DriftError.
- model.pkl absent -> version check is skipped (drift can still run).
- model.pkl corrupt -> DriftError wrapping the specific load failure.
- Successful load returns a usable reference dict.
- The committed operational reference produces STABLE PSI on every
  raw feature for a fresh healthy pump (regression guard for the
  reference rebuild + the ADR 0009 surface shrink).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.drift import DriftError, compute_psi, load_reference
from shared.features import FEATURE_NAMES, PSI_FEATURE_NAMES


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_valid_reference(path: Path, *, model_version: str = "v0.1.0-seed-0") -> None:
    """Write a minimal valid reference distribution to ``path``.

    Per ADR 0009: ``feature_names`` and the ``features`` map cover
    ``PSI_FEATURE_NAMES`` (4 entries), not ``FEATURE_NAMES`` (8).
    """
    edges = [float(i) for i in range(11)]
    counts = [100] * 10
    payload = {
        "model_version": model_version,
        "feature_names": list(PSI_FEATURE_NAMES),
        "n_bins": 10,
        "features": {
            name: {"bin_edges": list(edges), "bin_counts": list(counts)}
            for name in PSI_FEATURE_NAMES
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_load_reference_missing_file_raises(tmp_path):
    """No reference on disk -> DriftError with the missing path."""
    missing = tmp_path / "nope.json"
    with pytest.raises(DriftError, match="not found"):
        load_reference(ref_path=missing, model_path=tmp_path / "no-model.pkl")


def test_load_reference_malformed_json_raises(tmp_path):
    """Truncated JSON -> DriftError wrapping the parse error."""
    bad = tmp_path / "ref.json"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(DriftError, match="not valid JSON"):
        load_reference(ref_path=bad, model_path=tmp_path / "no-model.pkl")


def test_load_reference_missing_features_key_raises(tmp_path):
    """Top-level dict without 'features' -> DriftError."""
    bad = tmp_path / "ref.json"
    bad.write_text('{"model_version": "x"}', encoding="utf-8")
    with pytest.raises(DriftError, match="features"):
        load_reference(ref_path=bad, model_path=tmp_path / "no-model.pkl")


def test_load_reference_feature_names_mismatch_raises(tmp_path):
    """A reference with the wrong feature_names tuple -> DriftError so
    silent mode-parity divergence is caught at load time."""
    bad = tmp_path / "ref.json"
    bad.write_text(
        json.dumps(
            {
                "model_version": "v0.1.0",
                "feature_names": ["some_other_feature"],
                "features": {"some_other_feature": {"bin_edges": [], "bin_counts": []}},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(DriftError, match="feature_names"):
        load_reference(ref_path=bad, model_path=tmp_path / "no-model.pkl")


def test_load_reference_pre_adr_0009_eight_feature_list_raises(tmp_path):
    """A pre-ADR-0009 reference (8-element feature_names) is now
    rejected. The error message points at the rebuild command.

    This is the silent-divergence guard: a stale reference would
    re-introduce the autocorrelation noise problem ADR 0009 closes
    by reinstating PSI on the four rolling features."""
    edges = [float(i) for i in range(11)]
    counts = [100] * 10
    payload = {
        "model_version": "v0.1.0-seed-0",
        "feature_names": list(FEATURE_NAMES),  # 8 entries — pre-ADR-0009
        "n_bins": 10,
        "features": {
            name: {"bin_edges": list(edges), "bin_counts": list(counts)}
            for name in FEATURE_NAMES
        },
    }
    bad = tmp_path / "ref.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DriftError, match="pre-ADR-0009"):
        load_reference(ref_path=bad, model_path=tmp_path / "no-model.pkl")


def test_load_reference_model_pkl_absent_skips_version_check(tmp_path):
    """Reference present, model.pkl absent -> load_reference succeeds
    (no version check to perform). The dev-environment scenario where
    only the reference is shipped (e.g., a fleet-PSI Lambda layer)
    should still work."""
    ref_path = tmp_path / "ref.json"
    _write_valid_reference(ref_path)
    ref = load_reference(ref_path=ref_path, model_path=tmp_path / "no-model.pkl")
    assert ref["model_version"] == "v0.1.0-seed-0"


def test_load_reference_model_version_mismatch_raises(tmp_path):
    """Reference says v0.1.0-seed-0, model.pkl says v0.1.0-seed-1 ->
    DriftError so the lambda_scorer cold-start fails fast rather than
    silently scoring vs. one version and PSI-ing vs. another."""
    import joblib

    ref_path = tmp_path / "ref.json"
    _write_valid_reference(ref_path, model_version="v0.1.0-seed-0")

    model_path = tmp_path / "model.pkl"
    joblib.dump(
        {
            "model_version": "v0.1.0-seed-1",  # mismatch
            "feature_names": list(FEATURE_NAMES),
            "auc_held_out": 0.99,
            "classifier": object(),
        },
        model_path,
    )

    with pytest.raises(DriftError, match="version mismatch"):
        load_reference(ref_path=ref_path, model_path=model_path)


def test_load_reference_model_version_match_passes(tmp_path):
    """Versions match -> load succeeds, version check passes."""
    import joblib

    ref_path = tmp_path / "ref.json"
    _write_valid_reference(ref_path, model_version="v0.1.0-seed-0")

    model_path = tmp_path / "model.pkl"
    joblib.dump(
        {
            "model_version": "v0.1.0-seed-0",
            "feature_names": list(FEATURE_NAMES),
            "auc_held_out": 0.99,
            "classifier": object(),
        },
        model_path,
    )

    ref = load_reference(ref_path=ref_path, model_path=model_path)
    assert ref["model_version"] == "v0.1.0-seed-0"


def test_load_reference_model_pkl_corrupt_raises_drifterror(tmp_path):
    """Q4 (Gemini 2026-06-01): a model.pkl that's present but corrupt
    raises DriftError with the specific exception type in the message,
    not a raw pickle exception. Narrowed from the previous
    catch-Exception."""
    ref_path = tmp_path / "ref.json"
    _write_valid_reference(ref_path)

    model_path = tmp_path / "model.pkl"
    # Write garbage bytes that joblib won't be able to unpickle.
    model_path.write_bytes(b"not a valid pickle stream \x00\x01\x02")

    with pytest.raises(DriftError, match="failed to load for the version-match"):
        load_reference(ref_path=ref_path, model_path=model_path)


def test_load_reference_returns_dict_usable_by_compute_psi(tmp_path):
    """End-to-end: load_reference -> compute_psi composes without
    explicit shape gymnastics on the caller's side.

    Per ADR 0009: compute_psi returns a dict keyed by
    ``PSI_FEATURE_NAMES`` (4 keys), not ``FEATURE_NAMES`` (8). The
    input feature dict carries all 8 names because
    ``extract_features`` always produces all 8; compute_psi ignores
    the rolling-feature keys."""
    ref_path = tmp_path / "ref.json"
    _write_valid_reference(ref_path)
    ref = load_reference(ref_path=ref_path, model_path=tmp_path / "no-model.pkl")

    features = {name: 5.0 for name in FEATURE_NAMES}
    psi = compute_psi([features], reference=ref)
    assert set(psi.keys()) == set(PSI_FEATURE_NAMES)


# ---------------------------------------------------------------------------
# Operational reference: demo-paced PSI regression guard (ADR 0008 + ADR 0009)
# ---------------------------------------------------------------------------


def test_demo_paced_healthy_psi_stable():
    """ADR 0008 + ADR 0009 regression guard: the committed operational
    reference must produce STABLE PSI on every PSI surface feature for
    a fresh HEALTHY pump running ``DEFAULT_PROFILES``.

    Replicates the drift session's measurement harness (docs/sessions/
    2026-06-01-drift-real-psi.md §"Reference-Validity carry-in --
    MEASUREMENT") with two corrections:

    1. Uses ``Pump.step()`` with ``DEFAULT_PROFILES`` (not the drift
       session's hand-rolled noise model, which had a motor_current
       spec error of 3.5 vs the simulator's 4.0). The simulator is
       what the live runtime emits, so it's the honest comparison.
    2. Skips the WINDOW_TICKS warm-up: the live runtime's PSI window
       in steady state is fully post-warm-up, and the operational
       reference is built the same way (model.train's
       ``_generate_operational_samples`` discards warm-up).

    Acceptance (ADR 0009 tightening of ADR 0008's split):

    - PSI dict keys equal ``PSI_FEATURE_NAMES`` exactly. Rolling
      features are not in the dict at all (the four-key contract
      replaces ADR 0008's eight-key dict with a soft rolling bound).
    - Every key's PSI < 0.10 STABLE. The per-tick noise envelope is
      IID by construction, so PSI on the four raw features stays in
      the STABLE band against a multi-pump reference. The drift
      session's measurement showed PSI 1.3-6.7 SIGNIFICANT here under
      the training reference; ADR 0008 took it to STABLE on raw
      features; ADR 0009 retires the rolling-feature autocorrelation
      noise by dropping those features from the surface entirely.
    """
    from collections import deque

    from model.train import WINDOW_TICKS
    from simulator.pump import DEFAULT_PROFILES, Pump, PumpState

    # Fresh seed not used by the operational reference (which used
    # seeds 0..4 derived from --seed 0). Independent noise instance.
    pump = Pump("P-99", seed=42, profiles=dict(DEFAULT_PROFILES))
    window: deque = deque(maxlen=WINDOW_TICKS)
    features = []
    for t in range(WINDOW_TICKS + 1800):
        window.append(pump.step())
        if t < WINDOW_TICKS:
            continue  # warm-up
        # Lazy import inside test body so the file's import surface
        # stays narrow (the rest of this file uses synthetic refs).
        from shared.features import extract_features as _ef
        features.append(_ef(list(window)))
    assert pump.state is PumpState.HEALTHY, (
        f"pump left HEALTHY mid-window ({pump.state}); DEFAULT_PROFILES"
        " dwell shrank below 1800+WINDOW_TICKS"
    )

    ref = load_reference()  # default path = operational reference
    assert ref["model_version"], "operational reference is missing model_version"

    psi = compute_psi(features, reference=ref)

    # ADR 0009: hard four-key contract. Rolling features are not in
    # the PSI dict (compute_psi iterates PSI_FEATURE_NAMES only).
    assert set(psi.keys()) == set(PSI_FEATURE_NAMES), (
        f"PSI dict keys {sorted(psi.keys())} != PSI_FEATURE_NAMES "
        f"{sorted(PSI_FEATURE_NAMES)}; ADR 0009 surface shrink regressed."
    )

    failures = {n: psi[n] for n in PSI_FEATURE_NAMES if psi[n] >= 0.10}
    assert not failures, (
        "PSI exceeded STABLE threshold (0.10) on healthy demo "
        "telemetry; the operational reference rebuild is broken. "
        f"failures: {failures}"
    )
