"""Tests for ``lambda_fleet_psi.handler``.

Coverage:
- Structural parity: ``compute_psi``, ``psi_alert_should_fire``,
  ``load_reference`` loaded from ``shared/`` (ADR 0005), mirroring the
  ``lambda_scorer`` guards. This Lambda is in the parity set because it
  imports ``compute_psi``; the guards pin that it shares ONE definition
  of fleet-drift-is-meaningful-and-breaching with the per-pump scorer.
- Cold-start: reference loads; FLEET_SIZE expands to P-01..P-NN;
  missing SNS_TOPIC_ARN fails fast (ADR 0012 posture).
- Pooling: readings across multiple pumps fold into ONE fleet PSI on
  the FLEET STATE row (ADR 0018); ``pumps_reporting`` counts the
  contributors.
- Healthy fleet (reference-distributed) → no alert.
- Drifting fleet (warm + breaching) → edge-triggered SNS publish once,
  no republish on a persisting breach (ADR 0012).
- Empty fleet → true no-op (no STATE row, no publish).
- Warmup gate (ADR 0017): a sub-``PSI_MIN_SAMPLES`` pooled window does
  NOT arm an alert even though its PSI breaches — and ``latest_psi`` is
  still written (gate is on the alert, not the value).
"""

from __future__ import annotations

import importlib
import inspect
import json
from pathlib import Path
from unittest import mock

import pytest

from shared.drift import PSI_SIGNIFICANT_THRESHOLD
from shared.features import PSI_FEATURE_NAMES
from lambda_fleet_psi.tests.conftest import (
    get_fleet_state,
    seed_pump_extreme,
    seed_pump_spanning,
)


# --- Structural-parity guards (mirror the lambda_scorer guards) ---

def _assert_loads_from_shared(func) -> None:
    func_file = Path(inspect.getfile(func)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()
    assert shared_dir in func_file.parents, (
        f"{func.__name__} is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


def test_structural_parity_compute_psi_loads_from_shared():
    import lambda_fleet_psi.handler as handler_mod
    _assert_loads_from_shared(handler_mod.compute_psi)


def test_structural_parity_psi_alert_should_fire_loads_from_shared():
    """The fleet Lambda must arm via the SAME shared decision as the
    per-pump scorer (ADR 0017 + 0018) — not a vendored fork — so the
    two AWS alert sites cannot diverge on the warmup boundary or the
    threshold (north star #6).
    """
    import lambda_fleet_psi.handler as handler_mod
    _assert_loads_from_shared(handler_mod.psi_alert_should_fire)


def test_structural_parity_load_reference_loads_from_shared():
    import lambda_fleet_psi.handler as handler_mod
    _assert_loads_from_shared(handler_mod.load_reference)


# --- Cold-start tests ---

def test_cold_start_reference_and_fleet_ids(fresh_fleet):
    """Reference loads with the ADR 0009 4-feature surface; FLEET_SIZE
    expands to the two-digit P-01..P-NN id list.
    """
    handler_mod, _ = fresh_fleet
    ref = handler_mod.REFERENCE
    assert isinstance(ref, dict)
    assert tuple(ref["feature_names"]) == PSI_FEATURE_NAMES
    assert handler_mod.FLEET_PUMP_IDS == tuple(f"P-{i:02d}" for i in range(1, 16))


def test_cold_start_missing_sns_topic_arn_raises(monkeypatch):
    """SNS_TOPIC_ARN is required — its absence fails the cold start
    (ADR 0012 fail-fast posture), not the first invocation.
    """
    monkeypatch.delenv("SNS_TOPIC_ARN", raising=False)
    import lambda_fleet_psi.handler as handler_mod
    with pytest.raises(KeyError):
        importlib.reload(handler_mod)


# --- Pooling / healthy fleet ---

def test_pool_across_pumps_writes_fleet_row(fresh_fleet):
    """Readings from several pumps fold into ONE fleet PSI on the FLEET
    STATE row; a reference-distributed (healthy) fleet stays under the
    threshold and publishes nothing.
    """
    handler_mod, table = fresh_fleet
    sns_stub = mock.MagicMock()
    handler_mod._SNS = sns_stub

    for pid in ("P-01", "P-02", "P-03"):
        seed_pump_spanning(table, pid, 60, handler_mod.REFERENCE)

    result = handler_mod.handler({})

    assert result["pumps_reporting"] == 3
    assert result["alert_flag"] is False
    sns_stub.publish.assert_not_called()

    state = get_fleet_state(table)
    assert state is not None
    assert state["pump_id"] == "FLEET"
    assert set(state["latest_psi"].keys()) == set(PSI_FEATURE_NAMES)
    assert int(state["pumps_reporting"]) == 3
    assert "last_alert_sent_at" not in state
    # Healthy (reference-distributed) pooled window reads STABLE.
    assert max(float(v) for v in state["latest_psi"].values()) < PSI_SIGNIFICANT_THRESHOLD


def test_empty_fleet_is_noop(fresh_fleet):
    """No reading rows on any pump → no STATE row, no publish."""
    handler_mod, table = fresh_fleet
    sns_stub = mock.MagicMock()
    handler_mod._SNS = sns_stub

    result = handler_mod.handler({})

    assert result["pumps_reporting"] == 0
    assert result["fleet_max_psi"] is None
    assert result["alert_flag"] is False
    sns_stub.publish.assert_not_called()
    assert get_fleet_state(table) is None


# --- Drifting fleet / edge-triggered alert ---

def test_drifting_fleet_publishes_once_on_edge(fresh_fleet):
    """A warm, breaching pooled window arms a fleet alert: one SNS
    publish on the False→True edge, scoped pump_id='FLEET',
    alert_type 'psi_breach'. A second invocation while still breached
    does NOT republish (ADR 0012 edge-trigger).
    """
    handler_mod, table = fresh_fleet
    sns_stub = mock.MagicMock()
    handler_mod._SNS = sns_stub

    # 2 pumps x 80 extreme = 160 pooled >= PSI_MIN_SAMPLES (warm) and
    # far out-of-distribution (breaching).
    seed_pump_extreme(table, "P-01", 80)
    seed_pump_extreme(table, "P-02", 80)

    first = handler_mod.handler({})
    second = handler_mod.handler({})

    assert first["alert_flag"] is True
    assert first["published"] is True
    assert second["alert_flag"] is True
    assert second["published"] is False
    assert sns_stub.publish.call_count == 1

    payload = json.loads(sns_stub.publish.call_args.kwargs["Message"])
    assert payload["pump_id"] == "FLEET"
    assert payload["alert_type"] == "psi_breach"
    assert payload["pumps_reporting"] == 2
    assert set(payload["psi"].keys()) == set(PSI_FEATURE_NAMES)
    assert payload["scope"] == "fleet"   # generic-subscriber filter (review §2)
    assert payload["score"] is None       # drift-only path carries a null score

    state = get_fleet_state(table)
    assert state["alert_flag"] is True
    assert "last_alert_sent_at" in state


def test_warmup_gate_blocks_sub_window_fleet_alert(fresh_fleet):
    """ADR 0017 warmup gate at fleet scale: a pooled window below
    PSI_MIN_SAMPLES does NOT arm, even though its PSI breaches. The
    FLEET row is still written with the breaching value — the gate is
    on the alert, not the computation.
    """
    handler_mod, table = fresh_fleet
    sns_stub = mock.MagicMock()
    handler_mod._SNS = sns_stub

    # One pump, 10 extreme readings = 10 pooled < PSI_MIN_SAMPLES (150).
    seed_pump_extreme(table, "P-01", 10)

    result = handler_mod.handler({})

    assert result["pumps_reporting"] == 1
    assert result["alert_flag"] is False
    sns_stub.publish.assert_not_called()

    state = get_fleet_state(table)
    assert state["alert_flag"] is False
    assert "last_alert_sent_at" not in state
    # PSI was still computed + stored and DOES breach.
    assert max(float(v) for v in state["latest_psi"].values()) > PSI_SIGNIFICANT_THRESHOLD
