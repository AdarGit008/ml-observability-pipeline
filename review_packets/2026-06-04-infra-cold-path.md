# Review Packet 2026-06-04 — infra / lambda_s3_batcher — infra-cold-path

> Run from the repo root:
> `.\scripts\gemini_review.ps1 -Slug infra-cold-path`

## Role for the reviewer model
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

(Per ADR 0011, this packet may be reviewed by any model in the cascade: Gemini, DeepSeek R1 via OpenRouter, Llama 3.3 70B via Groq, or Llama 3.3 70B via Cerebras. The role is identical across providers; the response file's footer records which one actually wrote the response.)

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

This session completes the AWS data plane's cold path (ADR 0015): an
EventBridge rule (60 s) wakes `lambda_s3_batcher`, which drains
reading rows written since its last run — found via a per-pump
**WATERMARK** reserved-SK row (sibling of ADR 0010's STATE) + 15
Queries with an exclusive-lower-bound BETWEEN — into one Parquet file
per batch (pyarrow, no pandas) under
`s3://<bucket>/year=/month=/day=/hour=/`. A Terraform-declared Glue
table with **partition projection** (no Crawler, no CreatePartition)
makes it Athena-queryable. The batcher computes nothing and imports
no `shared/` code (outside the ADR 0005 parity set; inverse-import
test pins it). Also: the IoT Rule gained the twice-deferred
`error_action` (republish to `factory/errors`, single-topic-scoped
role), and `aws_teardown.sh` now sweeps every cold-path resource.
Suite: 404 passed + 1 skipped (baseline 386+1 + 18 new batcher tests).

## Changed files

**New:**
- `docs/adr/0015-cold-path-batcher-watermark-pyarrow-cadence.md` — the three locked knobs (read pattern, engine, cadence) + partition projection + force_destroy.
- `lambda_s3_batcher/{__init__,handler}.py` — handler (cutoff = now − SAFETY_LAG; BatchGetItem watermarks; per-pump Query; pyarrow table → S3 put; watermarks advance after put, never regress; empty batch = true no-op).
- `lambda_s3_batcher/tests/{conftest,test_batcher}.py` — 18 moto tests incl. Parquet round-trip read-back, boundary-row once-only, put-failure leaves watermarks, single BatchGetItem, inverse parity import test, cold-start validation.
- `infra/modules/s3_archive/*` — bucket `<project>-pump-archive-<account-id>`, `force_destroy = true` (PO call), public-access block, SSE-S3.
- `infra/modules/glue_catalog/*` — database + table, schema in Terraform, projection params for year/month/day/hour.
- `infra/modules/lambda_s3_batcher/*` — Lambda 256 MB/30 s, `reserved_concurrent_executions = 1`, EventBridge rule + target + permission, scoped IAM (Query/BatchGetItem/PutItem on table; PutObject on `<bucket>/year=*`; logs on own group).
- `scripts/build_batcher.{ps1,sh}` + `scripts/batcher_requirements.txt` — pyarrow-only staging + ADR 0006 §Q4 footprint check.

**Modified:**
- `infra/{main,variables,outputs}.tf` — three new modules wired; batcher/Glue variables + outputs.
- `infra/modules/iot_rule/{main,variables}.tf` — `error_action` republish + `<rule>_error_republish` role (iot:Publish on the one topic).
- `scripts/aws_teardown.sh` — bucket / Glue db+table / batcher Lambda + log group + role + EventBridge rule / error-republish role absence checks.
- `requirements.txt` (+pyarrow), `context/{lambda_s3_batcher,infra,_interfaces}.md` (WATERMARK row joins §DynamoDB schema; §S3 archive layout mechanics).
- `scripts/build_lambda.{ps1,sh}` — post-cascade fix from the PO-side verification step: numpy exempted from the tests-strip (numpy 2.4.x's `numpy.testing` needs `numpy._core.tests._natype` on the cold-start import path via scipy's `from numpy import *`; the Docker smoke-check's first real run caught it). Same run surfaced sklearn version skew (pickle 1.9.0 vs dist 1.7.2 under the manylinux2014 cap): `lambda_requirements.txt` now pins the training versions and the scripts accept manylinux_2_28 wheels; build_batcher needed the same platform fix (pyarrow 21+ is manylinux_2_28-only).
- `infra/modules/lambda_scorer/{main,variables}.tf` + batcher module + root — measured zips (scorer 62.1 MB > the 50 MB direct-upload limit) forced the ADR 0006 §Q4 fallback: both Lambdas now upload code via `aws_s3_object` to the archive bucket's `deploy/` prefix and reference `s3_bucket`/`s3_key`. Session log §State has the full account of all four verification-pass catches.

## Key code (inline for review)

Watermark window query (`lambda_s3_batcher/handler.py`):

```python
def _query_new_rows(pump_id: str, last_cutoff: str, cutoff: str) -> list[dict]:
    if not last_cutoff < cutoff:
        return []  # never query (or regress) past this pump's watermark
    condition = Key("pump_id").eq(pump_id) & Key("sk").between(
        last_cutoff + "0", cutoff   # suffix => exclusive lower bound
    )
    rows, kwargs = [], {"KeyConditionExpression": condition}
    while True:
        resp = _TABLE.query(**kwargs)
        rows.extend(resp.get("Items", []))
        if not (last_key := resp.get("LastEvaluatedKey")):
            return rows
        kwargs["ExclusiveStartKey"] = last_key
```

Batch core (put before watermark advance — at-least-once):

```python
    if not rows:
        return {"archived_rows": 0, "pumps_with_rows": 0,
                "cutoff": cutoff, "s3_key": None}      # true no-op
    key = _s3_key(cutoff)
    _S3.put_object(Bucket=S3_BUCKET, Key=key,
                   Body=_write_parquet(_to_arrow_table(rows)))
    _advance_watermarks(watermarks, cutoff, _iso(datetime.now(timezone.utc)))
```

Glue projection parameters (`infra/modules/glue_catalog/main.tf`):

```hcl
  parameters = {
    "projection.enabled"        = "true"
    "projection.year.type"      = "integer"
    "projection.year.range"     = "2025,2035"
    "projection.year.digits"    = "4"
    # month/day/hour analogous, digits=2
    "storage.location.template" = "s3://${var.bucket_name}/year=$${year}/month=$${month}/day=$${day}/hour=$${hour}/"
  }
```

Batcher IAM (the no-compute tripwire):

```hcl
Action = ["dynamodb:Query", "dynamodb:BatchGetItem", "dynamodb:PutItem"]
Resource = var.table_arn
# ...
Action = ["s3:PutObject"]; Resource = "${var.bucket_arn}/year=*"
```

## Specific questions for the reviewer

1. **Watermark correctness.** The exclusive lower bound is `last_cutoff + "0"` (lexicographic suffix trick on ISO-8601-ms strings). Is there any SK the scorer can legally write that this mishandles — e.g. boundary collisions, or a reading keyed exactly at a cutoff? (`_interfaces.md` pins SK format to ISO-8601 UTC ms + Z.)
2. **Safety lag.** `cutoff = now − 5 s` assumes the scorer's IoT→Lambda→PutItem pipeline lands a reading within 5 s of its telemetry ts. Rows later than that are skipped permanently (ADR 0015 accepts this for demo scale). Is the failure mode acceptable, and is 5 s the right default vs. e.g. 10 s?
3. **Advance-all-watermarks.** After a successful put, pumps with NO rows also advance to `cutoff`. Convince yourself this can't lose a late row that a per-pump "only advance contributors" policy would have caught — or flag the case it can.
4. **Partition projection ranges.** `year` is projected over 2025–2035. Any operational gotcha (Athena query behavior outside range, projection + `ts string` column interplay)?
5. **PutItem grant breadth.** The batcher's `dynamodb:PutItem` is scoped to the table ARN, which technically lets it overwrite reading/STATE rows, not just WATERMARK rows. IAM can't scope to an SK. Is the tripwire comment + tests enough, or is there a cheap structural mitigation we missed?
6. **reserved_concurrent_executions = 1** on the batcher: right call for the watermark race, or does a stuck 30 s invocation + 60 s cadence create a throttling failure mode worth handling?
7. **Cost math.** ADR 0015 claims ~450–900 RRU/demo for the read pattern and ~$0.0002/demo S3 PUT residue. Spot-check the arithmetic against ADR 0013's method.

## What I'm NOT looking for in this review
- Style/formatting; test coverage breadth (18 tests reviewed inline).
- The cadence choice (60 s) and force_destroy — recorded PO calls.
- Apply-time behavior — `terraform apply` is demo-day only; validate+plan run PO-side before commit.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Boundary collision: row keyed exactly at the cutoff missed? | Rejected (misreading) | A row at the cutoff is ARCHIVED in that batch (BETWEEN's hi bound is inclusive); the suffix only prevents RE-archiving it next batch. Pinned by `test_boundary_row_at_cutoff_archived_once`; `_query_new_rows`' docstring documents the trick. No change. |
| 2. Safety-lag skip risk: document + production posture | Already addressed | ADR 0015 §Consequences documents permanent-skip + names the production paths (widen lag / Streams, §Alternatives 1B); `SAFETY_LAG_SECONDS` is a tfvars knob. No change. |
| 3. Advance-all-watermarks: add explicit test | Already addressed | `test_watermarks_advance_for_all_pumps_including_rowless` is exactly that test (asserts all 15 watermarks == cutoff with one contributing pump). |
| 4. Projection out-of-range behavior undocumented | Addressed | Comment added to `infra/modules/glue_catalog/main.tf`: out-of-range predicates return empty (not errors); widen `projection.year.range` if the project outlives 2035. |
| 5. PutItem breadth: separate watermark table or SK check | Rejected (documented) | IAM has `dynamodb:LeadingKeys` (PK) but no sort-key condition — "WATERMARK only" is inexpressible in policy. A separate table re-runs ADR 0010 §Alternatives 2B and loses for the same reasons (doubles IaC surface, second policy, no operational gain). KNOWN BREADTH comment added at the grant in `infra/modules/lambda_s3_batcher/main.tf`; the single PutItem call site + tests hold the discipline. |
| 6. concurrency=1 throttling on a stuck invocation | Rejected (self-healing) | EventBridge invokes async; a throttled tick is retried with backoff by Lambda's async path, and a MISSED tick is harmless by construction — the next successful run drains the wider window from the same watermarks. A retry mechanism would add the very overlap risk concurrency=1 removes. |
| 7. Cost math | Verified | Reviewer confirms; ADR 0013 method cross-checked in-session. Post-first-apply actuals remain a demo-day follow-up. |

Provenance: response by **groq** (`llama-3.3-70b-versatile`), 2026-06-04 — see `review_responses/2026-06-04-infra-cold-path.md` footer (ADR 0011 weighting applies).
