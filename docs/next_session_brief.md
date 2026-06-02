# Next session brief — lambda_scorer MVP: cold-start + per-pump score path (+ resolve §6 Q5 at plan-step)

## Goal

Stand up the `lambda_scorer/` module's MVP: cold-start path that loads the bundled model + operational PSI reference, hot path that handles one IoT-Rule-delivered MQTT message per invocation (parse → fetch per-pump feature window from DynamoDB → append + extract features → score → write state record). Resolve HANDOFF.md §6 Q5 (DynamoDB schema) at plan-step — `context/lambda_scorer.md` §Open questions still lists Q5 as the blocking decision. PSI and SNS deferred to a follow-on session unless plan-step says otherwise.

After this session, the AWS-mode scoring path is wired end-to-end except for drift detection (PSI) and alerting (SNS), which carry into the next lambda_scorer session.

## How to start this session — plain-language walkthrough FIRST

Same rule. Claude walks PO through Q5 + the MVP scope + the deploy-zip recipe in plain language BEFORE any code. ONE paragraph each.

If anything in the in-scope items below has changed meaning since the brief was written (e.g., a Q5 option went stale, the operational reference shape changed), say so before greenlight.

## In-scope items (in order)

### Item 1 — Resolve HANDOFF.md §6 Q5 (DynamoDB schema)

What changes: pick one of the three options the §6 Q5 footprint lists (or a refinement), document access patterns explicitly, capture as either a session-log decision OR a new ADR 0010 if structurally novel (recommendation: ADR 0010 — this is the project's first DynamoDB schema decision and sticks for the project's lifetime). `context/_interfaces.md` §"DynamoDB schema" gets filled in from TBD to the resolution.

Three options on the table per HANDOFF.md §6 Q5:
- **A.** `PK = pump_id, SK = timestamp` (one row per reading; hot-pump fan-out is wide; simplest access pattern)
- **B.** `PK = pump_id#bucket(1min), SK = timestamp` (better partition dispersion + locality but query complexity grows)
- **C.** `PK = pump_id, SK = state` (single mutable row per pump; loses raw-reading history; cheapest writes)

Claude's leaning at plan-step: **A** for MVP simplicity (15 pumps × 30 msg/min = 450 RPS at most — well inside DynamoDB's per-partition 1000 WCU floor; "hot pump fan-out" isn't a real problem at this fleet size). Option B is over-engineering for the demo scale. Option C drops the rolling-window source-of-truth.

Open at plan-step: does the rolling 1-hour PSI window live in DynamoDB (read-path each invocation) or in-process memory (cold-start cache)? The latter forces the Lambda to re-warm on every cold start; the former adds a query per invocation. PO call.

### Item 2 — `lambda_scorer/` scaffold

Create `lambda_scorer/__init__.py`, `lambda_scorer/handler.py`, `lambda_scorer/tests/__init__.py`, `lambda_scorer/tests/test_handler.py`. Pattern mirrors `local_runtime/` — same import-from-shared discipline.

### Item 3 — Cold-start handler

Top-level module loads (run once per container, not per invocation):
- Bundled `model/artifacts/model.pkl` via `shared.score` (already importable as peer per ADR 0005)
- Bundled `model/artifacts/operational_reference_distribution.json` via `shared.drift.load_reference()` — version-match against the model bundle, raises `DriftError` on desync (ADR 0007)
- DynamoDB resource client (`boto3.resource('dynamodb')`)

Per `context/lambda_scorer.md` §Resource sizing: 512 MB memory ceiling; bundled-pickle deploy zip per HANDOFF §6 Q3 default. Cold-start latency target: <2 s per `context/lambda_scorer.md` §Open questions (if exceeded, switch to S3 cold-load — but measure first).

### Item 4 — Hot-path handler

Per invocation:
1. Parse IoT Rule event envelope → extract `pump_id`, `ts`, raw telemetry per `context/_interfaces.md` §Telemetry payload.
2. Fetch per-pump feature window from DynamoDB (shape depends on Item 1 resolution).
3. Append latest reading to the window (preserve 150-sample rolling cap per `shared.features.extract_features` window semantics).
4. Call `shared.features.extract_features(window)` → 8-feature dict.
5. Call `shared.score.score(features)` → P(failure_48h) ∈ [0, 1].
6. Write state record: `{pump_id, ts, score}` (PSI + alert deferred to follow-on session unless scope expands at plan-step).

### Item 5 — Structural-parity guard

Add `test_structural_parity_lambda_scorer_imports_from_shared` to `lambda_scorer/tests/test_handler.py` mirroring the three local_runtime guards:
- `lambda_scorer.handler` imports `shared.score.score` (not a vendored copy)
- `lambda_scorer.handler` imports `shared.drift.load_reference` (not a vendored copy)
- `lambda_scorer.handler` imports `shared.features.extract_features` (not a vendored copy)

Per ADR 0005's parity-vendoring guard pattern.

### Item 6 — Tests with `moto`

Use `moto` to mock DynamoDB. No real AWS calls in tests. Cover:
- Cold-start happy path (model + reference load clean, version match OK)
- Cold-start version mismatch (`DriftError` raised)
- Hot-path happy path (event → window read → score → state write)
- Hot-path malformed event (missing pump_id, missing telemetry field)
- Hot-path version drift since cold-start (re-validate or assume cold-start frozen? plan-step call)

## Component

`lambda_scorer` — **Tier 2b parity-touching** (imports `shared/features.py + shared/score.py + shared/drift.py`). See [[ml_obs_pipeline_shared_parity_boundary]] — the contract stays at `FEATURE_NAMES` (8) + `PSI_FEATURE_NAMES` (4). No parity-boundary edits this session unless plan-step surfaces a gap.

[[ml_obs_pipeline_parity_load_check]] applies — STOP if any Tier 2b load is missing from this brief.

## Loads

- **Tier 1:** `context/_global.md`, `context/_interfaces.md`.
- **Tier 2:** `context/lambda_scorer.md`.
- **Tier 2b parity:** `shared/{features,score,drift}.py` (all three) + ADR 0005.
- **Cross-component:** `context/model.md` (the model bundle Claude is loading), `context/drift.md` (the reference Claude is loading).
- **ADRs:** 0005 (parity boundary), 0006 §Footprint (deploy-zip footprint measurement — ~124 MB unzipped, Lambda 250 MB unzipped ceiling, ~50% headroom), 0007 (PSI cadence + load_reference contract), 0008 (operational reference), 0009 (PSI surface ≠ scorer feature set).
- **Open question being resolved:** HANDOFF.md §6 Q5 (DynamoDB schema).
- **Memory:** [[ml_obs_pipeline_shared_parity_boundary]], [[ml_obs_pipeline_parity_load_check]], [[ml_obs_pipeline_fuse_write_truncation]] (default to outputs/cp regardless of file size, per 2026-06-04 update), [[ml_obs_pipeline_git_on_windows]].

## Reference

- `HANDOFF.md` §6 Q3 (model packaging — bundled default), §6 Q4 (reference storage — bundled by default; the artifact name is `operational_reference_distribution.json` post-ADR-0008), §6 Q5 (DynamoDB schema — being resolved this session).
- `context/_interfaces.md` §"DynamoDB schema" (TBD → gets filled in), §"Lambda scorer event envelope", §"Lambda scorer DynamoDB writes".
- `context/lambda_scorer.md` (purpose, interfaces, resource sizing, open questions).
- `model/artifacts/README.md` §"Two pump counts, two purposes" — the operational reference is invariant under `--n-pumps` (15 pumps × 1800 ticks = 27 000 samples post-Item-3 of the 2026-06-04 session).
- `shared/drift.py::load_reference` — exact validation behaviour for the cold-start path (4-element `feature_names`, `model_version` match against `model.pkl`, lazy-imported joblib in the version-check branch).
- `local_runtime/service.py` — the structural cousin Claude is mirroring (same cold-start pattern, different runtime).
- `docs/sessions/2026-06-04-followup-items-3-4-5-7.md` §"Gemini review highlights" — read the disagreement-recorded Q1 (5→15 bump as session-log note) before touching `OPERATIONAL_REFERENCE_PUMPS`.

## Constraints

- **FUSE write truncation** (per [[ml_obs_pipeline_fuse_write_truncation]] 2026-06-04 update). Default to outputs/cp regardless of file size — even 76-line files have been hit. `Edit` is only safe for single-call edits to files under ~50 lines.
- **Parity boundary unchanged.** `shared/{features,score,drift}.py` stays at the locked contract. No edits there this session.
- **Bash 45 s cap.** No long-running training; use `moto` for all AWS mocks, no real AWS calls.
- **Lambda 512 MB memory + 250 MB unzipped deploy zip.** ADR 0006 §Footprint measured ~124 MB; bundling stays viable. Verify zip size at session close.
- **No real AWS spend.** All tests run against `moto`. No `aws sdk` calls outside of mocked test paths.
- **PO does git on Windows** per [[ml_obs_pipeline_git_on_windows]]. Commit drafts include the canonical PowerShell sequence per DEV_NORMS §7 (new 2026-06-04 norm).
- **Test count baseline: 350 passed + 1 skipped (post-2026-06-04).** Expect net delta = +N (Item 6's test additions). Structural-parity tests must stay green.

## Definition of done

- ✅ HANDOFF.md §6 Q5 resolved — captured as ADR 0010 (recommended) or session-log decision.
- ✅ `context/_interfaces.md` §"DynamoDB schema" updated from TBD to the resolution.
- ✅ `lambda_scorer/` scaffolded with `__init__.py`, `handler.py`, tests.
- ✅ Cold-start path tested with `moto`: model + reference loaded, version-match validated.
- ✅ Hot-path tested: IoT Rule event → DynamoDB read → feature extract → score → DynamoDB write. PSI + SNS NOT in scope.
- ✅ Structural-parity test green (`test_structural_parity_lambda_scorer_imports_from_shared` + the existing three local_runtime guards).
- ✅ Test count = 350 + 1 skipped + N new (Item 6).
- ✅ Deploy-zip footprint verified ≤ ~125 MB unzipped per ADR 0006 §Footprint.
- ✅ Session log + review packet written. Commit draft includes the PowerShell sequence per DEV_NORMS §7 (new norm).
- ⏳ Carry-forward: PSI compute + SNS alert + state-record-with-psi-dict in a follow-on lambda_scorer session.

## Open questions to raise with PO at plan-step

1. **§6 Q5 resolution** — Claude's lean is option A (`PK = pump_id, SK = timestamp`) for MVP simplicity. PO call.
2. **PSI window in DynamoDB or in-process memory?** Affects table design and cold-start time. Recommend: DynamoDB-backed (idempotent across cold starts; matches "stateless Lambda" north star).
3. **Q5 → ADR 0010 or session-log note?** Recommend ADR 0010 — first DynamoDB schema decision, sticks long-term. Compare with the 2026-06-04 session's Q1 disposition: that one was a refinement of an existing PO call; this one is a fresh structural decision.
4. **MVP scope discipline.** PSI + SNS deferred? Recommend yes — bundling them in this session pushes scope past one-day work. Carry to a follow-on lambda_scorer session.
5. **Deploy-zip recipe re-verification.** ADR 0006 §Footprint table was updated this session (model.pkl ~290 KB + operational reference ~2.2 KB). Re-measure unzipped Lambda deploy size at session close.

## Tone note for the session

2026-06-04 closed cleanly; one disagreement recorded with Gemini (Q1 bump-vs-amendment). This session adds a new structural decision (Q5) to the locked-decision list. Plan-step discipline matters MORE here than in routine sessions because the schema decision sticks for the project's lifetime — a re-do means a data migration, not a code rewrite. ADR 0010 is the likely deliverable alongside the code.

Reminder: outputs/cp pattern is the default. Do not reach for `Edit` on D:\ files even speculatively — the 76-line incident on `context/model.md` proved the threshold is lower than the prior memory claimed. Per the 2026-06-04 update to [[ml_obs_pipeline_fuse_write_truncation]].
