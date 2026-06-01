# 2026-06-03 — drift: PSI surface ≠ scorer feature set (ADR 0009)

## Component
`drift` (Tier 2b parity-touching: `shared/{features,score,drift}.py` + ADR 0005). Touches `shared/features.py`, `shared/drift.py`, `model/train.py`, `local_runtime/influx_writer.py`. Tests in `local_runtime/tests/{test_drift_load,test_features,test_shared_stubs,test_influx_writer,test_service}.py` + `model/tests/test_train.py`.

## Intent
Close the autocorrelated-PSI-threshold-semantics open question carried in `context/drift.md` (Gemini Q1 of the 2026-06-02 review headline + ADR 0008 §Negative): drop the four rolling features from the PSI surface entirely. Rolling features remain scorer inputs (where their autocorrelation is a feature — temporal smoothing — not a bug). Pin the asymmetry between scorer input set and PSI surface in a new ADR 0009 and a structural-parity-friendly test so a future "let me harmonize these" PR has to update the ADR.

This is Item 1 of the brief's seven-item disposition list. Items 2–7 (Gemini's dashboard follow-up answer, 5→15 reference pump bump, banner comments, `model/artifacts/README.md`, native 30-pump regen, deploy-zip recipe verification) are deferred to a follow-up session at PO's discretion.

## PO decisions at plan-step (this session's brief §"Open questions")

1. **ADR strategy** → **new ADR 0009** (recommended). ADR 0008's reference-source split and ADR 0009's PSI-surface scope are architecturally distinct; two clean ADRs > one mixed-purpose one. Same logic as the 0006-vs-0008 call last session.
2. **`PSI_FEATURE_NAMES` location** → **`shared/features.py`** (recommended). Next to `FEATURE_NAMES`, same parity-boundary file. Two feature lists read symmetrically; a future addition has to touch the obvious place.

## What changed

### Code

- `shared/features.py` (+~30 lines): new `PSI_FEATURE_NAMES = ("vibration_amp", "bearing_temp", "motor_current", "rpm")` constant next to `FEATURE_NAMES`. Module docstring + inline comments document the asymmetry. The existing `RAW_SIGNAL_FIELDS` constant gets a clarifying comment ("today equals `PSI_FEATURE_NAMES`, but conceptually distinct: simulator wire format vs PSI drift surface").
- `shared/drift.py`:
  - Import switched from `FEATURE_NAMES` to `PSI_FEATURE_NAMES`.
  - Module docstring §"PSI feature surface (ADR 0009)" added.
  - `compute_psi` iterates `PSI_FEATURE_NAMES`; defensive zero-return uses `PSI_FEATURE_NAMES`. Docstring updated to describe the new 4-key return shape.
  - `load_reference` validation checks `feature_names` against `PSI_FEATURE_NAMES`; mismatch error message points at the rebuild command and names ADR 0009 explicitly.
- `model/train.py`:
  - Import grows `PSI_FEATURE_NAMES`. `from typing import Iterable` removed (was only used by the dropped `feature_names` parameter on `write_artifacts`).
  - `compute_reference_distribution` slices the 8-column X matrix down to the 4 PSI surface columns via `FEATURE_NAMES.index(name)` lookup. Returned dict has exactly 4 keys.
  - `write_artifacts` drops the `feature_names` parameter (model bundle and reference now diverge structurally). Bundle hardcodes `FEATURE_NAMES` (8 — scorer input contract); reference JSON hardcodes `PSI_FEATURE_NAMES` (4 — drift surface contract). Docstring documents the asymmetry.
- `local_runtime/influx_writer.py`:
  - Module docstring §Schema decision updated: 17 → 13 fields on compute ticks. Pre/post comparison documented.
  - `build_point` iterates `PSI_FEATURE_NAMES` for the `psi_*` fields. The 8-feature-dump loop stays at `FEATURE_NAMES` (scorer input contract unchanged).

### Artifacts

- `model/artifacts/model.pkl` — regenerated (290 KB, AUC 0.9972 on 3/12 hold-out, version `v0.1.0-seed-0`). Sandbox-runtime corpus is 12 pumps (45 s bash cap unchanged from ADR 0008 session). PO regenerates at 30 pumps natively on Windows for production (Item 6, deferred).
- `model/artifacts/operational_reference_distribution.json` — regenerated (**2.2 KB, half the pre-ADR-0009 size** because the surface shrank from 8 to 4 features), 9 000 samples (5 pumps × 1800 post-warm-up ticks), version `v0.1.0-seed-0`. Bin counts confirm equal-frequency binning (900/bin × 10 bins = 9 000 per feature).

### Tests

- `local_runtime/tests/test_features.py` — **+2 new tests** (the ADR 0009 asymmetry pins):
  - `test_psi_feature_names_pinned`: pins `PSI_FEATURE_NAMES` tuple value.
  - `test_psi_feature_names_is_subset_of_feature_names`: structural invariant — PSI surface ⊊ scorer input set. Catches both directions (PSI got a name not in `FEATURE_NAMES` → KeyError at compute time; PSI got equal-or-superset → ADR 0009 violation).
- `local_runtime/tests/test_drift_load.py` — **+1 new test** + `test_demo_paced_healthy_psi_stable` rewritten:
  - `_write_valid_reference` helper now writes `PSI_FEATURE_NAMES`-shaped references.
  - `test_load_reference_pre_adr_0009_eight_feature_list_raises`: regression guard for the silent-divergence case (a stale 8-element reference would re-introduce the autocorrelation noise problem ADR 0009 closes).
  - `test_load_reference_returns_dict_usable_by_compute_psi`: assertion updated to `set(psi.keys()) == set(PSI_FEATURE_NAMES)`.
  - `test_demo_paced_healthy_psi_stable` (ADR 0008's regression guard): retired the rolling-features-soft-bound branch. Now asserts `set(psi.keys()) == set(PSI_FEATURE_NAMES)` (hard four-key contract — rolling features simply aren't in the dict) AND `psi[name] < 0.10` on every key.
- `local_runtime/tests/test_shared_stubs.py` — synthetic-reference helpers + PSI assertions migrated from `FEATURE_NAMES` to `PSI_FEATURE_NAMES`. No new tests; the count is unchanged.
- `local_runtime/tests/test_influx_writer.py` — **+1 new test**:
  - `test_build_point_psi_fields_do_not_include_rolling_features`: regression guard for the schema shrink.
  - `test_build_point_field_count_with_psi` updated 17 → 13.
  - `_psi_all_set` helper + PSI iteration in build_point assertions migrated to `PSI_FEATURE_NAMES`.
- `local_runtime/tests/test_service.py` — `test_service_handle_psi_dict_present_when_compute_fires` and `test_service_handle_psi_is_none_on_non_compute_ticks` assertions migrated to `PSI_FEATURE_NAMES`. No new tests.
- `model/tests/test_train.py`:
  - `PSI_FEATURE_NAMES` added to import.
  - `test_compute_reference_distribution_per_feature_shape` asserts keys == `PSI_FEATURE_NAMES` AND rolling features explicitly NOT in the output (the asymmetry pin at the artifact-emission boundary).
  - `test_write_artifacts_round_trip` asserts `bundle["feature_names"] == list(FEATURE_NAMES)` (8) AND `ref_doc["feature_names"] == list(PSI_FEATURE_NAMES)` (4). The two artifacts' feature-name lists are now structurally different.

### Documentation

- `docs/adr/0009-psi-surface-vs-scorer-feature-set.md` — **new ADR**. Four decisions: (1) drop rolling features from PSI surface, (2) `PSI_FEATURE_NAMES` lives in `shared/features.py`, (3) `compute_psi` iterates the new constant, (4) `compute_reference_distribution` slices and `write_artifacts` emits the asymmetric `feature_names` lists. Alternatives considered: lift bands, downsample rolling features to IID, replace PSI with moving-average band, or keep as-is. Each rejected on cost-benefit; the asymmetry approach minimises code surface and operator cognitive load while preserving rolling features as model inputs.
- `docs/adr/0005-shared-mode-parity-package-and-subscriber-topology.md` — §3 schema line amended (17 → 13 fields per point + pointer to ADR 0009). New §Addendum 2026-06-03 (ADR 0009) section at the end with the long-form schema-impact + dashboards-pickup notes.
- `context/drift.md` — §"Current state" notes the ADR 0009 ship; §Interfaces updated to 4-key PSI dict; §Invariants gains the PSI surface ⊂ scorer input set entry; §Parameters describes the 4-feature surface; §"Open questions" → autocorrelated-PSI threshold semantics closed; §Related ADRs gains ADR 0009.
- `context/model.md` — §"Current state" notes ADR 0009 (artifact size halved); §Interfaces > Artifacts describes the bundle's 8-element vs reference's 4-element `feature_names` split; §"Open questions" → autocorrelated rolling-feature PSI closed; §Related ADRs gains ADR 0009.
- `context/local_runtime.md` — §"Current state" notes ADR 0009 row; §Interfaces field count 17 → 13; §"Mode parity invariant" notes the second locked-contract feature list (`PSI_FEATURE_NAMES` next to `FEATURE_NAMES`); §Tests adds the asymmetry pin; §"Open questions" → autocorrelated rolling-feature PSI closed; §Related ADRs + §Session log updated.

## Gemini's follow-up dashboard question (from 2026-06-02 review response)

Gemini asked: *"Does your current dashboard architecture easily support rendering PSI metrics for a subset of features while ignoring others, or will that require a tweak to the UI layer?"*

**Answer:** No tweak required — the dashboards session hasn't started, so there's no existing UI layer to tweak. The dashboards session will wire its panels against the four surviving `psi_*` field names from day one. PLAN.md §2.7 bands (< 0.10 / 0.10–0.25 / > 0.25) apply uniformly across all four — no conditional band logic per panel.

**Follow-up note for the dashboards session:** the PSI panel set is `psi_vibration_amp`, `psi_bearing_temp`, `psi_motor_current`, `psi_rpm` (in `FEATURE_NAMES` order). Historical InfluxDB rows that carry the four retired `psi_*` field names (`psi_vibration_amp_mean_5m` + siblings) are unaffected by this change — the writer simply stops emitting them going forward, and any queries against those field names go from "last value = recent reading" to "last value = old reading," which is correct behaviour for retired channels.

## Verification

### Acceptance criterion (this session's DoD)

- ✅ ADR 0009 written; ADR 0005 §3 amended with pointer.
- ✅ `shared/drift.py::compute_psi` iterates `PSI_FEATURE_NAMES` (4 names). Structural-parity tests still green.
- ✅ Operational reference regenerated via `model.train` with 4-key shape. Same `model_version` (`v0.1.0-seed-0`). Bin counts = 900/bin verified (= 9 000 / 10).
- ✅ `local_runtime/influx_writer.py` emits 4 `psi_*` fields. Field count on compute ticks = 13; non-compute ticks = 9 (unchanged).
- ✅ `test_demo_paced_healthy_psi_stable` updated to hard four-key contract + `psi[name] < 0.10` on every key.
- ✅ `test_psi_feature_names_is_subset_of_feature_names` pins the structural asymmetry.
- ✅ `context/drift.md` autocorrelated-PSI open question closed; ADR 0009 cross-references in place.
- ✅ `context/model.md` rolling-features-in-PSI-output reference updated.
- ✅ Gemini's dashboard follow-up answered (above) + dashboards-session follow-up bullet documented.
- ✅ Full pytest suite: **350 passed, 1 skipped** (vs 346 + 1 baseline post-2026-06-02 — net +4 from the new asymmetry-pin tests).
- ⚠️ PO regenerates production artifacts at 30 pumps natively (Item 6 of the brief — deferred). The committed artifacts here are the sandbox-runtime 12-pump variant from ADR 0008's regen pattern.

### PSI measurement (post-ADR-0009)

Re-ran the `test_demo_paced_healthy_psi_stable` harness against the regenerated operational reference (5 pumps × 1800 ticks, seed 0). Single HEALTHY pump `P-99` seed 42, 1800 post-warm-up samples:

| Feature        | PSI    | Band   |
|----------------|--------|--------|
| vibration_amp  | < 0.10 | STABLE |
| bearing_temp   | < 0.10 | STABLE |
| motor_current  | < 0.10 | STABLE |
| rpm            | < 0.10 | STABLE |
| *(rolling features)* | *— (not in PSI dict)* | *—* |

Hard four-key contract enforced by the test. Rolling features are not in the dict at all (the surface shrink eliminates the autocorrelation noise structurally rather than tolerating it via threshold widening).

### Test suite (full run from /tmp copy, FUSE workaround)

`pytest -p no:cacheprovider --tb=short -q` from the sandbox /tmp mirror: **350 passed, 1 skipped in 15.16s**.

Net delta from the 2026-06-02 baseline of 346 passed:
- +1 `test_psi_feature_names_pinned`
- +1 `test_psi_feature_names_is_subset_of_feature_names`
- +1 `test_load_reference_pre_adr_0009_eight_feature_list_raises`
- +1 `test_build_point_psi_fields_do_not_include_rolling_features`

Structural-parity tests (`test_structural_parity_no_vendoring` + siblings) green throughout — the parity boundary at `shared/{features,score,drift}.py` is unchanged in shape, only in the feature subset that drift iterates.

## Open follow-ups (carry into next session)

1. **Items 2–7 of the original ADR-0008-disposition brief**: Item 3 (5 → 15 operational reference pumps), Item 4 (banner comments above `_training_profiles` and `_operational_profiles`), Item 5 (`model/artifacts/README.md` documenting sandbox-vs-production), Item 6 (PO regenerates production artifacts at 30 pumps natively), Item 7 (verify the lambda_scorer deploy-zip recipe picks up the new artifact name). Item 2 (Gemini's dashboard follow-up) is closed inline above; Item 1 (drop rolling features from PSI surface) is closed by this session.
2. **Sandbox-runtime corpus is 12 pumps, not 30.** Same FUSE + bash 45 s constraint as ADR 0008. AUC stays at 0.997. PO regenerates at 30 pumps natively for the production artifact; same `v0.1.0-seed-0` tag either way.
3. **Lambda deploy-zip recipe verification** (Item 7) is a read-only check against HANDOFF.md §6 Q3 and ADR 0006 §Footprint. The artifact filename (`operational_reference_distribution.json`) is unchanged by this session — only its on-disk shape changed — so the recipe is mechanically correct. Carry as a one-line verification step in the lambda_scorer session brief.

## Sandbox-runtime caveats

- **FUSE write-truncation hit `Edit` on the four touched Python files (`shared/features.py`, `shared/drift.py`, `local_runtime/influx_writer.py`, `model/train.py`)** mid-session — first noticed when `python3 -m model.train` failed with `SyntaxError: '(' was never closed` at line 691. Recovery: wrote complete file contents to `/sessions/.../outputs/` via the Write tool's outputs-path, then `cp` to D:\. Same workaround as ADR 0008's `model/train.py` regen. All test-file edits used outputs/cp from the start to avoid the truncation entirely.
- **`sandbox-bash 45 s cap`** is unchanged; the 12-pump training fits with ~5 s margin. PO's native 30-pump regen sits outside the cap.

## Commit-message draft

```
drift: shrink PSI surface to four raw features (ADR 0009)

Closes the autocorrelated-PSI-threshold-semantics open question
carried in context/drift.md (Gemini Q1 of 2026-06-02 review + ADR
0008 §Negative). Per-pump PSI on the four rolling features was
structurally autocorrelation-bounded above the 0.10 STABLE band on
healthy fleets (0.10–0.40 across 10 test seeds; the 149/150-overlap
windows violate PSI's IID assumption). Increasing reference pump
count from 5 to 50 moved worst-case PSI < 0.05 — floor is structural,
not sample-size-bounded. ADR 0009 drops the four rolling features
from the PSI surface entirely; they remain scorer inputs.

* New PSI_FEATURE_NAMES = (vibration_amp, bearing_temp,
  motor_current, rpm) in shared/features.py — strict subset of
  FEATURE_NAMES.
* shared/drift.py: compute_psi + load_reference iterate /
  validate against PSI_FEATURE_NAMES. Pre-ADR-0009 references
  (8-element feature_names) rejected at load.
* model/train.py: compute_reference_distribution slices the
  8-column X matrix to the 4-column PSI surface. write_artifacts
  drops the feature_names parameter — bundle is FEATURE_NAMES
  (scorer input), reference JSON is PSI_FEATURE_NAMES (drift
  surface).
* local_runtime/influx_writer.py: build_point emits 4 psi_*
  fields. InfluxDB schema 17 → 13 fields per point on compute
  ticks (ADR 0005 §3 amended).
* Artifacts regenerated (sandbox 12-pump, AUC 0.9972, same
  v0.1.0-seed-0). PO regenerates 30 pumps natively for prod.
* Tests: +4 (2 ADR 0009 asymmetry pins, 1 pre-ADR-0009
  rejection guard, 1 schema-shrink regression guard). 346 → 350
  passed, 1 pre-existing skip unchanged.

Refs: ADR 0009 (new), ADR 0005 (§3 amend + §Addendum), ADR 0008
(closes §Negative autocorrelation follow-up).
```
