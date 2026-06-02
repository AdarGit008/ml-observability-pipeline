"""Test fixtures for ``lambda_scorer.handler``.

Provides ``fresh_handler``: a fixture that mocks DynamoDB via moto,
creates the per-ADR-0010 table, and re-imports the handler so its
module-level boto3 resource client binds to the moto-mocked client.

Why ``importlib.reload`` is mandatory: the handler binds
``_DDB = boto3.resource("dynamodb", ...)`` at module import time.
The FIRST test in a process gets a moto-mocked client only if
``@mock_aws`` is active before that import. Subsequent tests that
just ``import lambda_scorer.handler`` get the cached binding from
the first import, regardless of whether ``@mock_aws`` is active for
the current test. Reloading inside each fixture invocation forces
the binding to refresh against the current moto context.
"""

from __future__ import annotations

import importlib
from typing import Iterator

import boto3
import pytest
from moto import mock_aws


TABLE_NAME: str = "test_pump_hot_state"


@pytest.fixture
def fresh_handler(monkeypatch) -> Iterator[tuple]:
    """Yield (handler_module, table_resource) inside a moto context.

    Sets AWS_* env vars before entering ``mock_aws()`` so boto3
    doesn't try to use real credentials. Creates the table per
    ADR 0010's key schema before reloading the handler so the
    handler's module-level ``TABLE.query`` calls succeed.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("DDB_TABLE_NAME", TABLE_NAME)

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

        # Reload so the handler's module-level resource client picks
        # up the moto-mocked client + the test-set env vars.
        import lambda_scorer.handler as handler_mod
        importlib.reload(handler_mod)

        yield handler_mod, table
