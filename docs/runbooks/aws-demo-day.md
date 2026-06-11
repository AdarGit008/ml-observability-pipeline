# Runbook — AWS demo day (apply → simulate → observe → teardown)

End-to-end sequence for the AWS-mode demo: 15 simulated pumps publish
to IoT Core over mTLS, the hot path scores into DynamoDB, the cold
path archives to S3, Grafana renders `dashboards/aws.json` via the
adapter Function URL. Written by the 2026-06-07 iot-fleet session
(ADR 0016); the hot/cold-path pieces are per ADR 0010–0015.

**Cost posture:** IoT Core usage sits inside the 12-month free tier
(verified 2026-06-07, ADR 0016 §Cost); DynamoDB on-demand bills
~$0.10–0.20/demo (ADR 0013); S3/Glue/EventBridge are noise (ADR 0015).
Teardown after EVERY demo, no exceptions.

## 0. One-time pre-flight (first apply only)

- [ ] **Delete the Console-provisioned `P-00`** from the 2026-05-27
  smoke test — Terraform's `aws_iot_thing.pump[0]` collides with it.
  In the IoT Console (eu-central-1): detach + deactivate + delete its
  certificate, delete its per-Thing policy, delete the Thing. Also
  remove the old local copies (`simulator/.secrets/P-00/`) — apply
  rewrites that directory.
- [ ] `infra/terraform.tfvars` exists (copy from `terraform.tfvars.example`,
  set `alert_email`).
- [ ] First-ever apply: click the SNS subscription confirmation email
  or alerts never fire.

## 0.5 Pre-demo checks (every apply)

- [ ] **Reserved concurrency vs account quota** (2026-06-07 live-apply
  lesson; 2026-06-10 review F4). Both Lambdas ship with
  `reserved_concurrent_executions = -1` (no reservation) because the
  new-account Lambda concurrency quota sits at the floor — min-10-unreserved
  rejects ANY reservation. **If the account quota has since been raised
  above the floor**, restore the caps before applying:
    - `dashboards_adapter` -> `reserved_concurrency = 5` (variable default,
      `infra/modules/dashboards_adapter/variables.tf`)
    - `lambda_s3_batcher` -> `reserved_concurrent_executions = 1`
      (`infra/modules/lambda_s3_batcher/main.tf`)
  then `terraform apply` and confirm no quota-violation error.
- [ ] **Uncapped public adapter URL while the stack is up** (2026-06-10
  review F2). With `reserved_concurrency = -1` the adapter Function URL
  (AuthType=NONE, ADR 0014) can scale to the account concurrency limit —
  unauthenticated and uncapped. Standing exposure is zero (teardown after
  every demo), but during a demo there is no in-demo throttle. Watch
  CloudWatch `AWS/Lambda ConcurrentExecutions` for `pump-dashboard-adapter`;
  if it spikes unexpectedly, take the stack down (`aws_teardown.sh`).

## 1. Build + apply (PowerShell, repo root)

```powershell
.\scripts\build_lambda.ps1      # stages .build/lambda_dist/
.\scripts\build_adapter.ps1     # stages .build/adapter_dist/
.\scripts\build_batcher.ps1     # stages .build/batcher_dist/
.\scripts\build_fleet_psi.ps1   # stages .build/fleet_psi_dist/ (ADR 0018)
cd infra
terraform init                  # first time / after provider changes
terraform apply                 # review the plan before yes
```

All FOUR builds MUST precede plan/apply (`archive_file`/`aws_s3_object`
read the staged trees — incl. `fleet_psi_dist`, ADR 0018). Apply provisions the IoT fleet AND writes the cert
material — `simulator/.secrets/AmazonRootCA1.pem` plus
`P-NN/P-NN.cert.pem` + `P-NN.private.key` per pump (ADR 0016; this IS
the "cert pull" step). Plan/apply needs reach to amazontrust.com for
the root CA fetch.

## 2. Point the simulator at the fleet

```powershell
terraform output -raw iot_endpoint   # from infra/
```

Create/update `simulator/config.aws-iot.yaml` (gitignored):

```yaml
fleet:
  pump_count: 15          # must match infra fleet_size (default 15)
  setpoint_rpm: 1800.0
  ambient_celsius: 22.0
  base_seed: 0
scenario: healthy          # or seasonal_drift | fleet_expansion | real_failure
  # NB: with demo_mode: true, HEALTHY dwell compresses to ~60 ticks
  # (simulator/config.py) — the fleet runs the full HEALTHY->FAILED arc
  # in ~15 min. For a steady healthy baseline (healthy-quiet check) OR a
  # clean seasonal_drift fleet demo, set demo_mode: false so pump
  # auto-degradation does not contaminate the signal (2026-06-11 session).
broker:
  target: aws-iot
  url: "<paste iot_endpoint here>"
  tls:
    cert_path: "simulator/.secrets/{pump_id}/{pump_id}.cert.pem"
    key_path:  "simulator/.secrets/{pump_id}/{pump_id}.private.key"
    ca_path:   "simulator/.secrets/AmazonRootCA1.pem"
demo_mode: true
```

`{pump_id}` expands per pump (ADR 0016 §Decision 4). Then:

```powershell
cd ..   # repo root
python -m simulator --config simulator/config.aws-iot.yaml --log-level INFO
```

Expect 15 "pump P-NN connected" lines. A pump looping on
"publisher error … will retry" means a policy/identity mismatch; a
clean immediate exit (code 4) means cert paths are wrong on disk.

## 3. Observe

- **IoT side:** MQTT test client subscribed to `factory/pumps/+/telemetry`
  shows the firehose; `factory/errors` should stay silent (it carries
  scorer-invoke failures past IoT's retries).
- **Hot path:** DynamoDB `pump_hot_state` row counts climb;
  `aws s3 ls s3://<bucket>/year=.../` grows one Parquet/minute (cold
  path breathing, ADR 0015).
- **Grafana:** local Docker Grafana → Infinity datasource base URL =
  `terraform output -raw adapter_function_url` → import
  `dashboards/aws.json`.

### First-apply verification checklist (carried from dashboards #2)

Items checkable only against a live stack (`context/dashboards.md`
§Open questions — close them there once seen green):

- [ ] Infinity panels resolve their **relative URL against the
  datasource base URL** (panels assume base-URL joining; a 404 on
  every panel means this assumption broke).
- [ ] A pump with no alert history renders `last_alert_sent_at:`
  **`null` through the frontend parser** (column is pinned
  `type: string` defensively — verify no `Invalid date`/blank-row
  artifacts).
- [ ] `pumps_reporting` climbs to 15 within ~2 min of simulator start
  (STATE rows appear after each pump's first scored reading).
- [ ] **Cold-start canary** (post-first-apply follow-up, infra
  session #1): note the first invocation's duration in the scorer's
  log group vs warm invocations — the boto3-runtime-version latency
  measurement lives in `context/lambda_scorer.md` open items.

## 4. Teardown (mandatory)

The `.sh` scripts run through Git Bash, whose PATH does NOT include
`aws`/`terraform` (they resolve in PowerShell but not bash). Invoke
with both install dirs prepended (verified 2026-06-07):

```powershell
& "C:\Program Files\Git\bin\bash.exe" -lc 'export PATH="/c/Program Files/Amazon/AWSCLIV2:/c/Users/adar0/AppData/Local/Microsoft/WinGet/Packages/Hashicorp.Terraform_Microsoft.Winget.Source_8wekyb3d8bbwe:$PATH"; cd "/d/Claude/ML Observability Pipeline"; aws --version; terraform -version; bash scripts/aws_teardown.sh'
```

(The `aws --version` / `terraform -version` prefix is the sanity check —
if they print, the script finds them.) Equivalent if `aws`/`terraform`
are already on bash's PATH:

```bash
./scripts/aws_teardown.sh
```

**Gotcha (2026-06-11):** invoking the `.sh` via `& "…bash.exe" -lc '…'`
from PowerShell can swallow ALL stdout (observed twice). Prefer a native
Git Bash terminal; or verify $0 with PowerShell-native `aws` absence
checks (describe-table / list-functions / list-things / s3 ls / list-topics).

Destroys the stack and sweeps for residue: hot path, cold path, AND
the IoT fleet (Things `P-00`–`P-14`, the `pump-fleet-policy`, ACTIVE
certs as WARN, leftover `*.private.key` under `simulator/.secrets/`).
`terraform destroy` deletes the local cert files too — they are
Terraform resources (ADR 0016 §Decision 3). Exit 0 + "All clear"
or fix what it names and re-run `--verify-only`.
