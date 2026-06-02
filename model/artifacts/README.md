# model/artifacts/

Committed artifacts here are the **sandbox 12-pump build** — proof-of-pipeline only. The production canonical is regenerated **natively at 30 pumps** by the PO on Windows (the sandbox's 45-second bash cap forces the smaller corpus; see ADR 0008 §Negative and `docs/sessions/2026-06-02-model-operational-reference.md`). Both builds carry the same `model_version` (`v0.1.0-seed-0`) and both validate via `shared.drift.load_reference` because they share the 4-element `feature_names` per ADR 0009. Held-out AUC stays in the 0.997–0.998 band either way — well above the 0.85 threshold (ADR 0006).

## Files

- `model.pkl` — joblib bundle: `{model_version, feature_names, auc_held_out, classifier}`. `feature_names` is the **8-element `FEATURE_NAMES`** (the scorer input contract).
- `operational_reference_distribution.json` — per-PSI-feature equal-frequency 10-bin histograms. `feature_names` is the **4-element `PSI_FEATURE_NAMES`** (the drift surface contract, ADR 0009). Built from `DEFAULT_PROFILES` HEALTHY-only data via `_generate_operational_samples` — 15 pumps × 1800 post-warm-up ticks = 27,000 samples (2,700 per bin × 10 bins).

## Regenerate

```bash
# Production (PO, Windows-native — the canonical build)
python -m model.train --n-pumps 30 --seed 0

# Sandbox (Claude, Linux mount — pipeline validation only)
python -m model.train --n-pumps 12 --seed 0
```

Default `--reference-source operational` (ADR 0008) ships the operational reference consumed by `shared.drift.load_reference`. `--reference-source training` emits a separately-named `training_reference_distribution.json` for historical comparison only (not loaded by `shared.drift` at runtime). Same `model_version` for any given `--seed` regardless of `--n-pumps`.

## Related ADRs

- ADR 0006 — model family + feature engineering + training-time DEGRADING-dwell stretch
- ADR 0008 — operational PSI reference source-separated from training corpus
- ADR 0009 — PSI surface (4 raw features) ≠ scorer feature set (8 features)
