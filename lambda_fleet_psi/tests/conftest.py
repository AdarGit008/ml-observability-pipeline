"""Test fixtures for ``lambda_fleet_psi.handler``.

Mirrors the ``lambda_scorer`` / ``lambda_s3_batcher`` conftest
discipline:

- ``fresh_fleet`` reloads the handler INSIDE an active ``mock_aws``
  context so the module-level boto3 clients bind to moto. The handler
  needs both the ADR 0010 table AND an SNS topic (it edge-triggers
  alerts), so the fixture creates both — same shape as the scorer's
  ``fresh_handler``.
- The autouse ``_aws_credentials_guard`` gives every test fake
  credentials + a pinned region so a test that forgets ``fresh_fleet``
  fails loudly at the auth layer rather than reaching a real account.
- The module-level ``SNS_TOPIC_ARN`` setdefault lets handler imports
  OUTSIDE the moto fixture (structural-parity + cold-start tests)
  survive the required-env-var ``KeyError``.

Seeding helpers write reading rows shaped exactly per
``_interfaces.md §DynamoDB schema`` (``Decimal(str(...))`` — the
conversion the scorer performs). ``seed_pump_spanning`` cycles the
operational reference's own bin midpoints so a pump reads PSI ≈ 0
(distributed LIKE the reference); ``seed_pump_extreme`` writes
out-of-range values that clip into one bin and drive PSI >> 0.25 —
the same mechanics the scorer tests rely on.
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

# Placeholder ARN for handler imports outside the moto fixture (the
# fixture overrides it with a real moto-created topic). Must exist
# before any test runs because SNS_TOPIC_ARN is required at cold start.
os.environ.setdefault(
    "SNS_TOPIC_ARN",
    "arn:aws:sns:eu-central-1:000000000000:pump-alerts-placeholder",
)

# Telemetry far outside the operational reference's training-time
# min/max on every PSI feature — clips into the outermost bins,
# concentrating mass and driving PSI >> 0.25 (same values the scorer
# tests use).
_EXTREME = {
    "vibration_amp": 50.0,
    "bearing_temp": 250.0,
    "motor_current": 80.0,
    "rpm": 400.0,
}


@pytest.fixture(autouse=True)
def _aws_credentials_guard(monkeypatch) -> None:
    """Fake AWS credentials for EVERY test in this package."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def fresh_fleet(monkeypatch) -> Iterator[tuple]:
    """Yield ``(handler_module, table_resource)`` inside a moto context.

    Creates the ADR 0010 table + the SNS alert topic, then reloads the
    handler so its module-level clients bind against moto. Defaults
    apply (FLEET_SIZE=15) unless a test monkeypatches + reloads.
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

        sns = boto3.client("sns", region_name="eu-central-1")
        topic_arn = sns.create_topic(Name="test-pump-alerts")["TopicArn"]
        monkeypatch.setenv("SNS_TOPIC_ARN", topic_arn)

        import lambda_fleet_psi.handler as handler_mod

        importlib.reload(handler_mod)
        yield handler_mod, table


def _put_reading(table: Any, pump_id: str, ts: str, vals: dict) -> None:
    table.put_item(
        Item={
            "pump_id": pump_id,
            "sk": ts,
            "vibration_amp": Decimal(str(vals["vibration_amp"])),
            "bearing_temp": Decimal(str(vals["bearing_temp"])),
            "motor_current": Decimal(str(vals["motor_current"])),
            "rpm": Decimal(str(vals["rpm"])),
            "score": Decimal("0.05"),
        }
    )


def seed_pump_extreme(table: Any, pump_id: str, n: int) -> None:
    """Seed ``n`` out-of-range reading rows for one pump (drives PSI >> 0.25)."""
    for i in range(n):
        ts = f"2026-06-02T14:{10 + i // 60:02d}:{i % 60:02d}.000Z"
        _put_reading(table, pump_id, ts, _EXTREME)


def seed_pump_spanning(table: Any, pump_id: str, n: int, reference: dict) -> None:
    """Seed ``n`` reading rows whose PSI-feature values cycle the
    operational reference's bin midpoints — a window distributed LIKE
    the reference (PSI ≈ 0), the healthy-fleet case.
    """
    feats = reference["features"]
    from shared.features import PSI_FEATURE_NAMES

    for i in range(n):
        ts = f"2026-06-02T14:{10 + i // 60:02d}:{i % 60:02d}.000Z"
        vals: dict = {}
        for name in PSI_FEATURE_NAMES:
            edges = feats[name]["bin_edges"]
            b = i % (len(edges) - 1)
            vals[name] = (float(edges[b]) + float(edges[b + 1])) / 2.0
        _put_reading(table, pump_id, ts, vals)


def get_fleet_state(table: Any) -> dict | None:
    """Fetch the FLEET STATE row (or None if it was never written)."""
    return table.get_item(
        Key={"pump_id": "FLEET", "sk": "STATE"}
    ).get("Item")
