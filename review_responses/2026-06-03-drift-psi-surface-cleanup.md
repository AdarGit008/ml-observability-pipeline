# Review response — 2026-06-03: drift PSI surface cleanup (ADR 0009)

Gemini reviewed the packet at `review_packets/2026-06-03-drift-psi-surface-cleanup.md` on 2026-06-03 and **greenlit ADR 0009** with two action items folded into this session and one operational-sequence question to answer for the rollout.

ADR 0009 status flipped: `Accepted (PO sign-off 2026-06-03; Gemini approved 2026-06-03)`.

## Q1 — ADR clarity and three-place locking posture

**Part 1 — "Two lists, two purposes" paragraph at the top of ADR 0009.** Accepted. A new §"Principle (plain English)" section now sits between the metadata block and §Context. Its job is to land the statistical justification (rolling-window aggregation breaks IID; the same property that makes rolling features useful for prediction makes them useless for PSI) before the historical context. A new engineer 18 months from now opens the ADR and reads the *why* in the first paragraph instead of reverse-engineering it from §Decision.

**Part 2 — three-place locking (code + artifact + test) confirmed as sufficient.** No changes; the existing posture stands. Recording Gemini's table here for future reference:

| Location | Enforcement | Future-proofing value |
| --- | --- | --- |
| Code surface (`shared/features.py`) | Proximity of `PSI_FEATURE_NAMES` to `FEATURE_NAMES` | Immediate visual context; conscious routing decision on every feature addition. |
| Artifact emission (`model/train.py::write_artifacts`) | Structural divergence; no shared `feature_names` parameter | Strongest guard — actively prevents a future engineer from "DRYing" up the artifact generation and recoupling the lists. |
| Test surface (`local_runtime/tests/test_features.py`) | Strict-subset mathematical relation | CI enforcement; build fails with a pointer to ADR 0009 on any "make them identical" PR. |

## Other dispositions

- **Load reference guard.** Gemini called out the pre-ADR-0009 8-element rejection as "fantastic developer experience." Pinned by `test_load_reference_pre_adr_0009_eight_feature_list_raises`. No changes.
- **InfluxDB schema shrink (17 → 13 fields per point).** Confirmed safe; historical rows unaffected by dropped fields. Schema amendment in ADR 0005 §3 + §Addendum 2026-06-03 stands.
- **Dashboard readiness.** The dashboards session wires four `psi_*` panels from day one — no retroactive migration. Carried as a follow-up bullet in `context/drift.md` §Open questions.
- **Deferred items 2–7.** Gemini agrees none of them block this ADR.

## Q2 (Gemini's new question) — Rollout sequence and drift-monitoring blackout

> "Since `load_reference` now hard-rejects the old 8-element shape, what is the exact operational sequence for deploying this code alongside the PO-generated 30-pump reference to avoid a drift-monitoring blackout during the rollout?"

**Short answer: no blackout exists, because the (code, reference) pair is atomic per environment by construction.** The deploy unit always carries both the code and the reference from the same commit. There is no in-between state in which new code reads an old reference (or vice versa) on a live service.

Long-form sequence:

1. **Single commit ships code + sandbox reference together.** This session's commit carries (a) the updated `shared/drift.py` + `shared/features.py` (new `PSI_FEATURE_NAMES`, new validation), (b) the regenerated 12-pump sandbox `model.pkl` + `operational_reference_distribution.json` (both `v0.1.0-seed-0`, both 4-element-shaped). Any fresh checkout of HEAD or later has matching code + reference. There is no commit in repo history where `load_reference` validates against `PSI_FEATURE_NAMES` but the artifact still has the 8-element shape.

2. **PO regenerates production artifacts at 30 pumps natively** (Item 6 from the original brief, deferred). The command is `python -m model.train --n-pumps 30 --seed 0` on Windows. Output overwrites `model/artifacts/model.pkl` and `model/artifacts/operational_reference_distribution.json` in place; both keep `v0.1.0-seed-0`. PO commits the regenerated artifacts. Any fresh checkout post-regen has the production-sized artifacts; both shapes (12-pump and 30-pump) pass `load_reference`'s validation because both carry the 4-element `feature_names`. There is no operational difference between checking out the pre-regen vs post-regen commit from `load_reference`'s perspective — only the bin edges shift slightly to reflect 30-pump quantiles.

3. **Local mode rollout (this repo's `local_runtime`).** Local restart is `git pull && python -m local_runtime`. The service's `ScorerService.__init__` calls `load_reference()` once. With matching code + reference from the same commit, validation passes and PSI flows on the first compute tick. The atomic step is `git pull` — there is no window where one of the two is updated and the other isn't from the service's perspective.

4. **AWS mode rollout (future `lambda_scorer`).** The deploy zip (per ADR 0006 §Footprint + HANDOFF.md §6 Q3) bundles `shared/` + `lambda_scorer/` + `model/artifacts/` from the same commit. Terraform publishes the new function version; subsequent invocations cold-start with the new code + new reference together. Existing warm Lambda containers continue serving with their own (consistent, atomic) cold-start cache pair until they recycle — at which point the next cold start gets the new pair. Lambda's deploy model guarantees no mixed-shape state: a single zip carries a single (code, reference) pair, and a single function version always loads from a single zip. Old-version containers can run alongside new-version containers, but each container has internally consistent code + reference.

5. **The model_version cross-check is the safety net.** `_check_model_version_match` compares the `model_version` field embedded in both `model.pkl` and the reference JSON. A half-broken deploy (someone manually mixed in an old reference with the new model bundle) would `DriftError` at cold-start with a clear message naming both versions — fail-fast, not silent. The version-match check predates ADR 0009 (ADR 0007 §4) and stays operational after.

6. **Edge case — dev environments with stale local artifacts.** If a developer pulls HEAD but their working directory still has a stale `operational_reference_distribution.json` from a long-running branch (or from a manual partial copy), `load_reference()` fails-fast with the clear "rebuild via `python -m model.train`" DriftError. This is the desired behavior, not a blackout — the inconsistency surfaces at service init rather than producing silent autocorrelation noise downstream. Recovery is `python -m model.train`.

7. **Rollback path.** If for any reason the PO needs to revert to ADR 0008 semantics: revert the code commit AND restore the 8-element reference from git history. The reverse atomic move works because git preserves both shapes; the recovery is symmetric. No data migration; no special tooling.

The key architectural property is that the deploy artifact (git commit for local; Lambda zip for AWS) is *the* unit of consistency. The (code, reference) pair never decouples in flight.

## What's still pending after this response

- **Item 6 (PO native 30-pump regen).** Run on Windows; commit the production artifacts. PO can do this any time post-merge — the deploy property above guarantees no blackout regardless of when.
- **Item 7 (lambda_scorer deploy-zip recipe verification).** Read-only check against HANDOFF.md §6 Q3 and ADR 0006 §Footprint. The artifact filename (`operational_reference_distribution.json`) is unchanged; the recipe is mechanically correct.
- **Items 3, 4, 5 (5→15 ref pumps, banner comments, `model/artifacts/README.md`).** Independent of ADR 0009. PO's call when to schedule.

## Test count after this session

346 (post-2026-06-02 baseline) → **350 passed, 1 skipped** post-this-session. Net +4: `test_psi_feature_names_pinned`, `test_psi_feature_names_is_subset_of_feature_names`, `test_load_reference_pre_adr_0009_eight_feature_list_raises`, `test_build_point_psi_fields_do_not_include_rolling_features`. Structural-parity tests green throughout.
