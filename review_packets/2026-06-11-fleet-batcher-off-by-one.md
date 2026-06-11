# Review Packet 2026-06-11 — fleet/batcher pump-id off-by-one — fleet-batcher-off-by-one

> Run via: `.\scripts\run_review.ps1 -Slug fleet-batcher-off-by-one` (DeepSeek, ADR 0011 §Addendum 2026-06-10)

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.
6. (Operational corollary) Any local/AWS divergence in scoring/drift is a bug or an ADR.

Full constraint set: `context/_global.md`.

## How this was found (live verification, 2026-06-11)
During the fleet-PSI live apply, the adapter envelope showed `fleet.pumps_pooled: 14` on a 15-pump fleet. Tracing it: `lambda_fleet_psi` enumerates the fleet as `range(1, FLEET_SIZE + 1)` → `P-01..P-15`, which drops **P-00** and queries a nonexistent **P-15**. Inspecting the siblings, `lambda_s3_batcher` had the **identical** bug (so P-00's readings were never archived to S3 — silent cold-path data loss). `dashboards_adapter` was already correct (`range(FLEET_SIZE)`, fixed live 2026-06-07 but the same root pattern lived on in two other copies).

Note: a second live observation — every pump scoring `~0.9999` on `scenario: healthy` — was investigated and found **NOT a bug**: `demo_mode: true` compresses the HEALTHY dwell to ~60 ticks (`simulator/config.py`), so the fleet runs the full HEALTHY→FAILED arc in ~15 min and the pumps had genuinely degraded by read time. No code change for that; it is recorded in the session log. This packet is ONLY the off-by-one fix.

## Summary of the change
Correct the fleet pump-id enumeration in the two Lambdas that still had it 1-indexed, matching the already-correct adapter, and add guards so it cannot silently drift again.

- `lambda_fleet_psi/handler.py:122` and `lambda_s3_batcher/handler.py:101`: `range(1, FLEET_SIZE + 1)` → `range(FLEET_SIZE)` (now `P-00..P-(FLEET_SIZE-1)`).
- `lambda_fleet_psi/tests/test_handler.py`: the cold-start test previously **asserted the bug** (`== tuple(... for i in range(1, 16))`); changed to `range(15)` and strengthened with `FLEET_PUMP_IDS[0] == "P-00"` + `"P-15" not in FLEET_PUMP_IDS`.
- `lambda_s3_batcher/tests/test_batcher.py`: new `test_p00_is_archived_off_by_one_regression` — seeds ONLY P-00, asserts it lands in a Parquet file (the existing 18 tests all seed P-01/P-02, in-range either way, so none caught it).
- `lambda_fleet_psi/tests/test_fleet_id_consistency.py`: new SOURCE-level cross-component guard — asserts all three handlers (`adapter`, `batcher`, `fleet_psi`) use `range(FLEET_SIZE)` and none use `range(1, FLEET_SIZE + 1)`.
- Docstrings + `context/{_interfaces,lambda_fleet_psi,lambda_s3_batcher,dashboards}.md` wording.

`shared/` is untouched — this is outside the ADR 0005 parity boundary. Full suite: 453 passed, 1 skipped (in-sandbox, sklearn 1.7.2; production runs the matching 1.9.0).

## Core hunk
```python
# lambda_fleet_psi/handler.py  AND  lambda_s3_batcher/handler.py
FLEET_PUMP_IDS: tuple[str, ...] = tuple(
    f"P-{i:02d}" for i in range(FLEET_SIZE)   # was: range(1, FLEET_SIZE + 1)
)
```

```python
# lambda_fleet_psi/tests/test_fleet_id_consistency.py (new)
_HANDLERS = ("dashboards_adapter/handler.py",
             "lambda_s3_batcher/handler.py",
             "lambda_fleet_psi/handler.py")

@pytest.mark.parametrize("rel", _HANDLERS)
def test_fleet_pump_ids_are_zero_indexed(rel):
    src = (_REPO / rel).read_text(encoding="utf-8")
    assert "for i in range(FLEET_SIZE)" in src
    assert "range(1, FLEET_SIZE + 1)" not in src
```

## Specific questions for the reviewer

1. **Batcher first-run-after-fix semantics.** P-00 has never had a WATERMARK row, so post-fix the batcher treats its lower bound as epoch (ADR 0015) and drains all of P-00's history on the first batch. We always teardown→reapply (no accumulated history), so it's benign in practice. Is there any scenario (e.g. an apply that is NOT preceded by a fresh table) where draining P-00 from epoch is a problem? Should the fix note this explicitly?

2. **Source-level vs behavioral consistency guard.** The new `test_fleet_id_consistency.py` greps handler source text rather than importing the modules (which would require each handler's cold-start env vars). Is a text-match guard acceptable here, or does it invite false confidence (e.g. someone writes `range(0, FLEET_SIZE)` — semantically correct but fails the literal `range(FLEET_SIZE)` check)? Suggest a more robust assertion if so.

3. **SSOT debt.** The enumeration is duplicated across three independently-packaged Lambdas; we are deferring true dedup (a shared NON-parity fleet-id module would couple all three build scripts) and relying on the consistency test instead. Is "test, don't dedup" the right call for a portfolio repo, or is the duplication itself a red flag a reviewer would want removed now? `shared/` is the locked parity boundary and the wrong home (an AWS-fleet concept; the local parity peer has no cloud fleet).

4. **Scope.** The batcher fix was folded into a fleet-PSI session because it shared the exact root cause and is a real data-loss bug. Reasonable, or should cross-component fixes always be their own commit?

5. **Anything missed.** Are there other places the fleet is enumerated or sized (Terraform `fleet_size`, simulator `pump_count`, teardown sweep `P-00..P-(FLEET_SIZE-1)`) where a 0-vs-1 index or an off-by-one could still bite? The simulator emits `P-00..P-14`; confirm nothing else assumes 1-indexed.
