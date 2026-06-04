# model/artifacts/

Committed artifacts here are the **PO-native canonical build** (30-pump training corpus, `--seed 0`), regenerated on the Windows workstation. The sandbox runs a smaller 12-pump corpus for pipeline validation only — those builds never land in git (see §Commit policy). Both builds carry the same `model_version` (`v0.1.0-seed-0`) and both validate via `shared.drift.load_reference` because they share the 4-element `feature_names` per ADR 0009. Held-out AUC stays in the 0.997–0.998 band either way — well above the 0.85 threshold (ADR 0006).

## Commit policy (PO decision, 2026-06-04)

**Only PO-native canonical builds get committed.** Rationale: committed artifacts keep a fresh clone green (`pytest` passes out of the box — the right out-of-box experience for a portfolio repo), but sandbox-built artifacts carry sklearn-version skew risk (MVP review Q6), so they are excluded from staging.

- A session (sandbox or otherwise) that rebuilds `model.pkl` / `operational_reference_distribution.json` for validation purposes must NOT stage those files. Before the session's commit, the PO regenerates natively: `python -m model.train --n-pumps 30 --seed 0`.
- The pre-commit `git diff --cached --name-status` check in the canonical staging sequence (DEV_NORMS §7) is the enforcement point: artifact paths in the staged set are only acceptable when the PO ran the regen that produced them.
- No `.gitignore` entry — the files stay tracked; the policy governs *which build* of them gets staged.
- Version-skew expectation: a validation environment running an OLDER sklearn than the build environment emits `InconsistentVersionWarning` on unpickle (forward-unpickle is sklearn's riskier direction). Suite-green is the acceptance bar; the warning lives in the validation env, never in the artifact. (2026-06-04 review P4.)

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
# Production (PO, Windows-native — the canonical build, the ONLY one committed)
python -m model.train --n-pumps 30 --seed 0

# Sandbox / CI / any resource-constrained environment (pipeline validation only — never staged)
python -m model.train --n-pumps 12 --seed 0
```

Default `--reference-source operational` (ADR 0008) ships the operational reference consumed by `shared.drift.load_reference`. `--reference-source training` emits a separately-named `training_reference_distribution.json` for historical comparison only (not loaded by `shared.drift` at runtime). The 15-pump operational reference is invariant under `--n-pumps`.

## Related ADRs

- ADR 0006 — model family + feature engineering + training-time DEGRADING-dwell stretch
- ADR 0008 — operational PSI reference source-separated from training corpus
- ADR 0009 — PSI surface (4 raw features) ≠ scorer feature set (8 features)
