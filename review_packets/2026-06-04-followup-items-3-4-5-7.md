# Review Packet 2026-06-04 — model — close ADR 0008/0009 follow-up items 3, 4, 5, 7

> Paste this entire file into Gemini via:
> `gemini -p "$(cat review_packets/2026-06-04-followup-items-3-4-5-7.md)" > review_responses/2026-06-04-followup-items-3-4-5-7.md`

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`. Background ADRs for this session: `docs/adr/0008-operational-reference-source-separation.md`, `docs/adr/0009-psi-surface-vs-scorer-feature-set.md`.

## Summary of the change

This session closes the four small follow-up items deferred from the 2026-06-03 ADR 0009 session: (3) bumped `OPERATIONAL_REFERENCE_PUMPS` 5 → 15 to align the operational reference fleet with the demo fleet's pump count; (4) added three loud ADR 0008/0009 banner comments above `_training_profiles`, `_operational_profiles`, and `compute_reference_distribution` in `model/train.py` so a future scan-the-file reader can't miss the asymmetry; (5) wrote `model/artifacts/README.md` distinguishing the sandbox 12-pump build from the production canonical (PO native 30-pump regen, Item 6, deferred); (7) swept stale `reference_distribution.json` references out of live (non-frozen) docs and the `shared/drift.py` docstring + error-message strings. No parity contract changes — `FEATURE_NAMES`, `PSI_FEATURE_NAMES`, and the `shared.drift` function signatures are untouched. Structural-parity tests stay green (350 passed + 1 skipped, unchanged from baseline).

Out-of-scope discovery resolved at PO's direction: `HANDOFF.md` was a `.md.docx` Word binary mis-saved with a double extension; converted to clean GFM markdown via pandoc + sed-strip of Word's blockquote artifacts on list items, deleted the docx.

## Diff

```diff
# model/train.py — Item 3 + Item 4 (combined outputs/cp pass)
@@ -142,11 +142,17 @@ PSI_BIN_COUNT: int = 10
-# Operational reference shape (ADR 0008). 5 pumps × 1800 ticks = 9000
-# samples per the PO call in the 2026-06-02 session log:
-#  - 200 samples/bin × 10 bins = 2000-sample floor for stable equal-
-#    frequency quantiles; 9000 clears it ~4.5× with margin.
-#  - Five pumps average out per-pump noise instances; one pump would
-#    bake the reference into a single trajectory's noise realisation.
-OPERATIONAL_REFERENCE_PUMPS: int = 5
+# Operational reference shape (ADR 0008, refined by Item 3 of the
+# 2026-06-04 follow-up session). 15 pumps × 1800 ticks = 27_000
+# samples — matches the demo fleet's pump count so "the operational
+# baseline" reads as "the demo fleet's healthy baseline" with zero
+# mental translation for a future debugger:
+#  - 200 samples/bin × 10 bins = 2000-sample floor for stable equal-
+#    frequency quantiles; 27_000 clears it ~13.5× with margin.
+#  - Fifteen pumps average out per-pump noise instances; one pump
+#    would bake the reference into a single trajectory's noise
+#    realisation.
+#  - ADR 0008 §Footprint measured the PSI shift between 5- and 50-
+#    pump references at < 0.05 — the structural floor is settled.
+#    The 5 → 15 bump is for narrative alignment, not numerics.
+#  - Wall-clock cost: ~3× vs the 5-pump build (~3 s vs ~1 s sandbox).
+OPERATIONAL_REFERENCE_PUMPS: int = 15

# Banner above _training_profiles (line 187):
+# ### ASYMMETRIC PROFILES BY DESIGN (ADR 0008) ###
+# _training_profiles and _operational_profiles produce DIFFERENT
+# dict[PumpState, StateProfile] shapes on purpose. The model corpus
+# needs the stretched 48h DEGRADING ramp (ADR 0006); the operational
+# PSI reference needs DEFAULT_PROFILES verbatim (ADR 0008). Do NOT
+# "harmonize" them — conflation produced PSI 1.3–6.7 SIGNIFICANT on
+# healthy fleets pre-ADR-0008.

# Banner above _operational_profiles (line 240):
+# ### ASYMMETRIC PROFILES BY DESIGN (ADR 0008) ###
+# Companion to _training_profiles — see banner above for the full
+# rationale. Returns DEFAULT_PROFILES verbatim; mirrors
+# _training_profiles in shape (fresh dict, not the module-level
+# DEFAULT_PROFILES) but emphatically NOT in content.

# Banner above compute_reference_distribution (line 548):
+# ### PSI SURFACE ≠ SCORER FEATURE SET (ADR 0009) ###
+# This function slices the 8-column X matrix (FEATURE_NAMES) down to
+# the 4-column PSI surface (PSI_FEATURE_NAMES) before binning. Do NOT
+# iterate FEATURE_NAMES here — rolling features violate PSI's IID
+# assumption (149/150 overlap between consecutive 5-min windows) and
+# produce 0.10–0.40 autocorrelation noise on healthy fleets (ADR 0009
+# §Decision; ADR 0008 §Negative measurement).
```

```diff
# shared/drift.py — Item 7 string-only edits (no contract change)
@@ -148,5 +148,5 @@
     Args:
         ref_path: path to the reference JSON. Defaults to
-            ``model/artifacts/reference_distribution.json`` relative
+            ``model/artifacts/operational_reference_distribution.json`` relative
             to the repo root.
@@ -254,5 +254,5 @@
             "model/reference version mismatch -- "
             f"model.pkl model_version={model_version!r}, "
-            f"reference_distribution.json model_version={ref_version!r}. "
+            f"operational_reference_distribution.json model_version={ref_version!r}. "
             "Re-run `python -m model.train` so both artifacts share a version."
         )
```

```diff
# docs/adr/0006-model-family-and-feature-engineering.md — Item 7
@@ -285,5 +285,5 @@
- Implementation: `model/train.py`, `shared/score.py`,
   `model/artifacts/model.pkl`,
-  `model/artifacts/reference_distribution.json`.
+  `model/artifacts/operational_reference_distribution.json`.
@@ -368 +368 @@
-| `shared/` + `lambda_scorer/` + `model/artifacts/` (model.pkl 300 KB + reference 5 KB) | ~0.5 MB |
+| `shared/` + `lambda_scorer/` + `model/artifacts/` (model.pkl ~290 KB + operational reference ~2.2 KB, post-ADR-0009) | ~0.5 MB |
```

```diff
# context/_interfaces.md — Item 7
@@ -52,5 +52,5 @@
 ## Reference distribution (PSI baseline)
-- File: `model/artifacts/reference_distribution.json`
-- Format: per-feature histograms with bin edges + bin counts.
+- File: `model/artifacts/operational_reference_distribution.json` (ADR 0008 operational; 4-feature PSI surface per ADR 0009).
+- Format: per-PSI-feature histograms with bin edges + bin counts.
```

```diff
# HANDOFF.md — out-of-scope rename (docx → md) + Item 7 §6 Q4 fix
# (Full file regenerated from pandoc conversion of HANDOFF.md.docx;
# only Item-7-relevant change shown below.)
@@ -193 +193 @@
-4.  **Reference distribution storage.** reference\_distribution.json for PSI lives where during AWS mode? S3 ...
+4.  **Reference distribution storage.** operational\_reference\_distribution.json for PSI lives where during AWS mode? S3 ...
```

```diff
# model/artifacts/README.md — Item 5 (new file, 26 lines, 2 KB)
+# model/artifacts/
+
+Committed artifacts here are the **sandbox 12-pump build** — proof-of-pipeline only. The production canonical is regenerated **natively at 30 pumps** by the PO on Windows ...
+
+## Files
+- `model.pkl` — joblib bundle: `{model_version, feature_names, auc_held_out, classifier}`. `feature_names` is the **8-element `FEATURE_NAMES`** (the scorer input contract).
+- `operational_reference_distribution.json` — per-PSI-feature equal-frequency 10-bin histograms. `feature_names` is the **4-element `PSI_FEATURE_NAMES`** (the drift surface contract, ADR 0009). Built from `DEFAULT_PROFILES` HEALTHY-only data via `_generate_operational_samples` — 15 pumps × 1800 post-warm-up ticks = 27,000 samples (2,700 per bin × 10 bins).
+
+## Regenerate
+```bash
+# Production (PO, Windows-native — the canonical build)
+python -m model.train --n-pumps 30 --seed 0
+# Sandbox (Claude, Linux mount — pipeline validation only)
+python -m model.train --n-pumps 12 --seed 0
+```
+...
```

Artifact-shape verification (sandbox regen, 15 × 1800 = 27 000 samples, equal-frequency bins):

```
feature_names: ['vibration_amp', 'bearing_temp', 'motor_current', 'rpm']  (4 — PSI_FEATURE_NAMES per ADR 0009)
n_bins: 10
model_version: v0.1.0-seed-0
For each feature: 10 bins, total samples = 27000, bin_counts = [2700] * 10  ✓ equal-frequency
```

## Specific questions for Gemini

1. **Item 3 — ADR 0008 amendment or session-log note?** Recommendation here was session-log note: the 5→15 bump is a refinement of the PO call documented in ADR 0008 §1, not a reversal of its logic. ADR 0008 §Footprint already noted the structural floor (5→50 PSI shift < 0.05), so the bump is for narrative alignment, not numerics. Is that the right call, or should ADR 0008 carry a one-line addendum? (The session log captures the change either way.)

2. **Item 4 — banner placement vs existing docstrings.** The three `### …###` banner comments duplicate some content already present in each function's docstring (`_training_profiles` docstring already mentions the asymmetry; `_operational_profiles` docstring already mentions ADR 0008; `compute_reference_distribution` docstring already explains the 4-column slice). Banners exist for the scan-the-file reader who never opens docstrings. Is this a worthwhile redundancy, or does the duplication risk drifting — banner says one thing, docstring says another — over time? If you'd recommend a different mechanism (e.g., a single module-level "Three asymmetries" banner near the imports instead of three function-level banners), say so.

3. **Item 7 — scope expansion (4 markdown + 2 shared/drift.py string-only edits).** The brief framed Item 7 as a read-only deploy-zip-recipe check. The comprehensive grep surfaced live stale references in two parity-boundary files (`shared/drift.py` docstring + error-message strings). PO approved the pragmatic expansion: docstring + error-message edits don't change the parity contract (signatures, types, behavior), and punting them would mean the lambda_scorer cold-start error path prints the wrong filename. Was that the right call, or would you have flagged the brief constraint ("no edits to `shared/{features,score,drift}.py` this session") as harder than I read it?

## What I'm NOT looking for in this review

- Style/formatting on the new banner comments — PO sign-off at plan-step covered the `### ASYMMETRIC PROFILES BY DESIGN ###` literal format.
- Item 6 (production native 30-pump regen) is PO-side and out of this packet's scope.
- The HANDOFF.md.docx → HANDOFF.md rename was an unscheduled cleanup; the markdown content was preserved via pandoc + sed, not authored. PO confirmed the double extension was unintentional. Critique the Item-7-relevant §6 Q4 line edit, not the full rendered markdown.
- Test count delta = 0 by design (Items 3/4/5/7 are constant + comment + doc + string-only). No new test surface is in scope.

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. <summarize> | Addressed / Deferred / Rejected | <where, why> |
| 2. ... | ... | ... |
| 3. ... | ... | ... |
