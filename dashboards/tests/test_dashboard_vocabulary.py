"""Structural checks for the Grafana dashboard JSON pair.

`dashboards` is in the ADR 0005 parity set (DEV_NORMS §5 Tier 2b): the
panel-level field vocabulary IS the parity surface here. These tests are
the dashboard-side siblings of the structural parity tests in
`local_runtime/tests/test_service.py` (`test_structural_parity_no_vendoring`,
`test_structural_parity_score_loads_from_shared`,
`test_structural_parity_compute_psi_loads_from_shared`): instead of
pinning *import paths* into `shared/`, they pin the *names* panels query
to the vocabulary `shared.features` defines.

What's enforced:

1. Both JSON files parse — a malformed dashboard fails CI, not demo day.
2. Every `psi_*` token in either dashboard names a member of
   `shared.features.PSI_FEATURE_NAMES` (ADR 0009's four survivors).
   A panel against a retired rolling-feature PSI field
   (`psi_vibration_amp_mean_5m`, …) fails loudly here.
3. Local-mode Flux queries reference only ADR 0005 §3 field names.
4. AWS-mode Infinity column selectors reference only ADR 0014 wire
   contract keys.
5. Each dashboard references exactly its pinned datasource uid, and
   the provisioning YAML pins those uids (session-log decision,
   2026-06-04 dashboards #2).

The allowed sets are DERIVED from `shared.features` where possible —
not hand-copied — so a vocabulary change upstream propagates here
without a manual edit (or fails loudly if the dashboards lag).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from shared.features import PSI_FEATURE_NAMES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LOCAL_JSON = _REPO_ROOT / "dashboards" / "local.json"
_AWS_JSON = _REPO_ROOT / "dashboards" / "aws.json"
_DATASOURCES_YML = (
    _REPO_ROOT / "grafana" / "provisioning" / "datasources" / "datasources.yml"
)

# The four surviving PSI field names — ADR 0005 §3 spelling, derived
# from the parity boundary, never hand-listed (ADR 0009).
_PSI_FIELDS = {f"psi_{name}" for name in PSI_FEATURE_NAMES}

# ADR 0005 §3: fields local-mode panels may query. The 8 raw feature
# fields exist in InfluxDB but no current panel uses them; panels query
# the score + PSI surface only. Widen deliberately if a panel grows.
_LOCAL_QUERYABLE_FIELDS = {"score"} | _PSI_FIELDS

# ADR 0014 wire contract — envelope keys + per-pump keys
# (context/_interfaces.md §Grafana → DynamoDB adapter).
_ADAPTER_KEYS = {
    "fleet_size",
    "pumps_reporting",
    "pumps_pooled",  # FLEET object pooled-window count (ADR 0018; wire rename of the row attr)
    "as_of",
    "pump_id",
    "latest_ts",
    "latest_score",
    "alert_flag",
    "last_alert_sent_at",
} | _PSI_FIELDS

_PSI_TOKEN_RE = re.compile(r"psi_[a-z0-9_]+")
_FLUX_FIELD_RE = re.compile(r'r\._field == "([^"]+)"')


def _load(path: Path) -> tuple[dict, str]:
    raw = path.read_text(encoding="utf-8")
    return json.loads(raw), raw


def test_local_json_parses():
    dashboard, _ = _load(_LOCAL_JSON)
    assert dashboard["uid"] == "pump-fleet-local"
    assert dashboard["panels"], "local dashboard has no panels"


def test_aws_json_parses():
    dashboard, _ = _load(_AWS_JSON)
    assert dashboard["uid"] == "pump-fleet-aws"
    assert dashboard["panels"], "aws dashboard has no panels"


def test_psi_tokens_match_adr_0009_surface():
    """Every psi_* token in either file names one of the 4 survivors.

    This is the regression ADR 0009 §Negative warns about: a panel
    wired against a retired rolling-feature PSI field would render
    stale data silently. Token-level scan catches it anywhere in the
    JSON — queries, titles, selectors, overrides.
    """
    for path in (_LOCAL_JSON, _AWS_JSON):
        _, raw = _load(path)
        tokens = set(_PSI_TOKEN_RE.findall(raw))
        unexpected = tokens - _PSI_FIELDS
        assert not unexpected, (
            f"{path.name} references psi_* names outside "
            f"shared.features.PSI_FEATURE_NAMES (ADR 0009): {sorted(unexpected)}"
        )


def test_local_flux_fields_within_adr_0005_vocabulary():
    dashboard, _ = _load(_LOCAL_JSON)
    queries = [
        target["query"]
        for panel in dashboard["panels"]
        for target in panel.get("targets", [])
        if "query" in target
    ]
    fields = {
        field for query in queries for field in _FLUX_FIELD_RE.findall(query)
    }
    assert fields, "no Flux _field filters found in local.json"
    unexpected = fields - _LOCAL_QUERYABLE_FIELDS
    assert not unexpected, (
        "local.json Flux queries reference fields outside the ADR 0005 §3 "
        f"panel vocabulary: {sorted(unexpected)}"
    )


def test_aws_selectors_within_adr_0014_contract():
    dashboard, _ = _load(_AWS_JSON)
    selectors: set[str] = set()
    for panel in dashboard["panels"]:
        for target in panel.get("targets", []):
            for column in target.get("columns", []):
                selectors.add(column["selector"])
    assert selectors, "no Infinity column selectors found in aws.json"
    unexpected = selectors - _ADAPTER_KEYS
    assert not unexpected, (
        "aws.json Infinity selectors reference keys outside the ADR 0014 "
        f"wire contract: {sorted(unexpected)}"
    )


def test_datasource_uids_pinned_and_paired():
    """Each dashboard uses exactly its provisioned datasource uid."""
    provisioned = yaml.safe_load(_DATASOURCES_YML.read_text(encoding="utf-8"))
    uids = {ds["uid"] for ds in provisioned["datasources"]}
    assert {"influxdb-local", "infinity-aws"} <= uids

    for path, expected_uid in (
        (_LOCAL_JSON, "influxdb-local"),
        (_AWS_JSON, "infinity-aws"),
    ):
        dashboard, _ = _load(path)
        referenced = {
            panel["datasource"]["uid"]
            for panel in dashboard["panels"]
            if "datasource" in panel
        }
        assert referenced == {expected_uid}, (
            f"{path.name} must reference exactly its pinned datasource uid "
            f"{expected_uid!r}; found {sorted(referenced)}"
        )


def test_refresh_rate_is_demo_cadence():
    """5 s refresh on both dashboards (PO call, 2026-06-04 dashboards #2)."""
    for path in (_LOCAL_JSON, _AWS_JSON):
        dashboard, _ = _load(path)
        assert dashboard["refresh"] == "5s", path.name
