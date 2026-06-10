## Review: fleet-psi-infra Packet (2026-06-10)

### General Impression

Solid module that reuses the `lambda_s3_batcher` pattern well. The build script catches the right failure modes. The IAM scoping is tight. A few design decisions deserve closer examinationâparticularly the divergence from the S3 deploy path and a gap in the teardown script. None are blockers, but they represent technical debt or operational risk that the author should track.

---

### 1. IAM Query + GetItem + PutItem vs ADR "Query"

**Finding**  
The ADR's "Query" is an oversimplification. The handler code explicitly calls `get_item` and `put_item`, so granting all three is correct for *current* code. This is strict leastâprivilege with respect to the handler's actual API calls. The `Scan`, `UpdateItem`, `DeleteItem`, and `Batch*` actions are absent, which is exactly right.

**Risk / weakness**  
- The ADR still reads "IAM = Query on the table ARN + sns:Publish" â this is misleading and creates a maintenance trap. If a future developer relies on the ADR to scope a new Lambda, they might copy the wrong set.  
- `dynamodb:Query` without a condition on partition key (`LeadingKeys`) means the role can query *any* partition in the table. For a singleâtable design with mixed data (pump states vs. fleet state), this is broader than ideal. The batcher had the same caveat, so itâs consistent â but the risk should be documented in the moduleâs README.

**Recommendation**  
Amend ADR 0018 Â§Followâups to list `Query, GetItem, PutItem` explicitly. Add a comment in `main.tf` near the IAM statement that points to the ADR addendum. Consider raising a codeâreview ticket for adding `Condition` when AWS supports `LeadingKeys` for `Resource` â unlikely soon, but worth noting.

---

### 2. Direct `filename` Upload vs S3 Deploy Path

**Finding**  
The fleet package is âdriftâonlyâ (numpy, no sklearn) and measures ~54 MB unzipped. The current zip size is likely under the 50 MB directâupload limit, but the margin is thin. The decision breaks the established pattern used by the scorer and batcher.

**Risks / weaknesses**  
- **Size creep** â A minor numpy bump could push the zipped artifact above 50 MB. The footprint check only monitors *unzipped* size (200 MB threshold). There is **no warning for a 48â52 MB zipped crossing**. The first signal would be a failed `terraform apply`, which is an expensive feedback loop for a $0âcost project (apply would fail, stack stays up partially).  
- **Divergence** â The project north star is âone polished repo, not five halfâfinished ones.â Three of the four Lambdas now use two different deployment mechanisms. This increases cognitive load and the chance that a future change breaks the fleet path (e.g., adding sklearn would require switching to S3 anyway).  
- **Build complexity** â The S3 path already has the archive bucket, the `aws_s3_object` resource, and a clean convention. Abandoning it adds an extra code path for no measurable benefit.

**Recommendation**  
Switch to the S3 deploy path before the next apply. Itâs a oneâline change in the build script (upload to `s3://archive-bucket/deploy/`) and a trivial `filename = data.archive_file.output_path` to `s3_key = data.archive_file.output_path` in Terraform. Add a zippedâsize assertion to the build script (e.g., `if ($zip.Size -gt 50MB) { throw }`). This eliminates the risk, keeps the repo consistent, and adds a safety net.

---

### 3. `reserved_concurrent_executions = -1`

**Finding**  
The batcher deferred setting `reserved_concurrent_executions = 1` because of the accountâlevel quota (minimum 10 unreserved). The fleet Lambda has the same constraint. The author argues overlap is implausible (5âminute cadence, 30âs timeout) and the state is idempotentâoverwrite.

**Assessment**  
The risk is real but **acceptable** under the current design. The readâmodifyâwrite of the fleet state is deterministic given the same input snapshot. Two concurrent invocations that both read the same previous state will compute the same next state and write it twice â no corruption. If the second invocation reads after the first writes, it sees the updated state and computes correctly. No race condition that can produce incorrect output.

**Still worth documenting**  
- If the EventBridge rule ever fires more than once in a window (e.g., retry after Lambda timeout), there is no harm, but the noâconcurrency guarantee would simplify reasoning about edgeâtrigger behavior.  
- If the fleet state computation ever becomes nonâdeterministic (e.g., uses a timestamp from invocation start), concurrency could cause divergence. A comment in `main.tf` explaining why `-1` is safe (idempotent overwrite) would help future maintainers.

**Recommendation**  
Keep `-1` for now. Add an inline comment referencing the idempotency argument. If the account quota ever increases, reconsider `reserved_concurrent_executions = 1` for defense in depth.

---

### 4. `depends_on` / LogâGroup Race

**Finding**  
The function explicitly depends on the log group. EventBridge rule and permission implicitly depend on the functionâs ARN. This creates a proper creation order:

1. Log group  
2. Lambda function  
3. EventBridge rule + target + invocation permission  

When the rule fires for the first time, the function and log group are guaranteed to exist. The log stream will be created by the Lambda runtime, which has the right permission.

**No race of concern.**  
The only transient state is between the rule being created and the target being created (both happen after the function). If the rule fires during that window, the event is silently dropped â no cost, no misbehaviour.

**Recommendation**  
None. The current `depends_on` is correct.

---

### 5. Environment Variables KnownâAfterâApply in `plan`

**Finding**  
The environment block appears entirely opaque because `SNS_TOPIC_ARN = module.sns.topic_arn` is a resource attribute that is only resolved after apply. Terraformâs plan output will show `"variables": { ... }` with all values redacted â it does not selectively hide individual keys. This is standard behaviour. The `DDB_TABLE_NAME` and `FLEET_SIZE` are static values, but because one value is unknown, the entire block is shown as âknown after apply.â

**Benign.**  
This does not mask missing variables â if a variable were undefined, `terraform plan` would produce an error before reaching the plan output. The risk is cosmetic: the plan reader cannot verify the static values are set correctly until apply. This is identical to the scorer and batcher.

**Recommendation**  
Accept asâis. If you later want planâvisible static values, pass them as separate Terraform variables and build the environment map from a `merge` of a known map and a dependsâon map â but that adds complexity with no functional benefit.

---

### 6. Teardown Completeness

**Finding**  
The script asserts absence of:
- Function (`pumpâfleetâpsi`)  
- Log group (`/aws/lambda/pumpâfleetâpsi`)  
- Role (`pumpâfleetâpsiâexec`)  
- Rule (`pumpâfleetâpsiâschedule`)

**Missed resources that could leak cost**
- **EventBridge target** â Deleting the rule *does* cascade to remove the target. However, the script only checks rule *absence*, it does not explicitly delete the rule. If the rule still exists (e.g., `events:disable` was called instead of delete), the target might persist. The script should attempt to **remove targets** and **delete the rule** explicitly, not just assert absence.  
- **Invoke permission** â The `aws_lambda_permission` resource creates a resourceâbased policy statement on the Lambda function. When the function is deleted (step 1 in the script), all permissions are removed automatically. No additional cleanup needed.  
- **DynamoDB row** â The fleet state row is inside the table, which is destroyed by `terraform destroy` in a separate step. The script doesn't need to delete it individually.

**Recommendation**  
Add these lines to `aws_teardown.sh` after deleting the function (before deleting the role):
```bash
# Remove EventBridge target and rule
aws events remove-targets --rule "pump-fleet-psi-schedule" --ids "1" 2>/dev/null || true
aws events delete-rule --name "pump-fleet-psi-schedule" 2>/dev/null || true
```
Then change the assertion from ârule absentâ to ârule absent **or** no targets remainingâ. Without this, if the rule deletion fails for any reason, the target continues to invoke a nonâexistent function, generating error logs in CloudWatch (minor cost) and polluting the event bus.

---

### Additional Observations

1. **Build script `fleet_psi_requirements.txt`** â Doubleâcheck that `numpy` is pinned to the same version used in the scorer to ensure mode parity across invocations. The scorerâs requirements are in `packages/scorer/requirements.txt`. If they diverge, the fleet function could embed a different numpy binary at runtime, potentially causing subtle scoring differences. The footprint check should compare the numpy version from the two requirements files.

2. **Timeout and memory** â The modulesâ `variables.tf` and `main.tf` donât show a `timeout` or `memory_size` setting. The build scriptâs Docker coldâstart test likely validates these, but the Terraform should have explicit defaults. If omitted, Lambda defaults to 3s timeout and 128 MB â likely too small for numpy loading. Ensure `timeout` is set to 30s (as mentioned) and `memory_size` to at least 256 MB (numpy + drift compute). If not already in the module, add them.

3. **Dependency on the scorerâs SNS topic ARN** â The module receives `var.topic_arn` as an input. If the scorerâs SNS topic is ever renamed or its ARN changes, this module must be updated. Thatâs fine, but there is no `depends_on` relationship to `module.sns` â Terraform will infer it from the `topic_arn` reference. Confirm that `module.sns` is created before `module.fleet_psi` (it will be, because `module.sns` is listed first in `infra/main.tf` root, but explicit ordering is not guaranteed without `depends_on`). If the values are passed via variable, not direct module output, the dependency might be lost. Doubleâcheck the wiring.

4. **Log group retention** â The mainâs `infra/main.tf` fragment is not shown, but the module should set `retention_in_days` to something low (e.g., 7) to keep cost $0. If omitted, CloudWatch logs are retained indefinitely, incurring storage cost (though negligible at the $0 limit, but best practice).

---

### Summary Table

| Point | Verdict | Action Required |
|-------|---------|-----------------|
| 1. IAM scope vs ADR | OK, but ADR needs update | Update ADR and add comment in `main.tf` |
| 2. Direct upload vs S3 | Risk of >50 MB zip, divergence | **Switch to S3 deploy path + add zipped size check** |
| 3. Concurrency = -1 | Acceptable given idempotency | Add comment documenting reason |
| 4. depends_on race | No issue | None |
| 5. env known-after-apply | Benign | None |
| 6. Teardown completeness | Misses explicit rule/target deletion | Add `remove-targets` + `delete-rule` to teardown script |
| Additional | Timeout/memory, numpy version pin, retention | Verify Terraform defaults; add pinning check in build script |

Overall, the module is wellâconstructed and the reasoning is sound. The largest action item is **item 2** â aligning with the S3 deploy path â to avoid a future runtime surprise and keep the codebase consistent. The teardown script gap should also be fixed before the stack is ever applied (even though the current session is buildâonly, the script is part of the codebase).

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-10 15:06:29._

