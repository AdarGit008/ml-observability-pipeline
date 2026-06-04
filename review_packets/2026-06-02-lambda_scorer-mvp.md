# Review Packet 2026-06-02 — lambda_scorer — MVP cold-start + per-pump score path

> Paste this entire file into Gemini via:
> `gemini -p "$(cat review_packets/2026-06-02-lambda_scorer-mvp.md)" > review_responses/2026-06-02-lambda_scorer-mvp.md`

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

Shipped the `lambda_scorer/` MVP. Cold-start (model + operational reference load + `model_version` cross-check per ADR 0007; boto3 DynamoDB resource bound to the table) + per-pump score path (parse IoT-Rule event → `Query` last 150 reading rows from DynamoDB → append latest → `shared.features.extract_features` → `shared.score.score` → `PutItem` reading row + `PutItem` STATE row overwrite). PSI compute + SNS publish explicitly deferred to a follow-on session; the reference is loaded at cold-start so the follow-on plugs PSI in additively. Resolved HANDOFF.md §6 Q5 (DynamoDB schema) as **ADR 0010** — Option A (`PK=pump_id, SK=sk`) + a STATE-row sibling (`sk="STATE"`) + DynamoDB-backed PSI window as a forward commitment. Net test delta +10 (3 structural-parity, 2 cold-start, 5 hot-path); full sandbox suite is 360 passed + 1 skipped.

## Files changed

| File | Lines | Note |
|---|---|---|
| `docs/adr/0010-dynamodb-schema-hot-state.md` | +337 | NEW — schema decision + access patterns + alternatives + consequences |
| `lambda_scorer/__init__.py` | +24 | NEW — module docstring |
| `lambda_scorer/handler.py` | +250 | NEW — cold-start + hot-path |
| `lambda_scorer/tests/__init__.py` | +0 | NEW — empty marker |
| `lambda_scorer/tests/conftest.py` | +68 | NEW — `fresh_handler` fixture (moto + reload) |
| `lambda_scorer/tests/test_handler.py` | +305 | NEW — 10 tests covering parity + cold-start + hot-path |
| `context/_interfaces.md` | +25 net | §DynamoDB schema TBD → ADR 0010 resolution |
| `context/lambda_scorer.md` | rewritten (~38 lines) | Current state, interfaces, related ADRs +0010 |
| `requirements.txt` | +1 | `boto3>=1.34` with DEV_NORMS §6.2 justification |
| `model/artifacts/model.pkl` | regen | Sandbox-runtime rebuild (sklearn 1.7.2 compat; was 1.8.0-pickled and unreadable in sandbox) |
| `model/artifacts/operational_reference_distribution.json` | regen | Previously FUSE-truncated on disk; rebuilt cleanly via `python -m model.train` |

(Full session log at `docs/sessions/2026-06-02-lambda_scorer-mvp.md`.)

## Specific questions for Gemini

1. **ADR 0010 §Item ordering — reading PutItem + STATE PutItem are NOT in TransactWriteItems.** Two separate calls; STATE may briefly reflect an earlier invocation. The rationale: (a) the score path always reads reading rows (never STATE), so STATE drift can't corrupt the score; (b) dashboards are eventual-consistency tolerant; (c) `TransactWriteItems` is a drop-in upgrade if a future feature needs atomic ordering. Does this hold under any failure mode I'm not seeing? Specifically: what happens if the reading PutItem succeeds but the STATE PutItem fails (or vice versa)? The handler doesn't catch boto3 exceptions — Lambda treats the invocation as failed and IoT Rule may retry per the rule's error-handling configuration. Is the retry semantics ("idempotent because the reading-row SK is the timestamp; STATE-row overwrite is idempotent by definition") actually correct, or is there a subtle race I'm missing?

2. **`sk begins_with "2"` predicate as the STATE-row filter.** ISO-8601 timestamps start with year digits ("2026-…"); `"STATE"` starts with S. The score-path `Query` filters out STATE via `Key("sk").begins_with("2")`. This relies on the assumption that no reading-row timestamp will ever start with a non-"2" character — which holds for any year 2000–2999. Two failure modes to surface: (a) is there a more defensive convention that wouldn't tie us to a millennium digit (e.g., a `row_type` attribute + GSI, or a numeric SK that puts STATE at the end)? (b) is the `Limit=150` + `ScanIndexForward=False` semantics sound — specifically, does DynamoDB apply the begins_with filter before or after the Limit?

3. **Cold-start eager-load of the reference.** `lambda_scorer/handler.py` does `REFERENCE = load_reference()` at module scope. This forces every cold-start to do a disk read + JSON parse + joblib model_version peek (`joblib.load(model_path)` for the version-check branch). The model classifier itself is lazy-loaded inside `shared.score.score` on first invocation. The intent: fail fast on partial redeploys. The cost: ~50–150 ms of cold-start latency for the eager load. Is the trade-off well-placed, or should the reference load also be lazy with a one-time error path on first score? Note that the operational reference is small (~2 KB JSON), so the per-cold-start cost is dominated by the model_version-check joblib load.

4. **Decimal round-trip pattern in `_to_decimal`.** `Decimal(str(value))` rather than `Decimal(value)` to dodge the `Decimal(0.1) → 0.1000…0555` IEEE-754 surprise. Reading rows come back as Decimal and are cast to float at the read boundary (`_reading_to_telemetry`). Is this the right boundary, or should `extract_features` (which is in `shared/`) be Decimal-aware? Mode parity argues no — local mode never sees Decimal — but I want a second look at whether the boundary placement is principled.

5. **The `fresh_handler` fixture's `importlib.reload` pattern.** The handler's module-level boto3 binding captures whatever client was active at first import. Tests that need moto must reload the module *inside* the `with mock_aws():` context. The fixture handles this, but it's a footgun for a future test author who adds a new test and skips the fixture. Is there a defensible way to structure this so tests fail loud if they miss the reload (e.g., a marker that adds the fixture autouse), or is the fixture-name-as-discipline approach fine for a single-test-file MVP?

6. **Sandbox-side artifact regen — proof-of-pipeline vs. production canonical.** This session regenerated `model.pkl` + `operational_reference_distribution.json` in the sandbox (8 pumps, 2 hold-out) because the committed bundle was sklearn-1.8.0-pickled (incompatible with sandbox sklearn 1.7.2) and the reference JSON was FUSE-truncated mid-stream. The PO Windows-side 30-pump regen will overwrite both files at the next commit. `model/artifacts/README.md` §"Two pump counts, two purposes" documents the distinction. Is the sandbox-as-proof-of-pipeline pattern still pulling its weight, or has the sklearn-version-skew issue made it actively harmful to commit sandbox artifacts? Specifically: should `.gitignore` exclude the sandbox-built artifacts and require PO to regen before commit, or is the current "commit them, PO overwrites natively" pattern still the right shape?

## What I'm NOT looking for in this review

- PSI compute + SNS publish design — explicitly deferred to a follow-on session per the brief. Don't second-guess the scope split.
- Terraform / IaC for the table or the IoT Rule — IaC session is downstream of this one.
- Test coverage of the dashboards adapter — Grafana session pickup, not in this scope.
- Lambda concurrency / reserved-capacity tuning — sized in `context/lambda_scorer.md`; not optimized here.

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. <summarize> | Addressed / Deferred / Rejected | <where, why> |
| 2. ... | ... | ... |
