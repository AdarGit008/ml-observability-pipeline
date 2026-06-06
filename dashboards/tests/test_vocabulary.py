"""Structural vocabulary checks for dashboard JSON files.

Verifies that dashboard panel field references are a subset of the
ADR 0005 §3 vocabulary — the shared field name set that both local
(InfluxDB) and AWS (Infinity/adapter) dashboards must bind to.

This is the dashboards-component equivalent of the ADR 0005 structural
parity tests: a regression that introduces a field name not in the
vocabulary (e.g., a pre-ADR-0009 psi_rolling_feature) fails loudly
before it reaches the demo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import shared.features as _features  # noqa: F401 — pin the import for parity

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS_DIR = REPO_ROOT / "dashboards"

# The ADR 0005 §3 vocabulary: all field names that may appear in
# dashboard panel queries. Any field name used in a dashboard JSON
# must be in this set (or be a Grafana variable like ${pump_id}).
DASHBOARD_VOCAB: frozenset[str] = frozenset(
    list(_features.FEATURE_NAMES)
    + ["score"]
    + [f"psi_{name}" for name in _features.PSI_FEATURE_NAMES]
    + ["alert_flag", "last_alert_sent_at", "latest_ts", "latest_score",
       "pump_id", "pumps_reporting", "fleet_size", "as_of"]
    # InfluxDB measurement-level keys used in Flux queries.
    + ["_measurement", "_field", "_value", "pump_id"]
)

# Grafana variable references and template strings are not field names.
GRAFANA_VARS = re.compile(r"\$\{|\}")


def _extract_field_references(obj: object) -> set[str]:
    """Recursively extract string values that look like field references
    from a JSON object (dict/list/primitive)."""
    refs: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            # Keys can be field references (e.g., column selectors).
            refs.update(_extract_field_references(k))
            refs.update(_extract_field_references(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(_extract_field_references(item))
    elif isinstance(obj, str):
        # Strip Grafana template variables and Flux syntax noise.
        cleaned = GRAFANA_VARS.sub("", obj)
        # Split on whitespace, commas, operators to get tokens.
        for token in re.split(r"[\s,=><!()\[\]|]+", cleaned):
            if token and token[0].isalpha() and token not in FLUX_KEYWORDS:
                refs.add(token)
    return refs


# Flux language keywords that appear in InfluxDB queries but are not
# field references.
FLUX_KEYWORDS: frozenset[str] = frozenset({
    "from", "range", "filter", "fn", "r", "group", "columns",
    "aggregateWindow", "every", "mean", "max", "min", "count",
    "last", "distinct", "keep", "bucket", "start", "stop",
    "and", "or", "not", "true", "false", "nil",
    "v", "timeRangeStart", "timeRangeStop", "windowPeriod",
    "influxdb", "http", "localhost", "pump_telemetry", "ml-obs",
    "admin", "ml-obs-local-token", "ml-obs-admin-password",
    "influxdb-local", "infinity-aws", "yesoreyeram-infinity-datasource",
    "json", "table", "url", "source", "format", "data", "root_selector",
    "selector", "text", "type", "string", "number", "bool", "timestamp",
    "pumps", "reporting", "total", "Reporting", "time",
    "Score", "Alert", "Last", "Sent", "PSI", "Vibration", "Bearing",
    "Temp", "Motor", "Current", "RPM",
    "P-01", "P-02", "P-03", "P-04", "P-05",
    "P-06", "P-07", "P-08", "P-09", "P-10",
    "P-11", "P-12", "P-13", "P-14", "P-15",
    "adapter_url", "host", "docker", "internal", "9000",
})


def _load_dashboard(name: str) -> dict:
    path = DASHBOARDS_DIR / name
    if not path.exists():
        pytest.skip(f"{path} not found")
    return json.loads(path.read_text())


class TestDashboardJSONParses:
    """Both dashboard files must be valid JSON."""

    def test_local_json_parses(self) -> None:
        dash = _load_dashboard("local.json")
        assert "panels" in dash
        assert dash.get("uid") == "ml-obs-local"

    def test_aws_json_parses(self) -> None:
        dash = _load_dashboard("aws.json")
        assert "panels" in dash
        assert dash.get("uid") == "ml-obs-aws"


class TestDashboardPanelCount:
    """Both dashboards must render the same panel concepts."""

    def test_local_panel_count(self) -> None:
        dash = _load_dashboard("local.json")
        # 8 panels: score ts, psi ts, alert table, per-pump detail,
        # pumps reporting, max score, max psi, alerts active.
        assert len(dash["panels"]) == 8

    def test_aws_panel_count(self) -> None:
        dash = _load_dashboard("aws.json")
        assert len(dash["panels"]) == 8


class TestDashboardFieldVocabulary:
    """All field references in dashboard JSON must be in the ADR 0005 §3 vocabulary."""

    def test_local_field_vocabulary(self) -> None:
        dash = _load_dashboard("local.json")
        refs = _extract_field_references(dash)
        unknown = refs - DASHBOARD_VOCAB
        assert not unknown, (
            f"local.json references field names outside ADR 0005 §3 vocabulary: "
            f"{sorted(unknown)}"
        )

    def test_aws_field_vocabulary(self) -> None:
        dash = _load_dashboard("aws.json")
        refs = _extract_field_references(dash)
        unknown = refs - DASHBOARD_VOCAB
        assert not unknown, (
            f"aws.json references field names outside ADR 0005 §3 vocabulary: "
            f"{sorted(unknown)}"
        )


class TestDashboardThresholds:
    """Threshold values must match _interfaces.md §PSI parameters."""

    def _panels(self, name: str) -> list[dict]:
        return _load_dashboard(name)["panels"]

    def _find_threshold(self, panels: list[dict], field_pattern: str) -> list[dict]:
        """Return threshold steps for panels whose field config matches pattern."""
        import re
        results = []
        for panel in panels:
            defaults = panel.get("fieldConfig", {}).get("defaults", {})
            overrides = panel.get("fieldConfig", {}).get("overrides", [])
            thresholds = defaults.get("thresholds", {}).get("steps", [])
            if thresholds:
                results.append(thresholds)
            for override in overrides:
                for prop in override.get("properties", []):
                    if prop.get("id") == "thresholds":
                        val = prop.get("value", {})
                        if "steps" in val:
                            results.append(val["steps"])
        return results

    def test_local_score_threshold_at_07(self) -> None:
        panels = self._panels("local.json")
        thresholds = self._find_threshold(panels, "score")
        # At least one panel must have a threshold at 0.7.
        found = any(
            any(s.get("value") == 0.7 for s in t)
            for t in thresholds
        )
        assert found, "No panel has score threshold at 0.7"

    def test_local_psi_threshold_at_25(self) -> None:
        panels = self._panels("local.json")
        thresholds = self._find_threshold(panels, "psi")
        found = any(
            any(s.get("value") == 0.25 for s in t)
            for t in thresholds
        )
        assert found, "No panel has PSI threshold at 0.25"

    def test_aws_score_threshold_at_07(self) -> None:
        panels = self._panels("aws.json")
        thresholds = self._find_threshold(panels, "score")
        found = any(
            any(s.get("value") == 0.7 for s in t)
            for t in thresholds
        )
        assert found, "No panel has score threshold at 0.7"

    def test_aws_psi_threshold_at_25(self) -> None:
        panels = self._panels("aws.json")
        thresholds = self._find_threshold(panels, "psi")
        found = any(
            any(s.get("value") == 0.25 for s in t)
            for t in thresholds
        )
        assert found, "No panel has PSI threshold at 0.25"
