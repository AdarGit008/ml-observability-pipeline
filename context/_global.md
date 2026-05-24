# _global.md — always-loaded context

The minimum every session needs to know. Keep this lean.

## Project one-liner
Real-time ML observability pipeline for a simulated fleet of industrial pumps. Scores failure probability, detects data/model drift. Portfolio project for AWS student application.

## Hard constraints (do not violate without ADR)
1. **$0 lifetime cost.** Always-Free AWS services or local-only.
2. **Single PC.** No spare hardware.
3. **AWS-specific.** Avoid choices with clean GCP/Azure analogues.
4. **Mode parity.** Local and AWS demo modes share scoring/drift logic.
5. **Region: `eu-central-1` only.** Cross-region transfer adds cost.

## Tech locks (from PLAN.md §0)
- Language: Python 3.12
- IaC: Terraform
- Hot store: DynamoDB (not Timestream — closed to new accounts 2025-06-20)
- Archive: S3 + Glue Catalog (schema in Terraform, no Crawler)
- Batching: Lambda + EventBridge (not Firehose — no Always-Free)
- Local TSDB: InfluxDB OSS in Docker
- Dashboards: Grafana local Docker (not Amazon Managed Grafana — $9/user/month)
- MQTT broker (local mode): Mosquitto in Docker
- Drift metric: PSI (Population Stability Index)
- Model: HistGradientBoostingClassifier

## Style preferences (from HANDOFF.md §9)
- Direct, terse, structured. Match `PLAN.md` tone.
- Recommend defaults with one-sentence rationale; don't enumerate exhaustively.
- Prose over deep bullet trees when it reads better.
- Save substantial outputs as files, not chat walls.

## Anti-patterns (never deploy / never do)
- EC2 of any size.
- Amazon Managed Grafana.
- Kinesis Firehose.
- Glue Crawlers.
- Timestream-for-InfluxDB (RDS-style; ~$50/mo floor).
- Anything outside `eu-central-1` during demo.

## Cost guardrails (already configured per ACCOUNT_SETUP.md)
- Budget alert at $1 (email).
- Budget alert at $5 (email + SMS, will eventually trigger a disable-rules Lambda).
- `aws_teardown.sh` after every demo, no exceptions.

## Where to look next
- Workflow + roles: `DEV_NORMS.md`
- Component being worked on: `context/<component>.md`
- Cross-component data shapes: `context/_interfaces.md`
- Past decisions: `docs/adr/`
- Past sessions: `docs/sessions/`
