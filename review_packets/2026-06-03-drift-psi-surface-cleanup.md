# Review packet — 2026-06-03: drift PSI surface cleanup (ADR 0009)

## TL;DR for the reviewer
- Closing the autocorrelated-PSI-threshold-semantics open question from ADR 0008 §Negative.
- ADR 0009 drops the four rolling features from the PSI surface entirely. They remain scorer inputs.
- One headline question for the reviewer (below).

## Reading order
1. `docs/adr/0009-psi-surface-vs-scorer-feature-set.md` — new ADR (the architectural decision: PSI surface ≠ scorer feature set).
2. `docs/sessions/2026-06-03-drift-psi-surface-cleanup.md` — session log + verification + the answer to your 2026-06-02 dashboard follow-up.
3. `docs/adr/0005-shared-mode-parity-package-and-subscriber-topology.md` §3 + §Addendum 2026-06-03 — the InfluxDB schema impact (17 → 13 fields per point on compute ticks).
4. `shared/features.py` — the new `PSI_FEATURE_NAMES` constant lives next to `FEATURE_NAMES`. Module docstring documents the asymmetry.
5. `shared/drift.py` — `compute_psi` now iterates `PSI_FEATURE_NAMES`; `load_reference` validates the on-disk reference against it (a pre-ADR-0009 8-element reference is rejected with a clear error pointing at the rebuild command).
6. `model/train.py` — `compute_reference_distribution` slices the 8-column X matrix to the 4-column PSI surface; `write_artifacts` lost its `feature_names` parameter because bundle (8) and reference (4) now diverge structurally.
7. `local_runtime/influx_writer.py` — `build_point` emits 4 `psi_*` fields.
8. Tests: `local_runtime/tests/test_features.py` (the asymmetry pin), `local_runtime/tests/test_drift_load.py` (pre-ADR-0009 rejection guard + rewritten `test_demo_paced_healthy_psi_stable`), `local_runtime/tests/test_influx_writer.py` (schema-shrink regression guard), `model/tests/test_train.py` (reference shape + asymmetric artifact `feature_names`).

## Headline question

**Q1 — Did we get the asymmetry between scorer-input features and PSI-surface features clearly enough in ADR 0009?**

The ADR's principle is "scorer input ≠ PSI surface, by design." The architectural reason is that the same property (autocorrelation introduced by rolling-window aggregation) is what makes rolling features useful as model inputs (temporal smoothing for prediction) AND useless for PSI (broken IID for drift detection). The decision boundary is: rolling features stay where they earn their keep (scorer input) and get dropped where they don't (PSI surface).

The asymmetry is locked in three places:

1. **Code surface (`shared/features.py`):** `PSI_FEATURE_NAMES` lives next to `FEATURE_NAMES` in the same parity-boundary file. Module docstring + inline comments describe both contracts.
2. **Artifact emission (`model/train.py`):** `write_artifacts` lost the `feature_names` parameter because the model bundle (8) and reference JSON (4) now structurally diverge. Bundle hardcodes `FEATURE_NAMES`; reference hardcodes `PSI_FEATURE_NAMES`. Forcing the artifact lists to differ at the function boundary means a future "let me unify these" PR has to confront ADR 0009.
3. **Tests (`local_runtime/tests/test_features.py`):** `test_psi_feature_names_is_subset_of_feature_names` pins the strict-subset relation in both directions — PSI can't have names that `FEATURE_NAMES` doesn't (KeyError at compute time), and PSI can't be equal-or-superset (ADR violation). A `PSI_FEATURE_NAMES` extension has to update this test AND the ADR.

Specifically asking: does the ADR's §Decision section land the principle, or does it need a clearer "two lists, two purposes, here's why" paragraph at the top? And is the three-place locking (code + artifact + test) overkill, sufficient, or insufficient as a future-proofing posture?

## Dispositions of prior reviews

- **2026-06-02 review Q1 (headline endorsement of dropping rolling features from PSI):** **Accepted, this session's deliverable.** ADR 0009 ships the decision; tests + artifacts regenerated.
- **2026-06-02 review follow-up dashboard question** (*"does your current dashboard architecture easily support rendering PSI metrics for a subset of features while ignoring others?"*): **Answered inline in the session log §"Gemini's follow-up dashboard question."** Short version: no UI tweak required — the dashboards session hasn't started; it will wire its panels against the four surviving `psi_*` field names from day one.

## Confidence assessment

| Decision | Confidence | Why |
|---|---|---|
| Drop rolling features from PSI surface | High | Direct closure of an open question Gemini already endorsed (2026-06-02 Q1). Hard four-key contract enforced in the regression guard. |
| `PSI_FEATURE_NAMES` in `shared/features.py` | High | Proximity to `FEATURE_NAMES` makes future additions obvious. Structural test pins the strict-subset relation. |
| Write_artifacts asymmetric feature_names | Medium | Dropping the `feature_names` parameter is a small breaking change to a function only `model.train.main()` calls today. Existing test (`test_write_artifacts_round_trip`) updated to assert both shapes; round-trip green. |
| Reference JSON shape change is detectable at load | High | `load_reference` rejects pre-ADR-0009 (8-element) references with a clear error message pointing at the rebuild command. New test pins this. |
| Sandbox 12-pump corpus stays | Medium | Same constraint and same AUC as ADR 0008. PO regenerates at 30 pumps natively for production. |

## What's NOT in this session (deferred)

Items 2–7 of the original ADR-0008-disposition brief are deferred to a follow-up session at PO's discretion. None of them block Item 1's ship:

- Item 2 (Gemini's dashboard follow-up answer): answered inline in the session log — no separate work needed.
- Item 3 (5 → 15 operational reference pumps): pure constant change + retrain. Narrative-alignment refinement; doesn't move PSI numbers meaningfully on healthy fleets (autocorrelation floor was structural, not sample-size-bounded).
- Item 4 (banner comments above `_training_profiles` + `_operational_profiles`): five-minute change. Worth doing but not load-bearing on this ADR.
- Item 5 (`model/artifacts/README.md`): one-paragraph sandbox-vs-production disclaimer. Independent of this ADR.
- Item 6 (PO regenerates production artifacts at 30 pumps natively): PO-side, sequenced after this ADR so the production artifact has the four-key shape from day one.
- Item 7 (verify lambda_scorer deploy-zip recipe): read-only check. The artifact filename (`operational_reference_distribution.json`) is unchanged — only its on-disk shape changed — so the recipe is mechanically correct.
