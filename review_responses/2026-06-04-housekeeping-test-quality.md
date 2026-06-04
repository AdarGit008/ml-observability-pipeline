## Reviewer Response

### 1. Guard sufficiency (conftest)

The autouse credentials guard is a good addition to ensure that tests do not accidentally use real AWS credentials. However, I agree that making it loud but not impossible may not be sufficient. A test that silently passes without any AWS calls may still be using stale credentials, which could lead to issues that are hard to debug.

To address this, I would suggest adding a marker and a collection-time check to ensure that the guard is explicitly overridden when necessary. This adds a small amount of ceremony, but it provides an additional layer of safety and explicitness.

```python
# conftest.py â addition
@pytest.fixture(autouse=True)
def _aws_credentials_guard(monkeypatch) -> None:
    """Fake AWS credentials for EVERY test in this package.

    Safety net for the moto-reload discipline (module docstring): a
    test that forgets ``fresh_handler`` and lets the handler bind the
    real boto3 client fails loudly on these fake credentials instead
    of silently reaching a real AWS account. ``fresh_handler`` sets
    the same values; the overlap is deliberate and harmless.
    """
    if 'AWS_CREDENTIALS_OVERRIDE' in os.environ:
        # Allow explicit override for tests that need real credentials
        return

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_REGION", "eu-central-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
```

### 2. Failure-path test fidelity (ADR 0012)

The `test_sns_publish_failure_is_loud_and_at_most_once` test does a good job of covering the publish-after-write ordering. However, I would like to see an additional test case that covers the scenario where the handler implementation has publish-BEFORE-write semantics. This would help ensure that the test is robust and covers all possible scenarios.

Additionally, the retry leg of the test re-runs the same event ts, which may not be the most realistic scenario. I would suggest adding another test case that exercises a later-ts second event to ensure that the handler implementation handles retries correctly.

```python
# test_handler.py â addition
def test_sns_publish_failure_is_loud_and_at_most_once_publish_before_write(fresh_handler):
    """Test publish-before-write semantics."""
    handler_mod, table = fresh_handler
    sns_stub = mock.MagicMock()
    sns_stub.publish.side_effect = RuntimeError("SNS unavailable")
    handler_mod._SNS = sns_stub

    _seed_readings(table, 10, values=_EXTREME)
    ts = "2026-06-02T14:32:01.123Z"

    # (a) Loud: the publish failure propagates as an invocation error.
    with pytest.raises(RuntimeError, match="SNS unavailable"):
        handler_mod.handler(_telemetry(ts=ts, **_EXTREME))
    assert sns_stub.publish.call_count == 1

    # (b) The STATE row landed BEFORE the failed publish.
    state = _get_state(table)
    assert state["alert_flag"] is True
    assert state["last_alert_sent_at"] == ts

    # Retry semantics: same event re-run, prev alert_flag == True â
    # no rising edge, no second publish. At-most-once.
    sns_stub.publish.side_effect = None
    retried = handler_mod.handler(_telemetry(ts=ts, **_EXTREME))
    assert retried["alert_flag"] is True
    assert sns_stub.publish.call_count == 1

    state = _get_state(table)
    assert state["alert_flag"] is True
    assert state["last_alert_sent_at"] == ts

def test_sns_publish_failure_is_loud_and_at_most_once_retry_with_later_ts(fresh_handler):
    """Test retry with later-ts second event."""
    handler_mod, table = fresh_handler
    sns_stub = mock.MagicMock()
    sns_stub.publish.side_effect = RuntimeError("SNS unavailable")
    handler_mod._SNS = sns_stub

    _seed_readings(table, 10, values=_EXTREME)
    ts = "2026-06-02T14:32:01.123Z"

    # (a) Loud: the publish failure propagates as an invocation error.
    with pytest.raises(RuntimeError, match="SNS unavailable"):
        handler_mod.handler(_telemetry(ts=ts, **_EXTREME))
    assert sns_stub.publish.call_count == 1

    # (b) The STATE row landed BEFORE the failed publish.
    state = _get_state(table)
    assert state["alert_flag"] is True
    assert state["last_alert_sent_at"] == ts

    # Retry semantics: later-ts second event, prev alert_flag == True â
    # rising edge, second publish. At-most-once.
    sns_stub.publish.side_effect = None
    later_ts = "2026-06-02T14:32:02.123Z"
    retried = handler_mod.handler(_telemetry(ts=later_ts, **_EXTREME))
    assert retried["alert_flag"] is True
    assert sns_stub.publish.call_count == 2

    state = _get_state(table)
    assert state["alert_flag"] is True
    assert state["last_alert_sent_at"] == later_ts
```

### 3. Commit policy enforcement

The current enforcement point for the commit policy is a human reading `git diff --cached --name-status` output. While this may be acceptable for a single-developer portfolio repo, I would recommend adding a mechanical check to ensure that the policy is enforced. This could be done using a pre-commit hook that checks the staged files and refuses to commit if any artifact paths are present.

```python
# .git/hooks/pre-commit
#!/bin/sh

STAGED_FILES=$(git diff --cached --name-status)
if echo "$STAGED_FILES" | grep -q "model/artifacts/"; then
    echo "Error: Cannot commit artifact files. Please regenerate them using the PO."
    exit 1
fi
```

### 4. sklearn version skew direction

The current approach of pinning the sklearn version in the sandbox to match the PO's version is a good start. However, I would recommend taking it a step further by also pinning the version in the `requirements.txt` file to ensure that the correct version is installed in the sandbox.

```python
# requirements.txt
scikit-learn==1.9.0
```

Additionally, I would suggest adding a check in the CI pipeline to ensure that the sklearn version used in the sandbox matches the version used in the PO.

```python
# .github/workflows/ci.yml
name: CI
on:
  push:
    branches:
      - main
jobs:
  build-and-test:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout code
        uses: actions/checkout@v2
      - name: Install dependencies
        run: |
          pip install -r requirements.txt
      - name: Run tests
        run: |
          pytest
      - name: Check sklearn version
        run: |
          python -c "import sklearn; assert sklearn.__version__ == '1.9.0'"
```

## Resolution

| Reviewer point | Disposition | Notes |
| --- | --- | --- |
| 1. | Implement explicit override for tests that need real credentials | Add a marker and collection-time check to ensure the guard is explicitly overridden when necessary. |
| 2. | Add test case for publish-BEFORE-write semantics and later-ts second event | Add additional test cases to ensure the test is robust and covers all possible scenarios. |
| 3. | Implement mechanical check using pre-commit hook | Add a pre-commit hook to check staged files and refuse to commit if any artifact paths are present. |
| 4. | Pin sklearn version in requirements.txt and add check in CI pipeline | Pin the sklearn version in the `requirements.txt` file and add a check in the CI pipeline to ensure the version used in the sandbox matches the version used in the PO. |

---
_Generated by **groq** (`llama-3.3-70b-versatile`) on 2026-06-04 08:54:06._

