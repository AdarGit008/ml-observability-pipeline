"""Test fixtures for ``dashboards_adapter.handler``.

Mirrors the ``lambda_scorer/tests/conftest.py`` discipline (see its
module docstring for the long form):

- ``fresh_adapter`` reloads the handler INSIDE an active ``mock_aws``
  context so the module-level ``boto3.resource`` binds to moto, not a
  cached real client from a previous import.
- The autouse ``_aws_credentials_guard`` gives every test fake
  credentials + a pinned region so a future test that forgets
  ``fresh_adapter`` fails loudly at the auth layer instead of
  reaching a real account (2026-06-04 housekeeping posture).

The adapter has no SNS, no model artifact, and no reference JSON —
the fixture surface is just the ADR 0010 table plus a seeding helper.
"""

from __future__ import annotations

import importlib
import os
from decimal import Decimal
from typing import Any, Iterator

import boto3
import pytest
from moto import mock_aws


TABLE_NAME: str = "test_pump_hot_state"


@pytest.fixture(autouse=True)
def _aws_credentials_guard(monkeypatch) -> None:
    """Fake AWS credentials for EVERY test in this package."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def fresh_adapter(monkeypatch) -> Iterator[tuple]:
    """Yield ``(handler_module, table_resource)`` inside a moto context.

    Creates the ADR 0010 table, then reloads the handler so its
    module-level ``_DDB`` binds against moto. Default fleet size (15)
    applies unless the test monkeypatches ``FLEET_SIZE`` and reloads
    again itself.
    """
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("DDB_TABLE_NAME", TABLE_NAME)
    monkeypatch.delenv("FLEET_SIZE", raising=False)
    monkeypatch.delenv("DDB_ENDPOINT_URL", raising=False)

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="eu-central-1")
        table = ddb.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "pump_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pump_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.wait_until_exists()

        import dashboards_adapter.handler as handler_mod

        importlib.reload(handler_mod)
        yield handler_mod, table


def put_state_row(
    table: Any,
    pump_id: str,
    *,
    score: float = 0.05,
    psi: dict[str, float] | None = None,
    alert_flag: bool = False,
    last_alert_sent_at: str | None = None,
    latest_ts: str = "2026-06-04T12:00:00.000Z",
) -> None:
    """Seed one STATE row shaped exactly per ``_interfaces.md``.

    Floats go through ``Decimal(str(...))`` — the same conversion the
    scorer's ``_to_decimal`` performs — so the adapter's tests read
    the types production writes. ``last_alert_sent_at`` is OMITTED
    (not nulled) when ``None``, matching the ADR 0012 storage
    convention the adapter's null-mapping is tested against.
    """
    if psi is None:
        psi = {
            "vibration_amp": 0.02,
            "bearing_temp": 0.01,
            "motor_current": 0.03,
            "rpm": 0.02,
        }
    item: dict[str, Any] = {
        "pump_id": pump_id,
        "sk": "STATE",
        "latest_ts": latest_ts,
        "latest_score": Decimal(str(score)),
        "latest_psi": {k: Decimal(str(v)) for k, v in psi.items()},
        "alert_flag": alert_flag,
    }
    if last_alert_sent_at is not None:
        item["last_alert_sent_at"] = last_alert_sent_at
    table.put_item(Item=item)


def get_event(method: str = "GET") -> dict:
    """Minimal Function URL (payload v2.0) event."""
    return {"requestContext": {"http": {"method": method, "path": "/"}}}
