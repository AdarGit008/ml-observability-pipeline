"""Tests for ``lambda_scorer.handler``.

Coverage:
- Structural parity: ``extract_features``, ``score``, ``load_reference``
  loaded from ``shared/``, not vendored. Mirrors the three
  ``local_runtime/tests/test_service.py::test_structural_parity_*``
  guards per ADR 0005's parity-vendoring guard pattern.
- Cold-start happy path: reference loads cleanly, model_version
  matches model.pkl (the committed artifacts agree).
- Cold-start version mismatch: a forged reference with a different
  ``model_version`` triggers ``DriftError`` on module reload.
- Hot path on a cold pump: empty DynamoDB → window of 1 → score →
  reading row + STATE row both land.
- Hot path on a warm pump: DynamoDB has prior readings → window
  query returns them in newest-first order → handler reverses to
  oldest-first → ``extract_features`` rolling stats consume the
  full window → score lands.
- Hot path STATE-row filtering: STATE row exists in the partition
  but the score-path query excludes it via ``sk begins_with "2"``.
- Hot path malformed event: missing ``pump_id`` or missing telemetry
  field raises ``EventParseError``.

The moto-backed tests use the ``fresh_handler`` fixture defined in
``conftest.py``.
"""

from __future__ import annotations

import importlib
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest
from boto3.dynamodb.conditions import Key

from shared.drift import DriftError
from shared.features import PSI_FEATURE_NAMES


# --- Helpers ---

def _telemetry(pump_id: str = "P-07", ts: str = "2026-06-02T14:32:01.123Z",
               vibration_amp: float = 0.42, bearing_temp: float = 68.3,
               motor_current: float = 4.7, rpm: float = 1798.0) -> dict:
    """One synthetic telemetry payload matching the IoT Rule envelope.

    Defaults are pulled straight from
    ``context/_interfaces.md §Telemetry payload`` so tests read like
    documentation.
    """
    return {
        "pump_id": pump_id,
        "ts": ts,
        "vibration_amp": vibration_amp,
        "bearing_temp": bearing_temp,
        "motor_current": motor_current,
        "rpm": rpm,
    }


# --- Structural-parity tests (mirror the three local_runtime guards) ---

def test_structural_parity_extract_features_loads_from_shared():
    """``lambda_scorer.handler.extract_features`` must physically
    resolve to ``shared/features.py``, not a vendored copy under
    ``lambda_scorer/``. Mirrors
    ``local_runtime/tests/test_service.py::test_structural_parity_no_vendoring``
    per ADR 0005.
    """
    import lambda_scorer.handler as handler_mod

    func_file = Path(inspect.getfile(handler_mod.extract_features)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"extract_features is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


def test_structural_parity_score_loads_from_shared():
    """``lambda_scorer.handler.score_fn`` must resolve to
    ``shared/score.py``.
    """
    import lambda_scorer.handler as handler_mod

    func_file = Path(inspect.getfile(handler_mod.score_fn)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"score is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


def test_structural_parity_load_reference_loads_from_shared():
    """``lambda_scorer.handler.load_reference`` must resolve to
    ``shared/drift.py``.
    """
    import lambda_scorer.handler as handler_mod

    func_file = Path(inspect.getfile(handler_mod.load_reference)).resolve()
    repo_root = Path(__file__).resolve().parent.parent.parent
    shared_dir = (repo_root / "shared").resolve()

    assert shared_dir in func_file.parents, (
        f"load_reference is not loaded from shared/! "
        f"Loaded from: {func_file}; expected under: {shared_dir}"
    )


# --- Cold-start tests ---

def test_cold_start_reference_loaded():
    """Module import succeeds; ``REFERENCE`` is a dict carrying the
    expected ADR 0009 4-feature PSI surface and an ADR 0007
    ``model_version`` field.
    """
    import lambda_scorer.handler as handler_mod
    importlib.reload(handler_mod)

    ref = handler_mod.REFERENCE
    assert isinstance(ref, dict)
    assert "features" in ref
    assert "model_version" in ref
    assert tuple(ref["feature_names"]) == PSI_FEATURE_NAMES


def test_cold_start_version_mismatch_raises_drift_error(monkeypatch, tmp_path):
    """A reference JSON whose ``model_version`` doesn't match
    ``model.pkl``'s causes ``load_reference()`` to raise — which
    propagates out of the handler's module-level eager-load on
    reload. Cold-start fails fast; the Lambda would never come up.
    """
    bad_ref_path = tmp_path / "bad_reference.json"
    bad_ref_path.write_text(json.dumps({
        "model_version": "v9.9.9-deliberately-mismatched",
        "feature_names": list(PSI_FEATURE_NAMES),
        "n_bins": 2,
        "features": {
            name: {"bin_edges": [0.0, 0.5, 1.0], "bin_counts": [1, 1]}
            for name in PSI_FEATURE_NAMES
        },
    }))
    monkeypatch.setattr("shared.drift._DEFAULT_REF_PATH", bad_ref_path)

    import lambda_scorer.handler as handler_mod
    with pytest.raises(DriftError, match="version mismatch"):
        importlib.reload(handler_mod)

    # Restore a clean module state for downstream tests.
    monkeypatch.undo()
    importlib.reload(handler_mod)


# --- Hot-path tests ---

def test_handler_cold_pump_writes_reading_and_state(fresh_handler):
    """First message for a pump: DynamoDB is empty, so the window
    becomes ``[new_reading]`` (1 element). Verify the reading row +
    STATE row both land.
    """
    handler_mod, table = fresh_handler
    payload = _telemetry()

    result = handler_mod.handler(payload)

    assert result["pump_id"] == "P-07"
    assert result["ts"] == payload["ts"]
    assert 0.0 <= result["score"] <= 1.0

    response = table.query(
        KeyConditionExpression=Key("pump_id").eq("P-07"),
    )
    items = {item["sk"]: item for item in response["Items"]}
    # Reading row keyed by ISO ts; STATE row keyed by "STATE".
    assert payload["ts"] in items
    assert "STATE" in items
    assert len(items) == 2

    reading = items[payload["ts"]]
    assert reading["vibration_amp"] == Decimal(str(payload["vibration_amp"]))
    assert reading["bearing_temp"] == Decimal(str(payload["bearing_temp"]))
    assert reading["motor_current"] == Decimal(str(payload["motor_current"]))
    assert reading["rpm"] == Decimal(str(payload["rpm"]))
    assert "score" in reading

    state = items["STATE"]
    assert state["latest_ts"] == payload["ts"]
    assert "latest_score" in state
    # STATE row's latest_score == reading row's score for this invocation.
    assert state["latest_score"] == reading["score"]


def test_handler_warm_pump_reads_prior_readings(fresh_handler):
    """Seed DynamoDB with 5 prior readings, then invoke. Verify the
    query returns the seeded rows (the handler reverses them to
    oldest-first internally; the rolling stats from
    ``extract_features`` see the full 6-element window).
    """
    handler_mod, table = fresh_handler

    # Seed 5 prior readings with increasing timestamps + slowly
    # varying telemetry. ``Decimal(str(value))`` matches the
    # handler's own write path so the comparison post-read is clean.
    base_ts = "2026-06-02T14:30:0"
    for i in range(5):
        ts = f"{base_ts}{i}.000Z"
        table.put_item(Item={
            "pump_id": "P-07",
            "sk": ts,
            "vibration_amp": Decimal(str(0.40 + i * 0.01)),
            "bearing_temp": Decimal(str(68.0 + i * 0.1)),
            "motor_current": Decimal(str(4.5 + i * 0.05)),
            "rpm": Decimal(str(1800.0 - i)),
            "score": Decimal("0.05"),
        })

    payload = _telemetry(ts="2026-06-02T14:30:06.000Z",
                        vibration_amp=0.46, bearing_temp=68.6,
                        motor_current=4.78, rpm=1794.0)

    result = handler_mod.handler(payload)
    assert 0.0 <= result["score"] <= 1.0

    response = table.query(
        KeyConditionExpression=Key("pump_id").eq("P-07"),
    )
    items = response["Items"]
    # 5 seeded + 1 new reading + 1 STATE row = 7.
    assert len(items) == 7
    # STATE row latest_ts is the most recent invocation.
    state = next(item for item in items if item["sk"] == "STATE")
    assert state["latest_ts"] == payload["ts"]


def test_handler_window_query_excludes_state_row(fresh_handler):
    """A pre-existing STATE row in the partition must NOT enter the
    window the score path reads. The ``sk begins_with "2"`` predicate
    filters it out (ISO-8601 ts starts with year digit; "STATE"
    starts with S).

    Failure mode this guards against: if a future change drops the
    ``begins_with`` filter, the STATE row's attributes
    (``latest_ts``, ``latest_score``) would land in the window and
    ``extract_features`` would raise KeyError on a missing
    raw-signal field — surfacing the bug loudly, but only AFTER a
    pump has at least one STATE row. This test pins the filter
    behaviour before the bug can happen.
    """
    handler_mod, table = fresh_handler

    # Seed a STATE row directly (no reading rows in the partition).
    table.put_item(Item={
        "pump_id": "P-07",
        "sk": "STATE",
        "latest_ts": "2026-06-02T14:00:00.000Z",
        "latest_score": Decimal("0.42"),
    })

    payload = _telemetry()

    # If STATE row leaked into the window, extract_features would
    # raise KeyError (STATE row has no raw-signal fields). Successful
    # invocation is the assertion.
    result = handler_mod.handler(payload)
    assert 0.0 <= result["score"] <= 1.0

    # Verify the STATE row was overwritten (not duplicated) by this
    # invocation.
    response = table.query(
        KeyConditionExpression=Key("pump_id").eq("P-07")
        & Key("sk").eq("STATE"),
    )
    state_items = response["Items"]
    assert len(state_items) == 1
    assert state_items[0]["latest_ts"] == payload["ts"]


def test_handler_missing_pump_id_raises(fresh_handler):
    """Malformed event: missing ``pump_id`` raises ``EventParseError``."""
    handler_mod, _ = fresh_handler
    payload = _telemetry()
    del payload["pump_id"]

    with pytest.raises(handler_mod.EventParseError, match="pump_id"):
        handler_mod.handler(payload)


def test_handler_missing_telemetry_field_raises(fresh_handler):
    """Malformed event: missing a raw-signal field raises
    ``EventParseError``. Verifies the parse step rejects before
    DynamoDB or scoring run — caller gets a clear error from the
    first failed validation, not a downstream KeyError.
    """
    handler_mod, _ = fresh_handler
    payload = _telemetry()
    del payload["bearing_temp"]

    with pytest.raises(handler_mod.EventParseError, match="bearing_temp"):
        handler_mod.handler(payload)


def test_state_sk_outside_year_range_filter():
    """Pin the ADR 0010 reserved-SK convention: ``STATE_SK`` must not
    start with the digit ``"2"`` so the score-path window query's
    ``begins_with("2")`` predicate cleanly excludes it. This guards
    against a future change that renames the STATE row's sort-key
    literal to something that starts with "2" (e.g., "2tate" — yes
    that's contrived, but the regression would be silent: the STATE
    row would silently enter the window, ``extract_features`` would
    raise KeyError on a missing raw-signal field, and the failure
    surface would be "scoring breaks on every pump that's been
    invoked at least once" rather than "the STATE-row filter is
    wrong."

    Also pins the broader convention: ANY future reserved-SK row
    (``"META"``, etc.) must not start with "2". The handler's
    docstring §Year-2xxx assumption documents this.

    Groq review 2026-06-02 Q2 raised the millennium-digit fragility;
    this test makes the convention enforceable.
    """
    import lambda_scorer.handler as handler_mod

    assert not handler_mod.STATE_SK.startswith("2"), (
        f"STATE_SK={handler_mod.STATE_SK!r} starts with '2'; "
        f"the score-path window query's `begins_with('2')` predicate "
        f"would NOT exclude it, and the STATE row would leak into "
        f"the window the handler hands to extract_features."
    )

