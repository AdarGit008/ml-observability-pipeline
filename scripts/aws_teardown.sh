#!/usr/bin/env bash
# aws_teardown.sh — destroy the AWS demo stack and PROVE it's gone.
#
# "$0 lifetime cost" (north star #1) only holds if teardown is
# mandatory and verified. This script is the off switch: it runs
# `terraform destroy`, then sweeps the account with the AWS CLI and
# fails loudly if any resource either infra session creates is still
# present. Run after EVERY demo, no exceptions (context/_global.md
# §Cost guardrails).
#
# Coverage (infra session #1, 2026-06-04 + dashboards adapter session,
# 2026-06-04):
#   - DynamoDB table        pump_hot_state          (ADR 0010/0013)
#   - SNS topic             ml-obs-pipeline-pump-alerts + subscription
#   - Lambda                pump-scorer             (+ log group)
#   - Lambda                pump-dashboard-adapter  (+ log group,
#                           + Function URL)         (adapter session)
#   - IoT topic rule        pump_telemetry_to_scorer
#   - IAM roles/policies    pump-scorer-exec, pump-dashboard-adapter-exec,
#                           pump-s3-batcher-exec,
#                           pump_telemetry_to_scorer_error_republish
#
# Cold path (ADR 0015, cold-path session 2026-06-04):
#   - S3 bucket             <project>-pump-archive-<account-id>
#                           (force_destroy empties it; sweep proves absence)
#   - Glue database/table   pump_archive / pump_readings
#   - Lambda                pump-s3-batcher (+ log group)
#   - EventBridge rule      pump-s3-batcher-schedule
#
# Fleet-PSI (ADR 0018, fleet-psi infra session 2026-06-10):
#   - Lambda                pump-fleet-psi          (+ log group)
#   - IAM role              pump-fleet-psi-exec
#   - EventBridge rule      pump-fleet-psi-schedule
#   - FLEET STATE row       pump_id="FLEET" (swept with the table; no separate delete)
#
# IoT fleet (ADR 0016, iot-fleet session 2026-06-07):
#   - IoT Things           P-00..P-(FLEET_SIZE-1)
#   - IoT policy           pump-fleet-policy (shared, thing-variable scoped)
#   - IoT certificates     FAIL if any ACTIVE cert remains (count-based —
#                          certs have no stable names; terraform destroy
#                          detaches, deactivates, and deletes them;
#                          WARN->FAIL per 2026-06-07 cascade pt 4)
#   - Local key material   WARN if simulator/.secrets still holds
#                          *.private.key (terraform destroy removes the
#                          local_sensitive_file resources)
#
# Also re-checks the $1/$5 budget-alert posture (ACCOUNT_SETUP.md) —
# teardown is the natural moment to notice a deleted budget.
#
# NOTE: an UNCONFIRMED SNS email subscription cannot be deleted by
# Terraform or the CLI; AWS expires it after ~3 days. The sweep
# reports it as WARN, not FAIL.
#
# Usage:  ./scripts/aws_teardown.sh [--destroy-only | --verify-only]
# Env overrides (defaults match infra/variables.tf):
#   DDB_TABLE_NAME, SCORER_FN, ADAPTER_FN, BATCHER_FN, FLEET_FN, SNS_TOPIC_NAME,
#   IOT_RULE_NAME, GLUE_DB_NAME, GLUE_TABLE_NAME, PROJECT_TAG, BUCKET_NAME,
#   FLEET_SIZE, IOT_POLICY_NAME
set -uo pipefail

REGION="eu-central-1" # hard constraint #5 — do not parameterise
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

DDB_TABLE_NAME="${DDB_TABLE_NAME:-pump_hot_state}"
SCORER_FN="${SCORER_FN:-pump-scorer}"
ADAPTER_FN="${ADAPTER_FN:-pump-dashboard-adapter}"
SNS_TOPIC_NAME="${SNS_TOPIC_NAME:-ml-obs-pipeline-pump-alerts}"
IOT_RULE_NAME="${IOT_RULE_NAME:-pump_telemetry_to_scorer}"
BATCHER_FN="${BATCHER_FN:-pump-s3-batcher}"
FLEET_FN="${FLEET_FN:-pump-fleet-psi}"
GLUE_DB_NAME="${GLUE_DB_NAME:-pump_archive}"
GLUE_TABLE_NAME="${GLUE_TABLE_NAME:-pump_readings}"
PROJECT_TAG="${PROJECT_TAG:-ml-obs-pipeline}"
FLEET_SIZE="${FLEET_SIZE:-15}"
IOT_POLICY_NAME="${IOT_POLICY_NAME:-pump-fleet-policy}"
# BUCKET_NAME is account-suffixed — resolved after the sts call below.

MODE="${1:-full}" # full | --destroy-only | --verify-only
FAILURES=0
WARNINGS=0

say()  { printf '%s\n' "$*"; }
ok()   { printf '  OK    %s\n' "$*"; }
warn() { printf '  WARN  %s\n' "$*"; WARNINGS=$((WARNINGS + 1)); }
fail() { printf '  FAIL  %s\n' "$*"; FAILURES=$((FAILURES + 1)); }

command -v aws >/dev/null 2>&1 || { say "aws CLI not found on PATH"; exit 2; }

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text 2>/dev/null)" \
  || { say "aws sts get-caller-identity failed — credentials not configured?"; exit 2; }

# Deterministic bucket name (infra/modules/s3_archive naming rule).
BUCKET_NAME="${BUCKET_NAME:-${PROJECT_TAG}-pump-archive-${ACCOUNT_ID}}"

# ---------------------------------------------------------------- destroy
if [ "$MODE" != "--verify-only" ]; then
  command -v terraform >/dev/null 2>&1 || { say "terraform not found on PATH"; exit 2; }
  say "==> terraform destroy (infra/, region $REGION, account $ACCOUNT_ID)"
  (cd "$REPO_ROOT/infra" && terraform destroy -auto-approve) \
    || say "WARN: terraform destroy exited non-zero — sweep below tells the truth"
fi
if [ "$MODE" = "--destroy-only" ]; then
  say "==> --destroy-only: skipping verification sweep (NOT recommended)"
  exit 0
fi

# ------------------------------------------------------------------ sweep
# Each check asserts ABSENCE: the underlying CLI call must fail with
# NotFound for the check to pass.
say "==> Verification sweep (every resource must be GONE)"

if aws dynamodb describe-table --table-name "$DDB_TABLE_NAME" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "DynamoDB table $DDB_TABLE_NAME still exists"
else
  ok "DynamoDB table $DDB_TABLE_NAME gone"
fi

TOPIC_ARN="arn:aws:sns:${REGION}:${ACCOUNT_ID}:${SNS_TOPIC_NAME}"
if aws sns get-topic-attributes --topic-arn "$TOPIC_ARN" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "SNS topic $SNS_TOPIC_NAME still exists"
else
  ok "SNS topic $SNS_TOPIC_NAME gone"
fi

# Orphaned subscriptions: a PendingConfirmation sub is undeletable
# until AWS expires it (~3 days) — WARN, anything confirmed is FAIL.
SUBS="$(aws sns list-subscriptions --region "$REGION" \
          --query "Subscriptions[?TopicArn=='${TOPIC_ARN}'].SubscriptionArn" \
          --output text 2>/dev/null)"
if [ -n "$SUBS" ] && [ "$SUBS" != "None" ]; then
  for SUB in $SUBS; do
    if [ "$SUB" = "PendingConfirmation" ]; then
      warn "SNS subscription pending confirmation — undeletable; AWS expires it in ~3 days"
    else
      fail "SNS subscription still exists: $SUB"
    fi
  done
else
  ok "no SNS subscriptions for $SNS_TOPIC_NAME"
fi

for FN in "$SCORER_FN" "$ADAPTER_FN" "$BATCHER_FN" "$FLEET_FN"; do
  if aws lambda get-function --function-name "$FN" \
       --region "$REGION" >/dev/null 2>&1; then
    fail "Lambda $FN still exists"
  else
    ok "Lambda $FN gone"
  fi
  GROUPS_FOUND="$(aws logs describe-log-groups \
      --log-group-name-prefix "/aws/lambda/${FN}" --region "$REGION" \
      --query 'logGroups[].logGroupName' --output text 2>/dev/null)"
  if [ -n "$GROUPS_FOUND" ] && [ "$GROUPS_FOUND" != "None" ]; then
    fail "log group(s) still exist: $GROUPS_FOUND"
  else
    ok "log group /aws/lambda/${FN} gone"
  fi
done

if aws lambda get-function-url-config --function-name "$ADAPTER_FN" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "Function URL for $ADAPTER_FN still exists"
else
  ok "Function URL for $ADAPTER_FN gone"
fi

if aws iot get-topic-rule --rule-name "$IOT_RULE_NAME" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "IoT rule $IOT_RULE_NAME still exists"
else
  ok "IoT rule $IOT_RULE_NAME gone"
fi

for ROLE in "${SCORER_FN}-exec" "${ADAPTER_FN}-exec" "${BATCHER_FN}-exec" "${FLEET_FN}-exec" \
            "${IOT_RULE_NAME}_error_republish"; do
  if aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
    fail "IAM role $ROLE still exists"
  else
    ok "IAM role $ROLE gone"
  fi
done

# ---------------------------------------------- cold-path sweep (ADR 0015)
if aws s3api head-bucket --bucket "$BUCKET_NAME" >/dev/null 2>&1; then
  OBJ_COUNT="$(aws s3api list-objects-v2 --bucket "$BUCKET_NAME" \
      --query 'KeyCount' --output text 2>/dev/null)"
  fail "S3 bucket $BUCKET_NAME still exists (${OBJ_COUNT:-?} objects) — force_destroy should have emptied AND removed it"
else
  ok "S3 bucket $BUCKET_NAME gone"
fi

if aws glue get-database --name "$GLUE_DB_NAME" --region "$REGION" >/dev/null 2>&1; then
  fail "Glue database $GLUE_DB_NAME still exists"
else
  ok "Glue database $GLUE_DB_NAME gone"
fi

if aws glue get-table --database-name "$GLUE_DB_NAME" --name "$GLUE_TABLE_NAME" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "Glue table $GLUE_DB_NAME.$GLUE_TABLE_NAME still exists"
else
  ok "Glue table $GLUE_DB_NAME.$GLUE_TABLE_NAME gone"
fi

if aws events describe-rule --name "${BATCHER_FN}-schedule" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "EventBridge rule ${BATCHER_FN}-schedule still exists"
else
  ok "EventBridge rule ${BATCHER_FN}-schedule gone"
fi

if aws events describe-rule --name "${FLEET_FN}-schedule" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "EventBridge rule ${FLEET_FN}-schedule still exists"
else
  ok "EventBridge rule ${FLEET_FN}-schedule gone"
fi

# ---------------------------------------------- iot-fleet sweep (ADR 0016)
i=0
while [ "$i" -lt "$FLEET_SIZE" ]; do
  THING="$(printf 'P-%02d' "$i")"
  if aws iot describe-thing --thing-name "$THING" \
       --region "$REGION" >/dev/null 2>&1; then
    fail "IoT Thing $THING still exists"
  else
    ok "IoT Thing $THING gone"
  fi
  i=$((i + 1))
done

if aws iot get-policy --policy-name "$IOT_POLICY_NAME" \
     --region "$REGION" >/dev/null 2>&1; then
  fail "IoT policy $IOT_POLICY_NAME still exists"
else
  ok "IoT policy $IOT_POLICY_NAME gone"
fi

# Certificates have no stable names, so this is count-based. FAIL, not
# WARN (2026-06-07 cascade pt 4, PO-accepted): in a dedicated demo
# account the expected post-destroy count is always 0, so any ACTIVE
# cert — fleet leftover or stray Console experiment — should block and
# be investigated, not skimmed past. The fleet's own certs are deleted
# by terraform destroy via their attachments.
ACTIVE_CERTS="$(aws iot list-certificates --region "$REGION" \
    --query "certificates[?status=='ACTIVE'] | length(@)" --output text 2>/dev/null)"
if [ -n "$ACTIVE_CERTS" ] && [ "$ACTIVE_CERTS" != "None" ] && [ "$ACTIVE_CERTS" -gt 0 ] 2>/dev/null; then
  fail "$ACTIVE_CERTS ACTIVE IoT certificate(s) remain — expected 0 in a dedicated demo account (aws iot list-certificates)"
else
  ok "no ACTIVE IoT certificates remain"
fi

# Local key material: terraform destroy removes the local_sensitive_file
# resources; leftovers mean destroy was interrupted or files were copied.
LEFTOVER_KEYS="$(find "$REPO_ROOT/simulator/.secrets" -name '*.private.key' 2>/dev/null | wc -l | tr -d ' ')"
if [ "${LEFTOVER_KEYS:-0}" -gt 0 ]; then
  warn "$LEFTOVER_KEYS private key file(s) remain under simulator/.secrets — delete them (the cloud certs they matched are gone)"
else
  ok "no private key files remain under simulator/.secrets"
fi

# -------------------------------------------------- budget-alert posture
# The $1/$5 alerts (ACCOUNT_SETUP.md) must OUTLIVE every teardown —
# their absence is a cost blind spot, so it's a FAIL, not a WARN.
say "==> Budget-alert posture (\$1 + \$5 must exist)"
BUDGET_LIMITS="$(aws budgets describe-budgets --account-id "$ACCOUNT_ID" \
    --query 'Budgets[].BudgetLimit.Amount' --output text 2>/dev/null)"
for AMOUNT in 1 5; do
  if printf '%s\n' "$BUDGET_LIMITS" | tr '\t' '\n' | grep -qE "^${AMOUNT}(\.0+)?$"; then
    ok "\$${AMOUNT} budget alert present"
  else
    fail "\$${AMOUNT} budget alert NOT FOUND — re-create per ACCOUNT_SETUP.md"
  fi
done

# ----------------------------------------------------------------- report
say "==> Teardown sweep: $FAILURES failure(s), $WARNINGS warning(s)"
if [ "$FAILURES" -gt 0 ]; then
  say "RESIDUE DETECTED — costs may accrue. Delete the resources above manually,"
  say "then re-run: ./scripts/aws_teardown.sh --verify-only"
  exit 1
fi
say "All clear: nothing billable is standing."
