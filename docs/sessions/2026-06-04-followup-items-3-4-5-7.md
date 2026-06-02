# 2026-06-04 — model: close ADR 0008/0009 follow-up items 3, 4, 5, 7

## Component
`model` (Tier 2b parity-adjacent: Item 3 touches the artifact emission path; Item 4 is doc-only inside `model/train.py`; Item 7 includes string-only edits inside `shared/drift.py`). No parity-contract changes — `FEATURE_NAMES`, `PSI_FEATURE_NAMES`, and the `shared.drift` function signatures are untouched. Structural-parity tests stayed green throughout.

## Intent
Close the four small follow-up items deferred from the 2026-06-03 ADR 0009 session: bump the operational reference fleet from 5 → 15 pumps (narrative alignment with the demo fleet), add loud ADR 0008/0009 banner comments to `model/train.py`, document the sandbox-vs-production artifact distinction in a new `model/artifacts/README.md`, and sweep stale `reference_distribution.json` references out of the live (non-frozen) docs + the `shared/drift.py` docstring/error-message strings.

## PO decisions at plan-step

1. **Item 3 — ADR 0008 amendment or session-log note?** → **session-log note** (recommended). The 5 → 15 bump is a refinement of the original PO call documented in ADR 0008 §1, not a reversal of its logic. ADR 0008 §Footprint explicitly noted that increasing N from 5 → 50 moves the worst-case PSI by < 0.05 — the structural floor is settled, so the bump is for narrative alignment, not numerics.
2. **Item 4 — third banner above `compute_reference_distribution`?** → **yes** (recommended). That's the artifact-emission boundary where the ADR 0009 asymmetry first becomes visible on disk; highest-leverage spot for the pointer.
3. **Item 5 — README length?** → **single paragraph + regen code block** (recommended). Anything longer is over-engineering for a 2-file artifact dir.
4. **Item 7 — scope when stale references found outside ADR 0006 + HANDOFF.md?** → **(b) pragmatic expansion to all six sites** (recommended). The comprehensive grep surfaced two additional stale spots in `context/_interfaces.md` and two inside `shared/drift.py` (docstring + error-message strings). All four extras are string-only — no parity-contract change. Bundling them now avoids the lambda_scorer session hitting the stale name in `shared/drift.py`'s cold-start error message.
5. **Out-of-scope discovery: `HANDOFF.md.docx`.** The Item 7 read surfaced that HANDOFF.md was a `.md.docx` mis-saved Word binary, not plain markdown. PO confirmed the double extension was unintentional and approved cleanup: pandoc-converted the docx to clean GFM markdown, sed-stripped Word's blockquote artifacts on list items, dropped in as `HANDOFF.md`, deleted the docx via the cowork delete tool. Item 7's HANDOFF.md edit then ran against the new markdown — single line change instead of python-docx tooling.

## What changed

### Code

- `model/train.py` (+~25 net lines, all comments):
  - `OPERATIONAL_REFERENCE_PUMPS: int = 5` → `15`. Surrounding rationale comment block rewritten: 5 × 1800 = 9000 → 15 × 1800 = 27000; "4.5× margin" → "13.5× margin"; new narrative-alignment justification + ADR 0008 §Footprint pointer + ~3× wall-clock note.
  - `### ASYMMETRIC PROFILES BY DESIGN (ADR 0008) ###` banner above `_training_profiles` (7-line block explaining the model-vs-reference profile asymmetry + the PSI 1.3–6.7 SIGNIFICANT regression that conflation produces).
  - `### ASYMMETRIC PROFILES BY DESIGN (ADR 0008) ###` companion banner above `_operational_profiles` (5-line block pointing at the upstream banner; emphasizes "mirrors shape, not content").
  - `### PSI SURFACE ≠ SCORER FEATURE SET (ADR 0009) ###` banner above `compute_reference_distribution` (7-line block explaining the 4-column slice + the IID-assumption / 149-of-150-window-overlap rationale + ADR 0009 §Decision pointer).

- `shared/drift.py` (string-only edits, no contract change):
  - `load_reference` docstring `ref_path` parameter description: `reference_distribution.json` → `operational_reference_distribution.json`.
  - `_check_model_version_match` error-message string in the version-mismatch branch: `reference_distribution.json` → `operational_reference_distribution.json`. (Future lambda_scorer cold-start that hits this branch will now print the right filename.)

### Artifacts

- `model/artifacts/model.pkl` — regenerated (276.8 KB; AUC 0.998 on 3/12 hold-out; version `v0.1.0-seed-0`). Sandbox-runtime corpus is 12 pumps; the operational reference now consumes 15 pumps × 1800 ticks. PO regenerates at 30 pumps natively on Windows for production (Item 6, separate follow-up).
- `model/artifacts/operational_reference_distribution.json` — regenerated. **15 pumps × 1800 ticks = 27000 samples** (was 9000 pre-Item-3). Equal-frequency binning verified: 2700/bin × 10 bins per feature. `feature_names` stays at the 4-element `PSI_FEATURE_NAMES` per ADR 0009. `model_version = v0.1.0-seed-0`. File size 2.2 KB (unchanged from ADR 0009; the surface is the same 4 features, so the wire size doesn't scale with sample count).
- `model/artifacts/README.md` — new (26 lines, 2 KB). Documents the sandbox-vs-production artifact distinction + the `feature_names` asymmetry between `model.pkl` (8 — `FEATURE_NAMES`) and `operational_reference_distribution.json` (4 — `PSI_FEATURE_NAMES`) + a regen code block.

### Documentation

- `docs/adr/0006-model-family-and-feature-engineering.md`:
  - Line 287 (References §Implementation): filename `reference_distribution.json` → `operational_reference_distribution.json`.
  - Line 368 (footprint table): "model.pkl 300 KB + reference 5 KB" → "model.pkl ~290 KB + operational reference ~2.2 KB, post-ADR-0009". Subtotal cell unchanged (~0.5 MB rounds the same).
- `context/_interfaces.md` line 53 (Reference distribution §): filename rename + clarifying parenthetical "(ADR 0008 operational; 4-feature PSI surface per ADR 0009)". "Per-feature histograms" → "Per-PSI-feature histograms".
- `HANDOFF.md` (now plain markdown — see §Out-of-scope cleanup below) line 193 §6 Q4: filename `reference\_distribution.json` → `operational\_reference\_distribution.json`. Question intact (storage location is still open).
- ADR 0007 + ADR 0008's own historical mentions of the OLD `reference_distribution.json` filename intentionally left untouched — those are immutable-by-convention historical context within their accepted ADRs.

### Out-of-scope cleanup (PO-approved at Item 7 plan-step)

- **`HANDOFF.md.docx` → `HANDOFF.md`.** The file was a Word `.docx` binary mis-saved with a double extension (`.md.docx`). PO confirmed unintentional. Cleanup: pandoc → GFM markdown; sed-stripped Word's `> ` blockquote prefixes from list items; cp'd to `HANDOFF.md`; deleted the `.docx` via the cowork delete tool. Result: 297 lines / 15 KB clean markdown, sections 1–10 intact, only one `> ` remaining (line 3 — a legitimate "Instructions for next Claude" blockquote).
- Brief had referenced "HANDOFF.md §6 Q3" as the Item 7 target; actual stale filename is in §6 **Q4** ("Reference distribution storage"). Q3 is "Lambda model packaging" (deployment-zip vs Layers vs S3), artifact-name-agnostic, and needs no edit.

## Decisions

- **5 → 15 pump bump captured as a session-log note, not an ADR 0008 amendment.** Per the plan-step Q1 above. The bump is a refinement of the PO call documented in ADR 0008 §1, not a reversal of its logic. ADR 0008 §Footprint explicitly noted the structural floor; the bump is for narrative alignment.
- **Item 7 expanded from read-only check to four-file fix.** The comprehensive grep surfaced live stale references the brief hadn't catalogued. All edits are string-only — no parity contract change.

## Trade-offs surfaced

- **Three-line `> ` blockquote stripping (HANDOFF.md cleanup).** The pandoc GFM conversion produced `  - > TEXT` patterns from Word's bullet-with-blockquote formatting. Two options: (a) leave as-is (faithful to docx, ugly markdown), (b) plain `markdown` output mode (clean bullets, ASCII-grid tables). Picked (c) — gfm + sed-strip `^( *(?:-|\d+\.)\s+)> /\1/`. Kept clean tables and clean bullets. Single residual `> ` was a legitimate blockquote, not an artifact.
- **Sandbox-runtime model.pkl + reference are PROOF-OF-PIPELINE only.** The committed `model.pkl` (276.8 KB, AUC 0.998) was built from a 12-pump training corpus (45 s bash cap). The production canonical needs PO's native 30-pump regen (Item 6). The `model/artifacts/README.md` paragraph documents this distinction so a future reader doesn't get confused by the size delta.

## Gemini review highlights

Three direct questions + three adversarial observations. Full response at `review_responses/2026-06-04-followup-items-3-4-5-7.md` (re-encoded from PowerShell-default UTF-16 to UTF-8 in this session — a future `gemini > response.md` should use `Out-File -Encoding utf8` or the `scripts/gemini_review.ps1` wrapper per ADR 0001 to avoid the re-encode). Full disposition table in `review_packets/2026-06-04-followup-items-3-4-5-7.md` §Resolution.

**Headline disagreement (Q1):** Gemini recommended an "Amended 2026-06-04" note in ADR 0008 §Decision 2 to close the stale-spec gap (ADR reads "5 pumps", code reads "15"). PO call after review: **stand on session-log note** — ADR 0008 §Footprint already establishes the 5-pump structural floor at <0.05 PSI shift up to 50 pumps, so a refinement within that floor reads as session-log territory rather than an ADR amendment. Disagreement surfaced per DEV_NORMS §1.

**Agreements (Q2 + Q3):** Gemini concurred that the three loud banners in `model/train.py` work as tripwires precisely because docstrings get scrolled past, and that Item 7's scope expansion into `shared/drift.py` (docstring + error-message strings only) was the right engineering call over rigid brief adherence.

**Adversarial observations actioned (all three):**

- **Obs 1 (README 15-vs-30 ambiguity).** `model/artifacts/README.md` grew a "Two pump counts, two purposes" section spelling out `--n-pumps` (training corpus, 12 sandbox / 30 production) vs `OPERATIONAL_REFERENCE_PUMPS` (operational reference, fixed at 15, invariant under `--n-pumps`).
- **Obs 2 ("45-second bash cap" too meta).** README phrasing now reads "sandbox / CI / any resource-constrained environment" — portfolio-durable, drops the bash-specific number.
- **Obs 3 (HANDOFF.md anchor-link survival post-pandoc).** Grep confirms zero anchor-style internal links exist in HANDOFF.md. Non-issue.

**PO read of Gemini's overall posture:** Approved. Changes close ADR 0008/0009 technical debt cleanly; doc strategy is pragmatic; scope expansion was correct engineering judgment.

## Tests state

**350 passed + 1 skipped** in 15.26 s (sandbox). **Unchanged from the 2026-06-03 baseline** — Item 3's constant bump doesn't add or remove tests (the `n_pumps=2`/`ticks=300` sandbox test for `_generate_operational_samples` runs against its own tiny shape, unaffected by the module-level constant); Item 4 is comment-only; Item 5 is doc-only; Item 7 is string-only across docstrings and error messages. Structural-parity tests (`test_structural_parity_no_vendoring`, `test_structural_parity_compute_psi_loads_from_shared`, `test_psi_feature_names_is_subset_of_feature_names`) all green.

## Open follow-ups

- **Item 6 — PO regenerates production artifacts at 30 pumps natively.** Command: `cd "D:\Claude\ML Observability Pipeline" && python -m model.train --n-pumps 30 --seed 0`. Overwrites `model/artifacts/model.pkl` (~290 KB; was 276.8 KB in this sandbox build) + `model/artifacts/operational_reference_distribution.json` (2.2 KB; structurally identical shape — 4 features, 27000 samples). Same `v0.1.0-seed-0` tag. **PO-side only — bash 45 s cap blocks sandbox-side.** After this lands, every follow-up from the 2026-06-02 Gemini review of ADR 0008 is closed.
- **Lambda_scorer session pickup.** The deploy-zip recipe now references the correct filename in every live document (ADR 0006 §Implementation + §Footprint, `context/_interfaces.md` §Reference distribution, `HANDOFF.md` §6 Q4, `shared/drift.py` docstring + error message). The recipe is mechanically correct for the lambda_scorer session. Nothing else to verify Item-7-side.
- **Dashboards session pickup (carry-forward from 2026-06-03).** Four PSI panels (`psi_vibration_amp`, `psi_bearing_temp`, `psi_motor_current`, `psi_rpm`), not eight. PLAN.md §2.7 bands apply uniformly. Unchanged by this session.

## Context files updated

- `context/_interfaces.md` — Item 7 filename + ADR 0008/0009 parenthetical on §Reference distribution.
- `context/drift.md` — **no edits this session.** §Current state already names `operational_reference_distribution.json` correctly post-ADR-0008. The stale `reference_distribution.json` mentions there (line 10) are explicit historical context ("The training-time `reference_distribution.json` is retired") — legitimate, not stale.
- `context/model.md` — **no edits this session.** §Current state already names `operational_reference_distribution.json` post-ADR-0008 + ADR 0009. The 5-pump-old-reference mention at line 24 ("5 pumps × 1800 post-warm-up ticks") is now stale (the operational reference is 15 × 1800 post-Item-3). **Carry-forward: PO updates `context/model.md` line 24 from "5 pumps × 1800 post-warm-up ticks = 9 000 samples" → "15 pumps × 1800 post-warm-up ticks = 27 000 samples" alongside the Item 6 native regen commit.** Flagged here rather than edited in this session because the edit isn't load-bearing until PO regen lands.

## Note for next session

The drift surface + reference artifacts are now in their production shape. Item 6 (PO 30-pump native regen) is the only remaining action from the 2026-06-02/2026-06-03 ADR 0008/0009 arc; once it lands, the lambda_scorer session can pick up the model artifacts unchanged. The `shared/drift.py` docstring + error-message strings now name the correct artifact, so the cold-start error path won't mislead. `context/model.md` line 24 needs a quick "5 pumps" → "15 pumps" sweep when the PO Item 6 commit goes in (flagged above; not edited here because it'd be a stale edit if PO bumps further).

