# Project Handoff — ML Observability Pipeline for Predictive Maintenance

> **Instructions for next Claude:** Read this entire document, then read PLAN.md and ACCOUNT\_SETUP.md (attached separately). Your job in this chat is described in section 9. Don't start producing code or diagrams until the spec is finalized.

## 1\. Context and purpose

**Why this project exists:** Portfolio piece for a student-role application at AWS. Selected for differentiation in the applicant pool and alignment with two growing R\&D themes at AWS: MLOps / model observability, and industrial IoT.

**Hard constraints:**

  - Total project cost must be **$0** over the project lifetime.

  - Built and demonstrated entirely from **a single PC** (no spare hardware).

  - AWS account is **new** (post-July 2025), meaning credit-based free tier rather than the legacy 12-month free tier.

**Portfolio goals:**

  - Heavy use of AWS-specific services with no clean GCP/Azure analogues, to make the AWS connection unmistakable.

  - One polished repo, not five half-finished ones.

  - Trade-off rationale visible everywhere (README, ADRs, commit messages).

  - Architecture diagram is mandatory.

  - 60-second demo recording is mandatory.

## 2\. Project summary

**One-liner:** A real-time ML observability pipeline that monitors a simulated fleet of industrial pumps, scores them with a predictive-maintenance model, and detects data/model drift before it degrades operations.

**Scenario:**

  - \~15 simulated industrial pumps in a manufacturing plant

  - Each pump emits sensor readings every 2 seconds: vibration amplitude, bearing temperature, motor current draw, RPM

  - ML model predicts probability of bearing failure within next 48 hours

  - The pipeline observes the model's live behavior and catches drift early

**Three drift demo scenarios (the heart of the project):**

1.  **Seasonal drift** — rising ambient temperature shifts bearing temps across the fleet; model over-predicts failure; pipeline flags input drift.

2.  **Fleet expansion** — 3 new pumps with a different baseline vibration signature; model mis-scores them; pipeline flags unseen distribution.

3.  **Real failure** — one pump genuinely degrades; vibration + temp trend up; model correctly predicts; pipeline shows the system also works as designed (drift on one device with rising scores ≠ drift across the fleet with rising scores).

## 3\. Critical context: AWS Free Tier rules (post-July 2025)

**This is the most important context.** The original plan assumed the legacy 12-month free tier. That no longer exists for new accounts. The architecture has already been adjusted to fit the new rules, but the next Claude must understand why certain choices were made.

**New account rules (effective July 15, 2025):**

  - $100 credits at signup + up to $100 more by completing 5 onboarding activities ($20 each: EC2, RDS, Lambda, Bedrock, Budget setup)

  - Credits expire 12 months from account creation

  - Choice at signup: Free Plan (auto-closes after 6 months, restricted services) or Paid Plan (continues, full access, charges if you exceed Always-Free)

  - **User has chosen Paid Plan** — to avoid auto-closure, since recruiters may revisit the repo months later

**Always-Free services we rely on:**

  - Lambda: 1M requests + 400K GB-seconds/month

  - DynamoDB: 25 GB + 25 WCU/RCU

  - SNS: 1M publishes + 1,000 email deliveries

  - CloudWatch: 10 custom metrics + 1M API requests

**Services that look free but aren't (must avoid):**

  - **Amazon Timestream for LiveAnalytics** — closed to new customers on June 20, 2025. We literally cannot create Timestream databases on a new account. This drove a major architecture change.

  - **Kinesis Firehose** — no Always-Free tier.

  - **Amazon Managed Grafana** — $9/user/month.

  - **EC2** — credit-based for new accounts; we run Grafana locally instead.

  - **Glue Crawlers** — per-run cost.

  - **RDS for Timestream-for-InfluxDB** — \~$50/month minimum.

**Services that draw small amounts from credits:**

  - IoT Core (\~$1/M messages)

  - S3 (pennies for our volumes)

  - Athena ($5/TB scanned; our scans are MB-scale)

## 4\. Architecture (current state)

The architecture uses **two runtime modes** with identical scoring/drift logic:

### Mode A — Local dev (default, runs continuously)

Everything in Docker Compose on the PC. Zero AWS, zero cost.

PC (Docker Compose):

├── Mosquitto MQTT broker

├── Simulator (Python, asyncio + paho-mqtt) — 15 virtual pumps

├── local\_runtime/scorer\_service.py — subscribes to MQTT, scores, computes drift

├── InfluxDB OSS — time-series store

└── Grafana — dashboards reading InfluxDB

### Mode B — AWS demo (ephemeral, 30-minute sessions)

For recording the demo video and answering "is this real AWS?" in interviews. terraform apply → record → terraform destroy.

AWS (eu-central-1, Frankfurt):

├── IoT Core — MQTT ingestion, 15 Things, mTLS

├── IoT Rule A → lambda\_scorer (hot path)

│ └── reads/writes DynamoDB hot state, publishes to SNS on alert

├── IoT Rule B → lambda\_s3\_batcher (cold path, replaces Firehose)

│ └── batches 60s of messages into Parquet files in S3

├── DynamoDB — per-pump hot state (last N readings, PSI accumulator, latest score)

├── S3 — Parquet archive with Glue Catalog table (no Crawler — schema in Terraform)

├── SNS — email alerts

└── Lambda Function URL — serves DynamoDB queries to local Grafana

PC during AWS demo:

└── Grafana switches its datasource from local InfluxDB to the Lambda URL

### Why these choices (short version)

| **Original choice**    | **Replaced with**                       | **Reason**                         |
| ---------------------- | --------------------------------------- | ---------------------------------- |
| Timestream (hot + viz) | DynamoDB (state) + local InfluxDB (viz) | Timestream closed to new customers |
| Kinesis Firehose       | Lambda + EventBridge batching to S3     | Firehose has no Always-Free tier   |
| EC2-hosted Grafana     | Grafana in Docker on PC                 | EC2 burns credits; PC is free      |
| Glue Crawler           | Manual table definition in Terraform    | Crawlers cost per run              |
| Amazon Managed Grafana | Local Grafana                           | $9/user/month                      |

The trade-offs are framed as portfolio assets in the README: "I wanted Timestream, AWS closed it to new customers, so I made an architectural choice between X and Y."

## 5\. Decisions locked

| **Decision**           | **Choice**                                                                            |
| ---------------------- | ------------------------------------------------------------------------------------- |
| Equipment type         | Industrial pumps (×15)                                                                |
| IaC                    | Terraform                                                                             |
| AWS region             | eu-central-1 (Frankfurt) — closest full-feature region to Haifa                       |
| Account plan           | Paid Plan with strict budget alarms                                                   |
| Primary drift metric   | Population Stability Index (PSI), thresholds 0.1 / 0.25                               |
| Secondary drift metric | Kolmogorov–Smirnov                                                                    |
| Model                  | scikit-learn HistGradientBoostingClassifier (\<500 KB pickled)                        |
| Local-dev stack        | Docker Compose: Mosquitto + InfluxDB + Grafana                                        |
| Hot state              | DynamoDB                                                                              |
| Time-series viz store  | InfluxDB OSS in Docker (local)                                                        |
| Cold storage           | S3 via Lambda batching                                                                |
| Dashboard              | Local Grafana with two datasources (InfluxDB for local mode, Lambda URL for AWS mode) |
| Alerts                 | SNS → email                                                                           |

## 6\. Open questions to resolve in this chat

These were left unresolved after the planning chat and must be nailed down before code begins:

1.  **Grafana ↔ DynamoDB adapter design.** The Lambda Function URL approach was sketched but not specified. Options:
    
      - Simple JSON API matching Grafana JSON-datasource plugin format
    
      - Use grafana-dynamodb-datasource community plugin (verify it's maintained)
    
      - Skip dual-mode Grafana entirely; export DynamoDB to InfluxDB during AWS demo
    
      - Decision needs to be made before Week 3.

2.  **Synthetic data calibration.** Pure first-principles physical model, or calibrate noise/degradation curves against NASA IMS or Case Western Reserve bearing datasets? Calibration adds credibility but adds Week 1 scope.

3.  **Lambda model packaging.** Bundle the pickle in the deployment ZIP (simple, \~200 KB), use Lambda Layers (cleaner but more moving parts), or load from S3 at cold start (slower)? Bundle is the current default.

4.  **Reference distribution storage.** operational\_reference\_distribution.json for PSI lives where during AWS mode? S3 (lazy-loaded on cold start), DynamoDB (per-pump record), or bundled in Lambda? Current default: bundled.

5.  **DynamoDB schema.** Composite key options:
    
      - PK = pump\_id, SK = timestamp (one row per reading; hot pumps fan out widely)
    
      - PK = pump\_id\#bucket(1min), SK = timestamp (better locality but query complexity)
    
      - PK = pump\_id, SK = state (single mutable row per pump; lose history)
    
      - Need to pick one and write out the access patterns.

6.  **EventBridge schedule granularity.** The S3 batcher Lambda runs on a schedule; how often? 60s buffers \~7.5K records, which fits in one Parquet file. 5 min reduces invocations but increases data-loss window on errors. Default: 60s.

7.  **Demo video format.** Loom (free with watermark) vs. self-recorded MP4 in GitHub vs. asciinema for the CLI bits. Default: Loom for the dashboard, asciinema or shell recording for the aws\_demo.sh script. Need to confirm.

8.  **Repository visibility timing.** Public from day one (commit history is itself portfolio evidence), or private until Week 4 polish? Default: public from day one with clean commits.

9.  **Branch strategy.** Trunk-based with PRs to main, or feature branches? Default: PRs to main to show review discipline (even self-review).

10. **CI scope.** GitHub Actions running: lint + unit tests (cheap), Terraform plan against a real AWS account (uses credits and adds complexity), LocalStack-based integration tests (free but flaky)? Default: lint + unit tests only for portfolio version.

## 7\. Artifacts produced so far

| **File**          | **Status**                 | **Content**                                                                                                                   |
| ----------------- | -------------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| PLAN.md           | Complete draft (368 lines) | Repo structure, component-by-component design, week-by-week execution plan, cost strategy, risks, definition of done          |
| ACCOUNT\_SETUP.md | Complete (\~200 lines)     | Day-0 checklist: signup decisions, root lockdown, IAM user, budget alarms, earning the $200 in credits, local tooling install |
| HANDOFF.md        | This document              |                                                                                                                               |

**Nothing else exists yet.** No code, no Terraform, no architecture diagram, no GitHub repo, no AWS account created.

## 8\. Current execution status

  - ✅ Project scope and constraints defined

  - ✅ Architecture designed and adjusted for post-July-2025 AWS Free Tier reality

  - ✅ Trade-offs documented at the plan level

  - ✅ AWS account setup procedure documented

  - ⬜ AWS account created

  - ⬜ Open questions from section 6 resolved

  - ⬜ Finalized spec (data schemas, API contracts, exact file layouts)

  - ⬜ Architecture diagram drawn

  - ⬜ GitHub repo created

  - ⬜ Any code written

  - ⬜ Any infrastructure deployed

## 9\. Goal for this chat

**Finalize the spec and architecture, then form an execution plan ready to begin coding.** Specifically:

1.  **Resolve every open question in section 6.** For each, recommend a default, explain trade-offs, and lock the decision.

2.  **Pressure-test the architecture one more time.** Look for issues the prior chat missed. Particularly examine: the Grafana dual-datasource pattern, the cold-start latency of the Lambda scorer with a bundled pickle, the DynamoDB read/write capacity under demo load, the IoT Core mTLS provisioning flow.

3.  **Produce concrete spec artifacts:**
    
      - DynamoDB schema with access patterns
    
      - MQTT topic conventions and payload JSON schema
    
      - PSI calculation parameters (bin count, smoothing, edge cases)
    
      - Lambda environment variables and IAM permissions
    
      - Terraform module interfaces (inputs/outputs)
    
      - Grafana panel queries (one per panel, both modes)

4.  **Form a refined execution plan that supersedes PLAN.md.** Same week-by-week structure, but with the open questions resolved and concrete specs in place. Each task should be small enough to complete in a single coding session.

5.  **Identify the very first thing to build.** Recommend the starting point and explain why.

**What this chat should NOT do:**

  - Write production code

  - Write Terraform

  - Draw the architecture diagram (sketch in text is fine; the real diagram is a Week 1 deliverable)

  - Pressure-test the cost model again — that's been done

**Style preferences:**

  - Direct, terse, structured. Match the tone of PLAN.md.

  - Recommend defaults with one-sentence rationale; don't enumerate every option exhaustively.

  - Mobile-friendly formatting: prose over deep bullet trees where it reads better.

  - Save substantial outputs as markdown files (SPEC.md, updated PLAN.md, etc.) rather than pasting walls of text in chat.

## 10\. Suggested first move for this chat

Start by resolving open question 5 (DynamoDB schema), because every other question depends on knowing how the hot store is shaped. Then 1 (Grafana adapter), then 4 (reference distribution), then the remaining questions in whatever order makes sense. Save the final spec as SPEC.md and produce an updated PLAN.md that references it.
