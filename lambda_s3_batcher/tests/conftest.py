"""Test fixtures for ``lambda_s3_batcher.handler``.

Mirrors the ``dashboards_adapter/tests/conftest.py`` discipline:

- ``fresh_batcher`` reloads the handler INSIDE an active ``mock_aws``
  context so the module-level boto3 clients bind to moto.
- The autouse ``_aws_credentials_guard`` gives every test fake
  credentials + a pinned region so a test that forgets
  ``fresh_batcher`` fails loudly at the auth layer.

The batcher's fixture surface is the ADR 0010 table plus an S3
bucket; seeding helpers write reading rows shaped exactly per
``_interfaces.md §DynamoDB schema``.
"""

from __future__ import annotations

import importlib
import io
from decimal import Decimal
from typing import Any, Iterator

import boto3
import pyarrow.parquet as pq
import pytest
from moto import mock_aws


TABLE_NAME: str = "test_pump_hot_state"
BUCKET_NAME: str = "test-pump-archive"


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
def fresh_batcher(monkeypatch) -> Iterator[tuple]:
    """Yield ``(handler_module, table_resource, s3_client)`` in moto.

    Creates the ADR 0010 table + the archive bucket, then reloads the
    handler so its module-level clients bind against moto. Defaults
    apply (FLEET_SIZE=15, SAFETY_LAG_SECONDS=5) unless a test
    monkeypatches and reloads again itself.
    """
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("DDB_TABLE_NAME", TABLE_NAME)
    monkeypatch.setenv("S3_BUCKET", BUCKET_NAME)
    monkeypatch.delenv("FLEET_SIZE", raising=False)
    monkeypatch.delenv("SAFETY_LAG_SECONDS", raising=False)
    monkeypatch.delenv("DDB_ENDPOINT_URL", raising=False)
    monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)

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

        s3 = boto3.client("s3", region_name="eu-central-1")
        s3.create_bucket(
            Bucket=BUCKET_NAME,
            CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
        )

        import lambda_s3_batcher.handler as handler_mod

        importlib.reload(handler_mod)
        yield handler_mod, table, s3


def put_reading_row(
    table: Any,
    pump_id: str,
    ts: str,
    *,
    vibration_amp: float = 0.42,
    bearing_temp: float = 68.3,
    motor_current: float = 4.7,
    rpm: float = 1798.0,
    score: float = 0.05,
) -> None:
    """Seed one reading row shaped exactly per ``_interfaces.md``.

    Floats go through ``Decimal(str(...))`` — the conversion the
    scorer performs — so the batcher's tests read the types
    production writes.
    """
    table.put_item(
        Item={
            "pump_id": pump_id,
            "sk": ts,
            "vibration_amp": Decimal(str(vibration_amp)),
            "bearing_temp": Decimal(str(bearing_temp)),
            "motor_current": Decimal(str(motor_current)),
            "rpm": Decimal(str(rpm)),
            "score": Decimal(str(score)),
        }
    )


def list_archive_keys(s3: Any) -> list[str]:
    """All object keys in the archive bucket, sorted."""
    resp = s3.list_objects_v2(Bucket=BUCKET_NAME)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def read_parquet(s3: Any, key: str):
    """Round-trip read-back: fetch ``key`` and parse it AS PARQUET.

    Asserting on this table (not just the put call) is the test that
    the bytes in S3 are a real Parquet file Athena/Glue can read.
    """
    body = s3.get_object(Bucket=BUCKET_NAME, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body))
