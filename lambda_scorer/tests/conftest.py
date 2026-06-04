"""Test fixtures for ``lambda_scorer.handler``.

Provides ``fresh_handler``: a fixture that mocks DynamoDB + SNS via
moto, creates the per-ADR-0010 table and the alert topic, and
re-imports the handler so its module-level boto3 clients bind to the
moto-mocked clients.

Why ``importlib.reload`` is mandatory: the handler binds
``_DDB = boto3.resource("dynamodb", ...)`` (and ``_SNS``) at module
import time. The FIRST test in a process gets a moto-mocked client
only if ``@mock_aws`` is active before that import. Subsequent tests
that just ``import lambda_scorer.handler`` get the cached binding from
the first import, regardless of whether ``@mock_aws`` is active for
the current test. Reloading inside each fixture invocation forces
the binding to refresh against the current moto context.

Why the autouse ``_aws_credentials_guard`` (2026-06-04, MVP review Q5
disposition): the failure mode the reload discipline leaves open is a
FUTURE test that touches AWS but forgets ``fresh_handler`` — its
handler import binds the REAL boto3 client, and the mistake is silent
until a call goes over the wire. The guard closes that hole at the
credentials layer: every test in this package gets fake AWS
credentials + a pinned region, so an unmocked boto3 call fails loudly
with an auth/endpoint error instead of reaching a real account.
``AWS_EC2_METADATA_DISABLED`` stops botocore from probing the EC2
instance-metadata endpoint as a credentials fallback (a multi-second
hang in sandboxes). The guard makes forgetting ``fresh_handler``
LOUD, not impossible — moto-backed tests still need the fixture for
the reload + table/topic setup. Chosen over a marker + collection
check (stronger, but adds per-test ceremony) — see the 2026-06-04
housekeeping session log.

Why the module-level ``SNS_TOPIC_ARN`` setdefault: the PSI follow-on
made ``SNS_TOPIC_ARN`` a REQUIRED env var (``KeyError`` at cold-start
if unset — the production fail-fast posture). Tests that import or
reload the handler OUTSIDE the moto fixture (structural-parity tests,
the cold-start tests) would crash on that ``KeyError`` unless a value
is present. The setdefault below runs at conftest import — before any
test module in this package executes — so every handler import sees a
value. ``test_cold_start_missing_sns_topic_arn_raises_keyerror``
deletes it deliberately to pin the production behaviour.
"""

from __future__ import annotations

import importlib
import os
from typing import Iterator

import boto3
import pytest
from moto import mock_aws


TABLE_NAME: str = "test_pump_hot_state"

# Placeholder ARN for handler imports outside the moto fixture. The
# fixture overrides this with a real moto-created topic ARN. See the
# module docstring for why this must exist before any test runs.
os.environ.setdefault(
    "SNS_TOPIC_ARN",
    "arn:aws:sns:eu-central-1:000000000000:pump-alerts-placeholder",
)


@pytest.fixture(autouse=True)
def _aws_credentials_guard(monkeypatch) -> None:
    """Fake AWS credentials for EVERY test in this package.

    Safety net for the moto-reload discipline (module docstring): a
    test that forgets ``fresh_handler`` and lets the handler bind the
    real boto3 client fails loudly on these fake credentials instead
    of silently reaching a real AWS account. ``fresh_handler`` sets
    the same values; the overlap is deliberate and harmless.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


@pytest.fixture
def fresh_handler(monkeypatch) -> Iterator[tuple]:
    """Yield (handler_module, table_resource) inside a moto context.

    Sets AWS_* env vars before entering ``mock_aws()`` so boto3
    doesn't try to use real credentials. Creates the DynamoDB table
    per ADR 0010's key schema and the SNS alert topic before
    reloading the handler so the handler's module-level clients bind
    against live moto resources.
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

        # Alert topic per the PSI follow-on. The handler's cold-start
        # reads SNS_TOPIC_ARN (required); pointing it at a real
        # moto-created topic keeps un-stubbed publish paths honest.
        sns = boto3.client("sns", region_name="eu-central-1")
        topic_arn = sns.create_topic(Name="test-pump-alerts")["TopicArn"]
        monkeypatch.setenv("SNS_TOPIC_ARN", topic_arn)

        # Reload so the handler's module-level clients pick up the
        # moto-mocked clients + the test-set env vars.
        import lambda_scorer.handler as handler_mod
        importlib.reload(handler_mod)

        yield handler_mod, table
