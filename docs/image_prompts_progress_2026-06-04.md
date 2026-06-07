# ML Observability Pipeline — Progress Visualization Prompts (REV 2026-06-04)

Eight prompts for an AI image generator (Midjourney / DALL-E / Imagen / Flux).
Style: engineering blueprint / schematic — white and pale-cyan technical line art on a deep navy-blue background, annotated like a factory or aerospace blueprint. Monospace label typography, faint grid-paper texture, warm-amber highlights for the current focus.

A short "style anchor" is repeated at the top of each prompt so the eight images render as a coherent set.

> Refresh of the 2026-05-29 pack. Major updates since:
> - Model is REAL (ADR 0006, HistGradientBoostingClassifier, AUC 0.997+) — no longer a stub.
> - Drift is REAL PSI (ADR 0007) + reference source separation (ADR 0008) + PSI surface trimmed to 4 raw signals (ADR 0009).
> - `lambda_scorer` shipped (DynamoDB schema ADR 0010, edge-triggered SNS alerts ADR 0012).
> - Infra hot path in Terraform (ADR 0013, PAY_PER_REQUEST), dashboards adapter (ADR 0014), COLD PATH batcher → S3 + Glue (ADR 0015), teardown script.
> - Suite: 404 passed + 1 skipped. 15 ADRs accepted. NO `terraform apply` yet — apply is a demo-day act.
> - "YOU ARE HERE" marker moves to **Grafana dashboards (JSON pair)**.

**Global guardrail (include verbatim in every prompt):**

```
STRICT CONTENT RULE: This depicts a WORK-IN-PROGRESS engineering project, not a finished product. Render ONLY the components, labels, and stamps listed below — do not invent additional services, logos, charts, metrics, screens, or pipeline stages. Unbuilt items must look visibly unfinished (dashed/ghosted linework), never polished. No marketing gloss, no photorealism, no people.
```

---

## 1) Overview — full production timeline, with "you are here" marker

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace annotation labels, warm-amber accent for the current milestone. Coherent series, image 1 of 8.

STRICT CONTENT RULE: This depicts a WORK-IN-PROGRESS engineering project, not a finished product. Render ONLY the components, labels, and stamps listed below — do not invent additional services, logos, charts, metrics, screens, or pipeline stages. Unbuilt items must look visibly unfinished (dashed/ghosted linework), never polished. No marketing gloss, no photorealism, no people.

A horizontal production timeline of a real-time ML observability pipeline for an industrial pump fleet, drawn as an annotated blueprint. Solid linework = built, dashed/ghosted = not built. Left to right:

(1) ARCHITECTURE + DEV WORKFLOW — small icons for DynamoDB, S3 + Glue catalog, Lambda, EventBridge, AWS IoT Core, Mosquitto, InfluxDB, Grafana. Stamped "LOCKED · 15 ADRs ACCEPTED".

(2) SIMULATOR — 15 industrial centrifugal pumps in a row, P-00…P-14, feeding an MQTT broker. Three inset vignettes: "Seasonal Drift", "Fleet Expansion", "Real Failure (P-07)". Solid lines. Stamped "DONE · ADR 0002-0004".

(3) AWS IOT INGEST — pump P-00 → mTLS certificate icon → AWS IoT Core cloud. Stamped "SMOKE TEST GREEN 2026-05-28".

(4) SHARED MODE-PARITY CORE — framed box `shared/` with three solid sub-blocks: `features.py` (8-feature extractor), `score.py` (real HistGradientBoostingClassifier, "AUC 0.997"), `drift.py` (real PSI, "4 raw signals · ADR 0009"). Stamped "ADR 0005 · PARITY BOUNDARY · ALL REAL".

(5) LOCAL RUNTIME (DOCKER) — subscriber → 5-min rolling window per pump → shared/ core → InfluxDB writer, Docker-Compose badge (mosquitto, influxdb). Solid. Stamped "DONE".

(6) MODEL + DRIFT — model.pkl + reference_distribution.json artifacts flowing into shared/. Solid. Stamped "ADR 0006-0009 · DONE".

(7) LAMBDA SCORER + DYNAMODB + SNS — IoT Rule trigger → Lambda (imports shared/ parity bundle) → DynamoDB "pump_hot_state" table → SNS bell icon "edge-triggered alerts". Solid. Stamped "ADR 0010 · 0012 · DONE".

(8) INFRA AS CODE (TERRAFORM) — module blocks {dynamodb, sns, iam, lambda_scorer, iot_rule, dashboards_adapter, s3_archive, glue_catalog, lambda_s3_batcher} + a teardown-script broom icon. Solid frames, but a clear amber tag "NO APPLY YET — DEMO-DAY ACT". Stamped "ADR 0013-0015".

(9) COLD PATH — EventBridge clock (60 s) → batcher Lambda (watermark icon) → S3 bucket + Glue catalog. Solid. Stamped "ADR 0015 · DONE (commit pending)".

(10) GRAFANA DASHBOARDS — two dashboard JSON sheets side by side labeled "local.json (InfluxDB)" and "aws.json (adapter)". Dashed amber linework, glowing amber crosshair "YOU ARE HERE — NEXT SESSION". Adapter contract noted "ADR 0014 locked".

(11) DEMO DELIVERY — dashed final stamp "3 SCENARIO DEMOS + APPLY/TEARDOWN + PORTFOLIO PACKAGING — PENDING".

Top-right title block: "ML OBSERVABILITY PIPELINE / PORTFOLIO BUILD / REV. 2026-06-04 / SHEET 1 OF 8 — OVERVIEW".
Bottom-left budget stamp: "$0 AWS CEILING · eu-central-1 · accepted exception ≈ $0.10-0.20 per demo (ADR 0013)".
Bottom-right legend: "DONE (solid white) / NEXT (amber crosshair) / PENDING (dashed pale-cyan)".
Bottom-center quality stamp: "404 TESTS PASSED · 1 SKIPPED".

Wide 16:9. Crisp vector linework, dense annotation, blueprint feel.
```

---

## 2) What's delivered already

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace annotation labels, warm-amber milestone stamps. Coherent series, image 2 of 8.

STRICT CONTENT RULE: This depicts a WORK-IN-PROGRESS engineering project, not a finished product. Render ONLY the components, labels, and stamps listed below — do not invent additional services, logos, charts, metrics, screens, or pipeline stages. No marketing gloss, no photorealism, no people.

An exploded blueprint view of everything already built, eight framed panels on one sheet, dimensioned like an engineering drawing:

PANEL A — "DEV WORKFLOW". Three badges connected by arrows: "Product Owner", "Lead Architect (Claude)" holding ADR scrolls, "Reviewer cascade (Gemini + multi-provider, ADR 0011)" with a magnifying glass. Annotation: "15 ADRs accepted".

PANEL B — "SIMULATOR + SCENARIOS". Cutaway of one centrifugal pump (pressure, flow, vibration, temperature, bearing wear callouts), row of 15 pump icons → "asyncio + aiomqtt, 0.5 Hz", three scenario vignettes. Stamp "ADR 0002-0004".

PANEL C — "AWS IOT LIVE + ACCOUNT HARDENED". Pump P-00 → mTLS cert → AWS IoT Core; IAM shield, root-MFA padlock, two budget gauges "$1" and "$5" both ARMED. Stamp "SMOKE TEST GREEN".

PANEL D — "MODEL". HistGradientBoostingClassifier block → model.pkl + reference_distribution.json. Annotation "AUC 0.997 on held-out pumps · ADR 0006".

PANEL E — "DRIFT (REAL PSI)". Two overlaid binned histograms (reference vs live), "Laplace add-α" note, "reference = demo-paced healthy fleet (ADR 0008)", "PSI surface = 4 raw signals only (ADR 0009)".

PANEL F — "LAMBDA SCORER". IoT Rule → Lambda importing `shared/` (parity-bundle annotation) → DynamoDB table "pump_hot_state (ADR 0010)" → SNS "edge-triggered alerts (ADR 0012)".

PANEL G — "INFRA AS CODE". Terraform module grid: dynamodb, sns, iam, lambda_scorer, iot_rule, dashboards_adapter, s3_archive, glue_catalog, lambda_s3_batcher; build scripts + teardown broom "aws_teardown.sh". Amber tag "validated, NOT applied — demo-day act". Stamp "ADR 0013-0015".

PANEL H — "COLD PATH". EventBridge 60 s clock → batcher Lambda with watermark gauge "at-least-once, never regress" → S3 bucket → Glue catalog "partition projection, no Crawler". Stamp "ADR 0015".

Top-right title block: "DELIVERED / REV. 2026-06-04 / SHEET 2 OF 8".
Bottom-right: large warm-amber inspector stamp "404 PASSED · 1 SKIPPED".

Wide 16:9. Crisp vector linework. No photographic elements.
```

---

## 3) What's yet to deliver

```
Engineering blueprint schematic on dark navy paper, faint grid texture, monospace labels — but this sheet is deliberately SPARSE and mostly dashed/ghosted pale-cyan linework, signaling unfinished work. Coherent series, image 3 of 8.

STRICT CONTENT RULE: This depicts the UNBUILT remainder of a work-in-progress project. Render ONLY the items listed — do not invent extra features, screens, or stages. Everything here is dashed, ghosted, or sketched; nothing looks finished. No marketing gloss, no photorealism, no people.

A blueprint sheet titled "REMAINING WORK", four dashed panels with generous empty grid space:

PANEL 1 — "GRAFANA DASHBOARD PAIR" (amber crosshair, "NEXT SESSION"). Two ghosted dashboard sheets: "local.json → InfluxDB" and "aws.json → Infinity plugin → adapter Function URL". Sketched panel placeholders labeled only: fleet score heatmap, per-pump score timeseries, 4× PSI timeseries, alert-state table, pumps_reporting stat. Note "provisioning-as-code via docker compose — decision pending".

PANEL 2 — "TERRAFORM-MANAGED IOT THINGS". Ghosted rack of certificates P-00…P-14 with a Terraform icon. Note "Console-provisioned today — won't scale by hand".

PANEL 3 — "DEMO DAY". Dashed sequence: `terraform apply` → run 3 scenarios (Seasonal Drift / Fleet Expansion / Real Failure) → screenshots/recording → `aws_teardown.sh` sweep → budget check. Note "apply is intentionally deferred — $0 ceiling".

PANEL 4 — "PORTFOLIO PACKAGING". Ghosted README + architecture one-pager + application submission envelope stamped "AWS STUDENT ROLE".

Top-right title block: "YET TO DELIVER / REV. 2026-06-04 / SHEET 3 OF 8".
Bottom-left: small annotation "everything else on this project: see Sheet 2 — DONE".

Wide 16:9. Sparse, unfinished, honest. Crisp dashed vector linework.
```

---

## 4) Deep dive — Simulator & scenario engine

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace labels, warm-amber accents. Coherent series, image 4 of 8.

STRICT CONTENT RULE: Render ONLY what is listed. No invented sensors, services, or UI. No photorealism, no people.

A detailed component blueprint of the pump fleet simulator, single large sheet:

CENTER — exploded cutaway of one industrial centrifugal pump, engineering callouts to its simulated state variables: pressure, flow rate, vibration, temperature, bearing wear (bearing wear coupled to RPM — annotation "ADR 0002"). 

LEFT — a fleet rack of 15 pump silhouettes P-00…P-14, each with its own asyncio task line converging into an "aiomqtt publisher · 0.5 Hz" block, then forking to two outputs: "Mosquitto (local)" and "AWS IoT Core via mTLS (config.aws-iot.yaml)". Annotation "ADR 0003 · per-pump tasks".

RIGHT — the scenario controller drawn as a control panel: a `Scenario` ABC master dial with three selectable modes wired through a `make_scenario` factory into the fleet: 
  • SEASONAL DRIFT — sinusoidal ambient curve overlay,
  • FLEET EXPANSION — ghost pumps P-15+ joining the rack mid-run,
  • REAL FAILURE — P-07 highlighted with a rising bearing-wear curve.
Annotation "tick-driven controller · ADR 0004".

BOTTOM STRIP — config.yaml document icon feeding the whole sheet, "config-driven fleet".

Title block: "SIMULATOR / SHEET 4 OF 8 / REV. 2026-06-04". Stamp: "DONE · SMOKE-TESTED LOCAL + AWS IOT".

Wide 16:9. Crisp vector linework, dense but readable annotation.
```

---

## 5) Deep dive — shared/ parity core & local runtime

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace labels, warm-amber accents. Coherent series, image 5 of 8.

STRICT CONTENT RULE: Render ONLY what is listed. No invented modules or screens. No photorealism, no people.

A blueprint about ONE idea: two runtimes, one brain.

CENTER — a sealed, prominently framed vault labeled `shared/` containing three modules drawn as precision-machined parts:
  • `features.py` — 8-feature extractor gear,
  • `score.py` — HistGradientBoostingClassifier block stamped "AUC 0.997",
  • `drift.py` — PSI comparator with twin histograms, sub-label "PSI on 4 raw signals only (ADR 0009)".
Vault stamp: "MODE-PARITY BOUNDARY · ADR 0005 · NO VENDORING — enforced by test".

LEFT (LOCAL MODE) — Docker-Compose ship icon: Mosquitto container → `TelemetrySubscriber` (single wildcard connection `factory/pumps/+/telemetry`) → per-pump 5-minute rolling deque windows → arrow INTO the shared/ vault → arrow OUT to InfluxDB v2 cylinder. Label "ALWAYS-ON · runs on one PC".

RIGHT (AWS MODE) — AWS IoT Core cloud → IoT Rule → `lambda_scorer` Lambda block → the SAME arrow into the SAME shared/ vault → out to DynamoDB table. Label "EPHEMERAL · demo-day only".

Both arrows into the vault drawn identically and tagged "IDENTICAL CODE PATH".

OUTSIDE THE VAULT, clearly separated by a dashed exclusion boundary: `dashboards_adapter` and `lambda_s3_batcher` small blocks tagged "outside parity set — inverse-import test pins it".

Title block: "MODE PARITY / SHEET 5 OF 8 / REV. 2026-06-04".

Wide 16:9. Crisp vector linework, strong central symmetry.
```

---

## 6) Deep dive — Model & drift detection

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace labels, warm-amber accents. Coherent series, image 6 of 8.

STRICT CONTENT RULE: Render ONLY what is listed. No invented metrics, dashboards, or model internals. No photorealism, no people.

A two-half blueprint sheet:

TOP HALF — "FAILURE-PROBABILITY MODEL (ADR 0006)". Training corpus drawn as stacked telemetry sheets from healthy + failing pumps → feature bench with 8 labeled slots (4 raw signals + 4 rolling-window features) → HistGradientBoostingClassifier drawn as a gradient-boosted tree ensemble silhouette → two output artifacts on a shelf: `model.pkl` and `reference_distribution.json`, version tag "v0.1.0-seed-0". Dial gauge "AUC 0.9972 · held-out pumps".

BOTTOM HALF — "DRIFT DETECTION (ADR 0007-0009)". A comparator instrument: REFERENCE histogram (labeled "demo-paced HEALTHY fleet — source-separated, ADR 0008") vs LIVE histogram, needle gauge reading PSI with three zones etched "STABLE / MODERATE / SIGNIFICANT". Engraved notes: "np.histogram bins + Laplace add-α", "PSI computed on 4 RAW signals only — rolling features excluded, their overlapping windows violate IID (ADR 0009)".

A thin amber annotation thread connects the two halves: "same features module feeds both — shared/ parity".

Title block: "MODEL + DRIFT / SHEET 6 OF 8 / REV. 2026-06-04". Stamp: "DONE".

Wide 16:9. Instrument-panel aesthetic within blueprint style.
```

---

## 7) Deep dive — AWS hot path (scorer, hot state, alerts, adapter)

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace labels, warm-amber accents. Coherent series, image 7 of 8.

STRICT CONTENT RULE: Render ONLY what is listed. No invented AWS services, no extra dashboards or screens. No photorealism, no people.

A left-to-right signal-flow blueprint of the AWS demo-mode hot path:

(1) AWS IoT Core cloud receiving `factory/pumps/+/telemetry`, with a small error-branch arrow to topic `factory/errors` labeled "IoT Rule error_action".
(2) IoT Rule block triggering →
(3) `lambda_scorer` Lambda — cutaway shows the `shared/` parity bundle inside (features → score → PSI), annotation "boto3 NOT bundled — runtime-provided".
(4) DynamoDB table `pump_hot_state` drawn as a card catalog: per-pump rows + a reserved "WATERMARK" sort-key drawer. Stamps "schema ADR 0010" and "PAY_PER_REQUEST — provisioned mode infeasible, ADR 0013 · ≈$0.10-0.20/demo accepted".
(5) Branch upward: SNS bell labeled "EDGE-TRIGGERED — alert on state CHANGE only (ADR 0012)" → email envelope.
(6) Branch rightward: `dashboards_adapter` Lambda (annotation "BatchGetItem-only IAM · reserved concurrency 5 · OUTSIDE parity set") → "Function URL (AuthType=NONE)" plug socket → dashed Grafana panel placeholder tagged "consumer not built yet — Sheet 3". Contract stamp "ADR 0014: envelope + flat pumps[] + pumps_reporting".

BOTTOM STRIP — a broom icon labeled `aws_teardown.sh`: "destroy + absence sweep + budget check — nothing left running".

Title block: "AWS HOT PATH / SHEET 7 OF 8 / REV. 2026-06-04". Amber corner tag: "TERRAFORM READY — NOT APPLIED".

Wide 16:9. Crisp vector linework.
```

---

## 8) Deep dive — Cold path (archive to S3 + Glue)

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace labels, warm-amber accents. Coherent series, image 8 of 8.

STRICT CONTENT RULE: Render ONLY what is listed. No invented analytics tools, query engines, or charts. No photorealism, no people.

A blueprint of the archival cold path (ADR 0015), drawn as a careful conveyor:

(1) EventBridge clock face ticking "every 60 s" →
(2) `lambda_s3_batcher` Lambda — cutaway shows three internal stations:
    • per-pump Query against DynamoDB reading rows newer than each pump's WATERMARK,
    • a safety-lag gate "cutoff = now − 5 s",
    • a pyarrow press stamping rows into a Parquet brick (annotation "pyarrow, no pandas · ~100 MB unzipped"). 
   Engraved rule beneath: "AT-LEAST-ONCE: watermark advances ONLY after S3 put — never regresses. Empty batch = true no-op."
(3) S3 bucket vault labeled "<tag>-pump-archive-<acct>" with partition shelving "year=/month=/day=" →
(4) Glue Data Catalog ledger book, annotation "partition projection — no Crawler, no CreatePartition calls".

SIDE PANEL — free-tier meter: "S3 5 GB Always-Free · Glue 1M objects Always-Free · residue ≈ $0.0002/demo (recorded in ADR 0015)". 
SMALL NOTE — "reserved concurrency = 1 — single batcher, no races".

Title block: "COLD PATH / SHEET 8 OF 8 / REV. 2026-06-04". Stamp: "BUILT · 18 moto TESTS · commit pending PO-side".

Wide 16:9. Crisp vector linework, conveyor-line composition.
```
