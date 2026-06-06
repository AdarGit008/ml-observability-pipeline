# Monitoring Setup — ML Observability Pipeline

## Overview

The monitoring stack provides real-time visibility into model performance
and data drift for the simulated industrial pump fleet. It has two modes:

- **Local mode**: Grafana + InfluxDB in Docker. Zero AWS cost.
- **AWS mode**: Grafana + Infinity plugin → Lambda Function URL adapter → DynamoDB.

Both modes render the same panel concepts from a shared field vocabulary
(ADR 0005 §3), switched by datasource selection.

## Architecture

```
Local mode:
  Simulator → local_runtime → InfluxDB ← Grafana (InfluxDB datasource)

AWS mode:
  IoT Core → Lambda scorer → DynamoDB ← Lambda adapter → Grafana (Infinity datasource)
                                       ← SNS (alerts)
```

## Components

### Data Drift Detection (PSI)
- **Metric**: Population Stability Index (PSI) per feature
- **Features**: vibration_amp, bearing_temp, motor_current, rpm (4 raw signals)
- **Window**: Rolling 1-hour per pump (1800 samples at 2s tick)
- **Thresholds**: <0.1 stable, 0.1–0.25 warning, >0.25 significant
- **Implementation**: `shared/drift.py` — Laplace-smoothed, pure function
- **Cadence**: Every invocation (Lambda) / every 30 ticks (local, ~60s)

### Model Performance Monitoring
- **Metric**: P(failure_48h) ∈ [0, 1] per pump
- **Model**: HistGradientBoostingClassifier (<500 KB pickled)
- **Alert threshold**: score > 0.7
- **Implementation**: `shared/score.py` + `lambda_scorer/handler.py`

### Alerting
- **Mechanism**: Edge-triggered SNS publish (ADR 0012)
- **Trigger**: PSI > 0.25 OR score > 0.7 (False → True flip only)
- **State**: DynamoDB STATE row carries `alert_flag` + `last_alert_sent_at`
- **Dashboard**: Literal passthrough — no re-derived thresholds

## Dashboards

### Local Mode (`dashboards/local.json`)
- **Datasource**: InfluxDB v2 (Flux queries)
- **Panels**:
  - Fleet Health — Failure Probability (timeseries, per-pump)
  - Fleet PSI — Data Drift (timeseries, per-feature)
  - Alert State (table with color-coded scores)
  - Per-Pump Detail (score + PSI for selected pump)
  - Pumps Reporting (stat)
  - Max Fleet Score (gauge, threshold 0.7)
  - Max Fleet PSI (gauge, threshold 0.25)
  - Alerts Active (stat)

### AWS Mode (`dashboards/aws.json`)
- **Datasource**: Infinity plugin → Lambda Function URL adapter
- **Panels**: Same as local mode, reading from DynamoDB STATE rows
- **Adapter URL**: Set via Terraform output `adapter_function_url`

## Quick Start (Local)

```bash
# 1. Start infrastructure
docker compose up -d

# 2. Verify InfluxDB is ready
curl -s http://localhost:8086/health

# 3. Verify Grafana is ready (admin / ml-obs-admin-password)
curl -s http://localhost:3000/api/health

# 4. Start simulator
export INFLUX_TOKEN=ml-obs-local-token
python -m simulator &

# 5. Start scorer
python -m local_runtime

# 6. Open Grafana
# http://localhost:3000 → Dashboards → ML Observability → Local Mode
```

## Quick Start (AWS)

```bash
# 1. Apply infrastructure (includes Lambda adapter)
cd infra
terraform apply

# 2. Update Grafana Infinity datasource URL
# Set URL to: $(terraform output -raw adapter_function_url)

# 3. Open Grafana
# http://localhost:3000 → Dashboards → ML Observability → AWS Mode
```

## File Structure

```
dashboards/
  local.json          # Local-mode dashboard (InfluxDB)
  aws.json            # AWS-mode dashboard (Infinity/adapter)

infra/grafana/provisioning/
  datasources/
    datasources.yml   # InfluxDB + Infinity datasource provisioning
  dashboards/
    dashboards.yml    # Dashboard provisioning config

docker-compose.yml    # Grafana service added
```

## Field Vocabulary (ADR 0005 §3)

| Field | Type | Description |
|-------|------|-------------|
| `vibration_amp` | float | Latest vibration amplitude |
| `bearing_temp` | float | Latest bearing temperature |
| `motor_current` | float | Latest motor current draw |
| `rpm` | float | Latest RPM |
| `vibration_amp_mean_5m` | float | 5-min rolling mean vibration |
| `vibration_amp_std_5m` | float | 5-min rolling std vibration |
| `bearing_temp_mean_5m` | float | 5-min rolling mean bearing temp |
| `bearing_temp_std_5m` | float | 5-min rolling std bearing temp |
| `score` | float | P(failure_48h) |
| `psi_vibration_amp` | float | PSI for vibration_amp |
| `psi_bearing_temp` | float | PSI for bearing_temp |
| `psi_motor_current` | float | PSI for motor_current |
| `psi_rpm` | float | PSI for rpm |

## Related ADRs

- ADR 0005 §3 — InfluxDB field names
- ADR 0007 — PSI implementation and cadence
- ADR 0009 — PSI surface (4 features, not 8)
- ADR 0012 — Edge-triggered SNS alerts
- ADR 0014 — Grafana adapter API contract
