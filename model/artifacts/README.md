# model/artifacts/

Committed artifacts here are the **sandbox pipeline-validation build** (12-pump training corpus). The production canonical is regenerated **natively at 30 pumps** by the PO — the sandbox runs the smaller corpus to fit the resource-constrained environment (a few seconds of CPU per pump), then PO re-runs at full scale on the workstation. Both builds carry the same `model_version` (`v0.1.0-seed-0`) and both validate via `shared.drift.load_reference` because they share the 4-element `feature_names` per ADR 0009. Held-out AUC stays in the 0.997–0.998 band either way — well above the 0.85 threshold (ADR 0006).

## Two pump counts, two purposes

The training pipeline references two distinct fleet sizes — easy to conflate, so spelled out here:

- **`--n-pumps` CLI flag (training corpus)**: how many simulated pumps generate the labeled training matrix that fits `model.pkl`. Sandbox uses 12 to fit a short build budget; PO's canonical native build uses 30.
- **`OPERATIONAL_REFERENCE_PUMPS` module constant in `model/train.py` (operational PSI reference fleet)**: how many healthy demo-paced pumps are sampled to build `operational_reference_distribution.json`. Fixed at **15** — matching the actual demo fleet size so the PSI baseline reads as "the demo fleet's healthy distribution" with zero mental translation (ADR 0008 + 2026-06-04 Item 3 refinement). Independent of `--n-pumps`.

So a single invocation produces both files: `model.pkl` reflects the `--n-pumps` corpus, `operational_reference_distribution.json` always reflects the 15-pump operational reference. Same `model_version` tag for any given `--seed`.

## Files

- `model.pkl` — joblib bundle: `{model_version, feature_names, auc_held_out, classifier}`. `feature_names` is the **8-element `FEATURE_NAMES`** (the scorer input contract).
- `operational_reference_distribution.json` — per-PSI-feature equal-frequency 10-bin histograms. `feature_names` is the **4-element `PSI_FEATURE_NAMES`** (the drift surface contract, ADR 0009). Built from `DEFAULT_PROFILES` HEALTHY-only data via `_generate_operational_samples` — 15 pumps × 1800 post-warm-up ticks = 27,000 samples (2,700 per bin × 10 bins).

## Regenerate

```bash
# Production (PO, Windows-native — the canonical build)
python -m model.train --n-pumps 30 --seed 0

# Sandbox / CI / any resource-constrained environment (pipeline validation only)
python -m model.train --n-pumps 12 --seed 0
```

Default `--reference-source operational` (ADR 0008) ships the operational reference consumed by `shared.drift.load_reference`. `--reference-source training` emits a separately-named `training_reference_distribution.json` for historical comparison only (not loaded by `shared.drift` at runtime). The 15-pump operational reference is invariant under `--n-pumps`.

## Related ADRs

- ADR 0006 — model family + feature engineering + training-time DEGRADING-dwell stretch
- ADR 0008 — operational PSI reference source-separated from training corpus
- ADR 0009 — PSI surface (4 raw features) ≠ scorer feature set (8 features)
