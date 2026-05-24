# dashboards

## Purpose
Single local Grafana instance with two datasources. Renders fleet health, per-pump detail, and drift panels. Switches between local InfluxDB and AWS DynamoDB-via-Lambda-URL via datasource selection — no panel rewrites.

## Current state
- [ ] Not started.
- Spec defined in `PLAN.md §2.9`.

## Interfaces (in / out)
- **In (local mode):** InfluxDB at `localhost:8086`.
- **In (AWS mode):** Lambda Function URL exposing a `/query` JSON API.
- **Out:** Three panels: fleet heatmap of P(failure_48h), per-pump filtered detail, drift PSI panel.

## Open questions
- Grafana ↔ DynamoDB adapter design (HANDOFF.md §6 Q1) — blocking. Three options on the table; Lambda Function URL + JSON datasource plugin is the default.
- Should the AWS-mode dashboard JSON live in the same Grafana instance (datasource toggle) or be a second JSON file? Default: second JSON file to avoid datasource-switching panel errors.

## Related ADRs
None yet. Likely: Grafana adapter choice.
