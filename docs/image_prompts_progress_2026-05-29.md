# ML Observability Pipeline — Progress Visualization Prompts (REV 2026-05-29)

Three prompts for an AI image generator (Midjourney / DALL-E / Imagen / Flux).
Style: engineering blueprint / schematic — white and pale-cyan technical line art on a deep navy-blue background, annotated like a factory or aerospace blueprint. Mono-space label typography, faint grid paper texture, occasional warm-amber highlights to mark current focus.

A short "style anchor" is repeated at the top of each prompt so the three images render as a coherent set.

> This is a refresh of the 2026-05-28 prompt pack. Updates:
> - Scenarios are now DONE (ADR 0004) — simulator badge shows 224 tests and all three scenario vignettes are solid lines.
> - The `shared/` mode-parity core is now a first-class block on the timeline (ADR 0005) — shown as solid frames with ghosted interiors for `score.py` and `drift.py` to signal "interface locked, ML stub inside".
> - `local_runtime` (subscriber → 5-min window → InfluxDB) is DONE — 308 total tests, Docker Compose for Mosquitto + InfluxDB.
> - "YOU ARE HERE" marker shifts from Lambda scorer to **Model training** (the next session replaces `shared/score.py`'s stub).
> - Lambda + DynamoDB explicitly tagged "BLOCKED — DynamoDB schema TBD".

---

## 1) Overview — full production timeline, with "you are here" marker

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace annotation labels, warm-amber accent for the current milestone. Coherent series, image 1 of 3.

A horizontal production timeline of a real-time ML observability pipeline for an industrial pump fleet, drawn as an annotated blueprint. Solid linework for built stages, dashed/ghosted linework for unbuilt stages. Left to right:

(1) ARCHITECTURE — small icons for DynamoDB, S3 + Glue catalog, Lambda, EventBridge, AWS IoT Core, Mosquitto, InfluxDB, Grafana. Stamped "LOCKED · 5 ADRs".

(2) SIMULATOR — schematic of 15 industrial centrifugal pumps in a row, labeled P-00 to P-14, feeding an MQTT broker. Three small inset vignettes underneath labeled "Seasonal Drift", "Fleet Expansion (P-15…)", "Real Failure (P-07)" — all shown in solid lines. Stamped "DONE · 224 tests · ADR 0004".

(3) AWS IOT INGEST — pump P-00 connected via mTLS certificate icon to an AWS IoT Core cloud silhouette, packet labeled "factory/pumps/P-00/telemetry". Stamped "SMOKE TEST GREEN 2026-05-28 · CONNACK 0".

(4) SHARED MODE-PARITY CORE — a small framed box in the middle of the sheet labeled `shared/` with three sub-blocks: `features.py` (solid lines — 8-feature extractor, DONE), `score.py` (solid frame, ghosted interior, label "STUB — interface locked"), `drift.py` (solid frame, ghosted interior, label "STUB — PSI interface locked"). Stamped "ADR 0005 · PARITY BOUNDARY".

(5) LOCAL RUNTIME (DOCKER) — subscriber on `factory/pumps/+/telemetry` → 5-min rolling window per pump → InfluxDB writer, fed from Mosquitto, with a small Docker-Compose badge showing two containers (mosquitto, influxdb v2.7). Solid arrow up into the `shared/` core. Stamped "DONE 2026-05-29 · 85 new tests · 308 TOTAL".

(6) MODEL TRAINING — a HistGradientBoostingClassifier icon producing `model.pkl` + `reference_distribution.json`, with a solid amber arrow pointing INTO `shared/score.py` showing it will replace the stub. Marked "NEXT" in warm amber, with a glowing amber crosshair / "YOU ARE HERE" marker centred over this stage.

(7) DRIFT DETECTION (real PSI) — a binned-histogram comparison block with Laplace-smoothing label, arrow into `shared/drift.py`. Dashed lines, "PENDING".

(8) LAMBDA SCORER + DYNAMODB HOT STATE — a Lambda block consuming an IoT Rule trigger, importing from `shared/` (parity-bundle annotation), writing to a DynamoDB table. Dashed lines, with an amber "BLOCKED — DynamoDB schema TBD" tag.

(9) LAMBDA S3 BATCHER — EventBridge clock icon → Lambda → S3 bucket + Glue catalog. Dashed lines, "PENDING".

(10) GRAFANA DASHBOARDS — three panel mocks (failure prob per pump, PSI by feature, fleet heatmap). Small tag pointing back to ADR 0005 with caption "Influx schema already locked". Dashed lines, "PENDING".

(11) DEMO DELIVERY — final stamp "AWS STUDENT APPLICATION — PORTFOLIO READY". Dashed lines, "PENDING".

Top-right corner: blueprint title block "ML OBSERVABILITY PIPELINE / PORTFOLIO BUILD / REV. 2026-05-29 / SHEET 1 OF 3 — OVERVIEW".
Bottom-left corner: budget stamp "$0 AWS CEILING — eu-central-1 · ≈ $40-80 of $200 credit earned".
Bottom-right corner: legend mapping line weight and colour to "DONE (solid white) / NEXT (solid amber) / PENDING (dashed pale-cyan) / BLOCKED (dashed amber)".

Wide 16:9 composition. No photographic elements. No people. Crisp vector linework. Dense annotation, blueprint feel.
```

---

## 2) What's delivered already

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace annotation labels, warm-amber accent for milestone stamps. Coherent series, image 2 of 3.

An exploded blueprint view of the components already built for the ML observability pipeline. Six panels arranged on one sheet, each cleanly framed and dimensioned like an engineering drawing:

PANEL A — "DEV WORKFLOW FRAMEWORK". Three labeled silhouettes connected by arrows: a "Product Owner" badge, a "Lead Architect (Claude)" badge holding ADR scrolls, and a "Reviewer (Gemini CLI)" badge holding a magnifying glass. Annotations: "DEV_NORMS.md", "context/<component>.md", "review_packets/", "ADR-driven · 5 ADRs accepted".

PANEL B — "SIMULATOR + SCENARIOS". A cutaway schematic of one industrial centrifugal pump labeled P-00, with callouts to internal state: pressure, flow, vibration, temperature, bearing wear. Below it, a row of 15 identical pump icons P-00 … P-14 connected to a small box labeled "asyncio + aiomqtt publisher, 0.5 Hz". Three small framed inset vignettes underneath labeled "Seasonal Drift (sinusoidal ambient)", "Fleet Expansion (P-15…)", "Real Failure (P-07 bearing climb)". Stamp: "224 TESTS · ADR 0004".

PANEL C — "LOCAL SMOKE TEST". A Docker container labeled "eclipse-mosquitto" connected to the 15 pumps and to a "mosquitto_sub" subscriber on Windows. Annotation: "All 15 pumps publishing · clean Ctrl+C · 2026-05-27". Small note: "SelectorEventLoop fix applied".

PANEL D — "AWS IOT IS LIVE". A single pump P-00 → certificate + private key icon → mTLS handshake symbol → AWS IoT Core cloud, with a packet bubble "factory/pumps/P-00/telemetry". Stamp: "SMOKE TEST GREEN 2026-05-28 · CONNACK 0".

PANEL E — "LOCAL RUNTIME (NEW 2026-05-29)". A flow diagram: Mosquitto wildcard subscription → `TelemetrySubscriber` (one connection) → per-pump `deque` window (5 min) → `shared/features.extract_features` → `shared/score.score (stub)` → `shared/drift.compute_psi (stub)` → `InfluxWriter` → InfluxDB v2 measurement `pump_telemetry`. Side annotation listing the `shared/` parity boundary and "ADR 0005". Stamp: "+85 TESTS · 308 TOTAL · interfaces LOCKED".

PANEL F — "AWS ACCOUNT HARDENED". An IAM shield labeled "pdm-portfolio · 485215543435 · eu-central-1", root MFA padlock, "dev" user with AdminAccess + MFA, two budget gauges "$1 EMAIL" and "$5 EMAIL+SMS" both ARMED. Side note: "≈ $40–80 of $200 credit earned (Lambda ✓, EC2 ✓)".

Top-right corner: title block "ML OBSERVABILITY PIPELINE / DELIVERED / REV. 2026-05-29 / SHEET 2 OF 3".
Bottom-right corner: a large warm-amber inspector's stamp "PASSED" overlaying the sheet corner.

Wide 16:9 composition. No photographic elements. No people-faces, only badge silhouettes. Crisp vector linework.
```

---

## 3) What's still to deliver

```
Engineering blueprint schematic on dark navy paper, white and pale-cyan technical line art, faint grid texture, monospace annotation labels, warm-amber accents for "BLOCKING" callouts. Coherent series, image 3 of 3.

A blueprint sheet of the components and milestones still to build for the ML observability pipeline, drawn in the same style as the prior sheets but with each block rendered in dashed / "ghosted" lines to indicate "not yet built". Layout:

LEFT COLUMN — "REPLACE THE STUBS":
  • Box "MODEL TRAINING" with HistGradientBoostingClassifier icon, fast-forward simulator arrow producing 30-day × 30-pump training set, output `model.pkl` + `reference_distribution.json`, arrow into `shared/score.py` overwriting the stub. Tagged "NEXT".
  • Box "DRIFT DETECTOR — real PSI" with two histograms (reference vs. current) + Laplace-smoothing badge, arrow into `shared/drift.py` overwriting the stub.

CENTER COLUMN — "AWS HOT PATH":
  • Box "DYNAMODB SCHEMA — HOT STATE" highlighted in warm amber with stamp "BLOCKING — HANDOFF.md §6 Q5".
  • Block "LAMBDA SCORER" with `shared/` parity-bundle annotation, IoT Rule input arrow, DynamoDB output arrow. Below it, a note: "scripts/build_lambda.ps1 stages `shared/` + `lambda_scorer/` into .build/lambda_dist/ before Terraform archive_file" (Gemini Q1 follow-up).
  • Block "LAMBDA S3 BATCHER" connected to EventBridge clock icon, writing Parquet/JSON files to an S3 bucket with a Glue Catalog tag.
  • Block "TERRAFORM-MANAGED IOT THINGS / POLICIES / CERTS" with note "replaces hand-templated Console flow — must scale P-01 … P-14".

RIGHT COLUMN — "DEMO / DELIVERY":
  • Grafana dashboard mock with three small panels: "failure probability per pump", "PSI by feature", "fleet health heatmap". Caption "ADR 0005 schema = panels are pre-wired".
  • Three framed demo vignettes (simulator-side is already DONE — these vignettes show the end-to-end render):
    – "SEASONAL DRIFT" — sinusoidal temperature curve causing rising PSI in a dashboard panel.
    – "FLEET EXPANSION" — five new pumps P-15 … P-19 appearing on a fleet heatmap mid-demo.
    – "REAL FAILURE" — P-07 bearing-wear curve climbing toward a red threshold; failure-probability sparkline ramping.
  • Final stamp at the bottom: "AWS STUDENT APPLICATION — PORTFOLIO READY".

Top-right corner: title block "ML OBSERVABILITY PIPELINE / TO DELIVER / REV. 2026-05-29 / SHEET 3 OF 3".
Bottom-left corner: legend "DASHED = NOT YET BUILT · AMBER = BLOCKING · $0 AWS CEILING".

Wide 16:9 composition. No photographic elements. Ghosted dashed linework for unbuilt blocks; solid linework only for legend and frame. No people.
```

---

## Tips for the generator

- If you use **Midjourney**, append `--ar 16:9 --style raw --stylize 150` to each prompt. Reduce stylize if the type starts to drift into illustrative shapes.
- If you use **DALL-E / GPT-Image**, drop the trailing parameters and ask for "wide 16:9, blueprint schematic, dark navy paper".
- For consistency across the three: generate sheet 1 first, then paste a thumbnail of sheet 1 as a style reference for sheets 2 and 3 (most generators support image-conditioning now). The "Coherent series, image N of 3" line in each prompt is for tools that don't.
- Image generators are weak at long monospace text. If labels come out garbled, regenerate or accept the abstract shapes and add the labels yourself in Figma/Slides.
