# 2026-06-02 — lambda_scorer: MVP cold-start + per-pump score path

## Component
`lambda_scorer` (Tier 2b parity-touching: imports `shared.features.extract_features`, `shared.score.score`, and `shared.drift.load_reference` as peers per ADR 0005). Parity boundary unchanged — no edits to `shared/{features,score,drift}.py`. Structural-parity guards from `local_runtime/tests/test_service.py::test_structural_parity_*` mirrored as three new tests under `lambda_scorer/tests/test_handler.py`.

## Intent
Stand up the `lambda_scorer/` module's MVP: cold-start path (model + operational reference load with `model_version` cross-check per ADR 0007) and per-pump score path (parse IoT-Rule event → query feature window from DynamoDB → extract features → score → write reading row + STATE row per ADR 0010). Resolve HANDOFF.md §6 Q5 at plan-step. PSI compute + SNS publish explicitly deferred to a follow-on session.

## PO decisions at plan-step

1. **§6 Q5 disposition — option A + STATE-row sibling, captured as ADR 0010.** PO greenlit Option A (`PK=pump_id, SK=sk` where `sk` is ISO-8601 ts for reading rows, literal `"STATE"` for the STATE row). At 15 pumps × ~0.5 WCU/sec/pump we're four orders of magnitude under DynamoDB's per-partition floor — Option B's bucketing solves a phantom problem; Option C drops the history both MVP and the PSI follow-on need. STATE-row sibling adds one PutItem per invocation, unblocks the dashboards adapter (single `BatchGetItem` over 15 STATE rows on each panel refresh instead of 15 per-pump latest-row queries). Captured as ADR 0010 — first DynamoDB schema decision, sticks for project lifetime.
2. **PSI window storage — DynamoDB-backed (forward commitment).** PO greenlit: PSI follow-on widens the same `Query` from `Limit=150` to `Limit=1800`. In-process-memory was mathematically wrong (at 7.5 inv/sec across 15 pumps no single Lambda container reliably sees a full per-pump history; cold starts wipe the deque). DynamoDB-backed makes the handler a pure function from `(event, table state)` → `(table writes)` — cold containers and warm containers compute the same answer.
3. **MVP scope discipline — PSI + SNS deferred.** PO confirmed: this session ships cold-start + score path only. Reference is loaded at cold-start so the follow-on plugs PSI in without re-touching cold-start; STATE-row schema is extension-friendly (`latest_psi` + `alert_flag` are additive attributes).
4. **STATE row in this MVP session.** PO greenlit: ~5 lines of handler code, zero parity-boundary implications, unblocks dashboards cleanly. Shape extends additively when PSI lands.

## What changed

### New code

- `lambda_scorer/__init__.py` (24 lines) — module purpose + MVP-vs-follow-on scope statement + entry-point pointer.
- `lambda_scorer/handler.py` (250 lines) — cold-start (eager `load_reference()`; module-level boto3 resource bound to `DDB_TABLE_NAME`) + hot-path (event parse → `Query` 150 reading rows filtered by `sk begins_with "2"` → append → `extract_features` → `score` → reading-row + STATE-row `PutItem`). Imports kept inside `{stdlib, boto3, shared.*}` per the ADR 0006 §Q4 footprint envelope. `EventParseError` (ValueError subclass) for missing-field validation. `_to_decimal` helper passes through `str()` to dodge IEEE-754 round-trip surprises in DynamoDB Decimal writes.
- `lambda_scorer/tests/__init__.py` (empty).
- `lambda_scorer/tests/conftest.py` (68 lines) — `fresh_handler` fixture: `mock_aws()` context + table create per ADR 0010 schema + `importlib.reload(handler)` so the module-level boto3 resource binds to the moto-mocked client. Reload is mandatory: the first import in a process captures the boto3 binding, and subsequent tests that just import get the cached binding regardless of which moto context is active.
- `lambda_scorer/tests/test_handler.py` (305 lines, 10 tests) — 3 structural-parity (mirror local_runtime's three) + 2 cold-start (happy path; version-mismatch raises `DriftError` via reload + monkeypatched `_DEFAULT_REF_PATH`) + 5 hot-path (cold pump empty-DDB warm-up; warm pump seeded 5 priors; STATE-row filtering guard; malformed event missing `pump_id`; malformed event missing `bearing_temp`).

### Documentation

- `docs/adr/0010-dynamodb-schema-hot-state.md` (337 lines) — new. §Principle + §Context + §Decision (reading-row + STATE-row shapes; access-pattern table; on-demand capacity recommendation) + §Alternatives (composite-key shape A/B/C; STATE-row placement; ADR-vs-session-log shape) + §Consequences (+2 PutItems/invocation cost; STATE-row eventual consistency; PSI follow-on is additive) + §References. Three new commitments locked: (i) two row shapes share one table; (ii) reading PutItem + STATE PutItem are NOT `TransactWriteItems` — STATE drift is acceptable; (iii) PSI window storage stays DynamoDB-backed.
- `context/_interfaces.md` — §"DynamoDB schema" replaced TBD with the ADR 0010 resolution (reading-row + STATE-row schemas, access-pattern table, `sk begins_with "2"` filter rule pointer). §"Lambda scorer DynamoDB writes" updated with the MVP-scope vs PSI-follow-on extension callout. §"Grafana → DynamoDB adapter" picked up a one-line BatchGetItem pointer.
- `context/lambda_scorer.md` — rewritten. §Current state changed from "not started" → MVP shipped. §Interfaces table updated with the two-PutItem-per-invocation shape. §Open questions trimmed: DynamoDB schema (Q5) closed by ADR 0010; only cold-start latency open. §Related ADRs added 0010 + ADR 0007/0008/0009 cross-references.

### Runtime dep

- `requirements.txt` +1 line: `boto3>=1.34` with the DEV_NORMS §6.2 justification comment (Lambda DynamoDB read/write per ADR 0010; ships in deploy zip; footprint stays under ADR 0006 §Q4's ~124 MB measured baseline).

### Artifacts (sandbox regen — see §Trade-offs surfaced)

- `model/artifacts/model.pkl` — regenerated (204.4 KB; AUC 0.998 on 2/8 hold-out; version `v0.1.0-seed-0`). Sandbox-runtime sklearn 1.7.2 pickling; previous committed bundle was sklearn-1.8.0-pickled and incompatible with the sandbox (sklearn deserialization raises ValueError on numpy array layout). PO regenerates at the canonical 30 pumps natively on Windows.
- `model/artifacts/operational_reference_distribution.json` — regenerated (2.2 KB; 15 pumps × 1800 ticks = 27000 samples; `model_version = v0.1.0-seed-0`; 4-feature PSI surface per ADR 0009). The previously committed file was **FUSE-truncated mid-stream** — ended at `"...2700,\n        "` partway through the `bin_counts` array for the `rpm` feature, no closing brackets. JSON parse failed at byte 2180 of a 2289-byte file. This is the same FUSE write-truncation pattern documented in the memory `ml_obs_pipeline_fuse_write_truncation` — likely landed in a prior session via an `Edit`-tool path or an interrupted `cp`.

## Decisions

- **ADR 0010 — DynamoDB schema.** Option A + STATE-row sibling + DynamoDB-backed PSI window (forward commitment). Status: Accepted (PO sign-off 2026-06-02; Gemini review pending).
- **Reading PutItem + STATE PutItem are NOT issued via TransactWriteItems.** Two separate calls. STATE-row drift is brief (converges within milliseconds), dashboards are eventual-consistency tolerant, and the score path always reads the reading rows. Documented in ADR 0010 §Item ordering.
- **`sk begins_with "2"` filter rule reserved.** ISO-8601 timestamps start with year digits; "STATE" starts with S. Any future reserved-SK rows (e.g., `"META"`) must coexist with this filter; ADR 0010 §Reserved SK literal "STATE" carries the constraint.
- **Cold-start eager-loads the reference.** The handler does `REFERENCE = load_reference()` at module scope — not lazy. Rationale: a partial redeploy (new model.pkl, old operational_reference_distribution.json) must fail at cold-start, not at the first invocation; CloudWatch surfaces the init failure clearly.

## Trade-offs surfaced

- **`importlib.reload` discipline in moto-backed tests.** The handler's module-level `_DDB = boto3.resource("dynamodb")` captures the boto3 client at first import. Tests that need moto must reload the module *inside* the `with mock_aws():` context — otherwise the first test in a process binds against the real boto3 client and every subsequent moto test silently hits that binding. The `fresh_handler` fixture handles this. Documented in `conftest.py`'s docstring so a future maintainer adding a new test doesn't reinvent the reload pattern.
- **Decimal round-trip on DynamoDB writes.** boto3 rejects native `float` ("Float types are not supported"). `_to_decimal(value)` passes through `str(value)` rather than `Decimal(value)` directly because `Decimal(0.1)` is `0.10000000000000000555…` while `Decimal(str(0.1))` is exactly `0.1`. The DynamoDB-stored numbers are then read back as `Decimal` and cast to `float` at the read boundary (`_reading_to_telemetry`) so `extract_features` sees the same shape as local mode.
- **Sandbox sklearn version vs. committed model.pkl.** The previously committed `model/artifacts/model.pkl` was pickled with sklearn 1.8.0 (a more recent version than the sandbox's 1.7.2). `joblib.load` raised `ValueError: EOF: reading array data, expected 2856 bytes got 953` — a serialisation-layout incompatibility, NOT a content corruption. Regenerating the bundle sandbox-side (`python -m model.train --n-pumps 8 --n-test-pumps 2 --seed 0`) produced a 1.7.2-compatible 204.4 KB bundle. PO regenerates at the canonical 30-pump native build on Windows before any production-shape work; the sandbox-committed bundle is proof-of-pipeline only (per `model/artifacts/README.md` §"Two pump counts, two purposes").
- **FUSE-truncated reference JSON discovery.** `model/artifacts/operational_reference_distribution.json` on disk was truncated to 2289 bytes ending mid-`bin_counts`-array with no closing brackets. This was a SILENT FAILURE — `wc -l` showed 109 lines (matching the expected complete file's line count almost exactly), `ls -la` showed a normal-looking 2.2 KB. Detection came only at `json.load` time. The corruption mirrors the pattern documented in `ml_obs_pipeline_fuse_write_truncation`: FUSE writes ended mid-stream silently. Regenerated cleanly by `python -m model.train`; the canonical PO Windows-side regen will overwrite again at the 30-pump canonical build.
- **Sandbox-only pandas in the import trace.** `python -c "import lambda_scorer.handler; import sys; print('pandas' in sys.modules)"` reports `True` in the sandbox — pandas gets pulled in indirectly when sklearn loads the joblib bundle. Pandas is NOT a hard dep of sklearn (it's `Required-by` `camelot-py`, `seaborn`, `tabula-py` per `pip show pandas`, none of which are in `requirements.txt`). In a Lambda deploy zip built from `requirements.txt`, pandas is absent and sklearn skips the optional pandas-aware code paths. The ADR 0006 §Q4 ~124 MB baseline holds.

## Tests state

**360 passed + 1 skipped** in 16.71 s (sandbox). Net delta from the 2026-06-04 baseline (350 + 1 skipped): **+10 tests**, all under `lambda_scorer/tests/test_handler.py`. No regressions elsewhere.

Coverage breakdown:

| Test | What it pins |
|---|---|
| `test_structural_parity_extract_features_loads_from_shared` | `lambda_scorer.handler.extract_features` resolves to `shared/features.py` via `inspect.getfile` |
| `test_structural_parity_score_loads_from_shared` | Same for `score` → `shared/score.py` |
| `test_structural_parity_load_reference_loads_from_shared` | Same for `load_reference` → `shared/drift.py` |
| `test_cold_start_reference_loaded` | Module-level `REFERENCE` is a dict with `features` + `model_version` keys; `feature_names == PSI_FEATURE_NAMES` (ADR 0009) |
| `test_cold_start_version_mismatch_raises_drift_error` | Forged reference with mismatched `model_version` raises `DriftError` on `importlib.reload(handler)` |
| `test_handler_cold_pump_writes_reading_and_state` | Empty DynamoDB → reading row + STATE row both land; reading-row attributes are `Decimal(str(value))` |
| `test_handler_warm_pump_reads_prior_readings` | 5 seeded reading rows → window query returns them + the new reading → STATE row's `latest_ts` reflects the new invocation |
| `test_handler_window_query_excludes_state_row` | Pre-existing STATE row in the partition doesn't leak into the score-path window query (guards the `sk begins_with "2"` predicate) |
| `test_handler_missing_pump_id_raises` | `EventParseError` raised before any DynamoDB or scoring work |
| `test_handler_missing_telemetry_field_raises` | Same for a missing raw-signal field (`bearing_temp`) |

## Deploy-zip footprint verification

- Top-level imports in `handler.py`: `boto3`, `boto3.dynamodb.conditions.Key`, `shared.drift`, `shared.features`, `shared.score` + stdlib. No new heavyweight deps beyond what ADR 0006 §Q4 already measured.
- Heavy modules reachable from `import lambda_scorer.handler`: `boto3, botocore, joblib, numpy, scipy, sklearn` (+ sandbox-only `pandas` per §Trade-offs).
- `requirements.txt` additions: `boto3>=1.34`. Per AWS docs, the Python 3.10 / 3.12 Lambda runtime ships boto3 in the runtime base image — bundling it explicitly adds ~6 MB but pins the version against runtime drift. Footprint margin stays well within the 250 MB unzipped ceiling.

## Open follow-ups

- **PSI compute + SNS publish (next lambda_scorer session).** Widen the score-path `Query` Limit from 150 to 1800 for the PSI window; call `shared.drift.compute_psi(window, REFERENCE)`; extend the STATE-row `PutItem` with `latest_psi` (4-key dict per ADR 0009) and `alert_flag`; publish to SNS when `max(psi.values()) > 0.25` OR `score > 0.7` (per `_interfaces.md §SNS alert payload`). Schema is additive only — no migration.
- **Cold-start latency measurement (post-deploy, IaC session).** ADR 0006 §Q4 pre-authorized S3 cold-load as a fall-back if measured cold-start exceeds the <2 s target. Bundle stays the default until measurement says otherwise.
- **IaC session — DynamoDB module.** `infra/modules/dynamodb/` creates the `pump_hot_state` table per ADR 0010's key schema + `BillingMode=PAY_PER_REQUEST`. IAM policy for `lambda_scorer` execution role: `dynamodb:Query`, `dynamodb:PutItem`, scoped to the single table ARN.
- **PO Windows-side regen of canonical model.pkl + operational reference at 30 pumps.** Same `v0.1.0-seed-0` tag. The sandbox-committed bundle is replaced on next commit (`python -m model.train --n-pumps 30 --seed 0`). `model/artifacts/README.md` §"Two pump counts, two purposes" already documents the distinction.

## Context files updated

- `context/_interfaces.md` — §"DynamoDB schema" filled in from TBD per ADR 0010; §"Lambda scorer DynamoDB writes" MVP-vs-follow-on callout; §"Grafana → DynamoDB adapter" BatchGetItem pointer.
- `context/lambda_scorer.md` — §Current state from "not started" to MVP shipped; §Interfaces updated; §Open questions trimmed; §Related ADRs +0010 +0007/0008/0009 cross-refs.

## Note for next session

The PSI follow-on inherits a clean structural baseline: ADR 0010 schema locked, STATE row already written each invocation (just needs new attributes), reference already loaded at cold-start, structural-parity guards in place. The only API change is widening the score-path `Query` Limit and adding the `compute_psi` + SNS branches. Test additions: PSI-on-warm-window happy path, SNS publish on threshold breach, STATE-row attribute extension. Expect net delta around +6 tests.

## Reviewer feedback highlights

Review packet `review_packets/2026-06-02-lambda_scorer-mvp.md` ran through the multi-provider cascade at 2026-06-02 14:22; Gemini hit `429 RESOURCE_EXHAUSTED` (free-tier daily quota), OpenRouter returned `404` on the then-default `deepseek/deepseek-chat-v3.1:free` (model-ID drift), and **Groq** picked up the slack on `llama-3.3-70b-versatile`. Response file: `review_responses/2026-06-02-lambda_scorer-mvp.md`. Provenance footer is degraded on this one (literal `$usedModel` string instead of the expanded model name — see §Dev-workflow items below); the response IS from `llama-3.3-70b-versatile` per the console output.

**Calibration caveat (ADR 0011 §Consequences):** Llama-3.3-70b's adversarial-review posture is solid but a step behind Gemini Pro or DeepSeek R1 on rigor. Three of the six points were technically off or missed the question asked; weighting the findings reflects that.

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. ADR 0010 §Item ordering — recommend TransactWriteItems / retry-with-backoff | **Rejected** | Groq didn't engage the specific retry-idempotency argument from ADR 0010. Reading-row PutItem is overwrite-idempotent on duplicate ts; STATE-row PutItem is overwrite-idempotent by definition. At-least-once Lambda retry semantics hold. Flagged for Gemini re-run when daily quota resets. |
| 2. `sk begins_with "2"` predicate — claimed filter is post-Limit; flagged millennium-digit fragility | **Partially rejected, partially addressed** | Groq's post-Limit claim is wrong: `begins_with()` inside `KeyConditionExpression` is a sort-key range predicate applied at index-scan level (BEFORE Limit). `FilterExpression` is post-Limit; we don't use that. The fragility critique IS fair: handler.py's hot-path comment block was rewritten to document the year-2xxx assumption explicitly + cite the begins_with vs FilterExpression distinction; new test `test_state_sk_outside_year_range_filter` pins the convention so a future reserved-SK row that starts with "2" fails CI loud. |
| 3. Cold-start eager-load — recommend lazy with first-call error path | **Rejected** | Groq's suggestion is internally contradictory ("reduce cold-start latency while still providing a fail-fast mechanism"). Lazy defers failure to first invocation, which is exactly what ADR 0007's cold-start fail-fast contract is designed to avoid (partial-redeploy desync becomes a CloudWatch invocation error instead of a CloudWatch init-duration error). |
| 4. Decimal round-trip pattern in `_to_decimal` | **Addressed (agreed)** | Groq agreed boundary placement is principled. No change. |
| 5. `fresh_handler` fixture `importlib.reload` discipline | **Deferred** | Marker / autouse to prevent future test authors from missing the reload is reasonable. Not blocking for MVP; future test-quality session. |
| 6. Sandbox-side artifact regen — recommend gitignore + PO native regen pre-commit | **Deferred (PO call)** | PO decision 2026-06-02: keep committing sandbox artifacts during active dev; do a housekeeping pass once development wraps. No `.gitignore` changes this session. |

**Diff from review:**
- `lambda_scorer/handler.py` — hot-path comment block expanded to document the year-2xxx assumption + the begins_with-vs-FilterExpression distinction (+~20 lines).
- `lambda_scorer/tests/test_handler.py` — new `test_state_sk_outside_year_range_filter` (~25 lines) pins the convention.
- Tests: **361 passed + 1 skipped** (was 360 + 1 pre-review; +1 test net delta).

## Dev-workflow items surfaced during the review

- **Provenance-footer escape bug (`scripts/gemini_review.ps1`).** The 2026-06-02 response file rendered the model name as the literal string `$usedModel` instead of `llama-3.3-70b-versatile`. Cause: the script used `` `$usedModel` `` (backtick-dollar) which escapes the variable in PowerShell, emitting the literal name. Fix: changed to `` ``$usedModel`` `` (double-backtick) — PowerShell expands the variable, the doubled backticks render as a markdown code-span around the expanded value. Per ADR 0011 §Decision #3 ("regeneration is the prescribed fix, not a manual patch"), the 2026-06-02 response file's footer is left as-is — first-audit-artifact honest history; future responses will be clean.
- **OpenRouter default model drift.** `deepseek/deepseek-chat-v3.1:free` (the initial default) returned 404 on the first cascade run. Live OpenRouter catalog confirmed `deepseek/deepseek-r1:free` is on free tier per the 2026-04-24 announcement. Default swapped in the same commit. Free-tier model identifiers drift; ADR 0011 §Consequences §Negative anticipates this.

