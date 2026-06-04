# Review Packet 2026-06-04 — lambda_scorer / repo — housekeeping-test-quality

> Run via: `.\scripts\gemini_review.ps1 -Slug 2026-06-04-housekeeping-test-quality`

## Role for the reviewer model

You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

(Per ADR 0011, this packet may be reviewed by any model in the cascade: Gemini, DeepSeek R1 via OpenRouter, Llama 3.3 70B via Groq, or Llama 3.3 70B via Cerebras. The role is identical across providers; the response file's footer records which one actually wrote the response.)

## Project north stars (constraint anchors)

1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change

Housekeeping / test-quality session clearing the deferred items from the 2026-06-02 MVP + PSI follow-on reviews. No production code changed; no parity-boundary files touched. Four substantive deltas: (1) an autouse `_aws_credentials_guard` fixture in `lambda_scorer/tests/conftest.py` — every test in the package gets fake AWS credentials so a future moto-backed test that forgets the `fresh_handler` reload fails loudly instead of silently binding the real boto3 client (MVP review Q5 disposition); (2) `test_sns_publish_failure_is_loud_and_at_most_once` in `lambda_scorer/tests/test_handler.py` — pins ADR 0012's publish-after-write at-most-once ordering (publish raises → invocation errors AND the STATE row already carries `alert_flag=True` + `last_alert_sent_at`; the same-event retry does not re-publish); (3) a commit policy in `model/artifacts/README.md` — only PO-native 30-pump canonical builds are committed, sandbox builds never staged, no `.gitignore` (MVP review Q6 disposition, PO call); (4) the committed artifacts themselves regenerated PO-natively at 30 pumps (AUC 0.9978, `v0.1.0-seed-0`). Plus a stale-reference sweep across three context docs (future-tense "future `lambda_scorer`" fixed; resolved PSI parameters de-TBD'd). Suite: 368 → 369 passed + 1 skipped; all four structural-parity guards untouched and green.

## Diff

Changed files (test + doc + artifact only):

- `lambda_scorer/tests/conftest.py` — new autouse fixture + docstring section explaining the guard's scope (loud-not-impossible).
- `lambda_scorer/tests/test_handler.py` — new failure-path test + module-docstring coverage bullet.
- `model/artifacts/README.md` — new §Commit policy; intro paragraph now describes committed artifacts as the PO-native canonical build.
- `model/artifacts/model.pkl`, `model/artifacts/operational_reference_distribution.json` — PO-native 30-pump regen (binary + JSON; same `model_version` tag).
- `context/_global.md`, `context/local_runtime.md`, `context/_interfaces.md`, `context/lambda_scorer.md` — stale-reference sweep + test-count refresh.

Key new code, in full:

```python
# conftest.py — addition
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
```

```python
# test_handler.py — addition
def test_sns_publish_failure_is_loud_and_at_most_once(fresh_handler):
    """Pin ADR 0012 §Decision 3 / §Consequences: publish AFTER the
    STATE write, at-most-once per edge. [...]"""
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

    # Retry semantics: same event re-run, prev alert_flag == True —
    # no rising edge, no second publish. At-most-once.
    sns_stub.publish.side_effect = None
    retried = handler_mod.handler(_telemetry(ts=ts, **_EXTREME))
    assert retried["alert_flag"] is True
    assert sns_stub.publish.call_count == 1

    state = _get_state(table)
    assert state["alert_flag"] is True
    assert state["last_alert_sent_at"] == ts
```

`model/artifacts/README.md` §Commit policy (new, verbatim):

> **Only PO-native canonical builds get committed.** Rationale: committed artifacts keep a fresh clone green (`pytest` passes out of the box — the right out-of-box experience for a portfolio repo), but sandbox-built artifacts carry sklearn-version skew risk (MVP review Q6), so they are excluded from staging.
> - A session (sandbox or otherwise) that rebuilds `model.pkl` / `operational_reference_distribution.json` for validation purposes must NOT stage those files. Before the session's commit, the PO regenerates natively: `python -m model.train --n-pumps 30 --seed 0`.
> - The pre-commit `git diff --cached --name-status` check in the canonical staging sequence (DEV_NORMS §7) is the enforcement point: artifact paths in the staged set are only acceptable when the PO ran the regen that produced them.
> - No `.gitignore` entry — the files stay tracked; the policy governs *which build* of them gets staged.

## Specific questions for the reviewer

1. **Guard sufficiency (conftest):** the autouse credentials guard makes a forgotten `fresh_handler` *loud* (auth failure on any real AWS call) but not *impossible* — a marker + collection-time check was rejected as ceremony. At a 19-test file with one fixture, is loud-not-impossible the right stopping point, or is there a failure mode (e.g., a test asserting on handler module state without any AWS call, silently green against a stale real-client binding) that justifies the stronger mechanism?
2. **Failure-path test fidelity (ADR 0012):** does `test_sns_publish_failure_is_loud_and_at_most_once` actually pin the publish-after-write ordering? Specifically: is there any handler implementation with publish-BEFORE-write semantics that would still pass all four assertion groups? The retry leg re-runs the same event ts — is same-ts retry the right model for an IoT-Rule redelivery, or should a later-ts second event also be exercised?
3. **Commit policy enforcement:** the policy's enforcement point is a human reading `git diff --cached --name-status` output. Is that acceptable for a single-developer portfolio repo, or does it deserve a mechanical check (e.g., a pre-commit hook refusing artifact paths unless an env var is set)? The counter-argument: hook complexity for a two-file directory.
4. **sklearn version skew direction:** canonical `model.pkl` is now built on sklearn 1.9.0 (PO native); the sandbox validates on 1.7.2 and emits `InconsistentVersionWarning` while the suite stays green. Forward-unpickle (newer-built artifact read by older sklearn) is the riskier direction per sklearn's docs. Is suite-green sufficient validation evidence here, or should the sandbox pin sklearn >= the PO's version (cost: pip availability lag in the sandbox image)?

## What I'm NOT looking for in this review

- Production-code design — the handler, `shared/`, and the parity boundary are unchanged this session (Tier 2b loaded read-only).
- Style / formatting.
- The Item 5 doc sweep — judgment calls about historical-record vs living-doc are PO-ratified.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. Marker/collection check + `AWS_CREDENTIALS_OVERRIDE` escape hatch | Rejected | The suite is moto-only by hard constraint (no real AWS, north star #1) — no legitimate real-credential test exists, so an override hatch reopens the exact hole the guard closes. The marker + collection check was weighed and PO-rejected at plan-step (ceremony vs. a 19-test file). The "stale credentials silently passing" concern conflates state: a test making no AWS call cannot misuse a client binding. |
| 2. Two extra failure-path tests | Rejected | Suggested test #1 is a verbatim copy of the landed test (its docstring says publish-before-write but the body is identical — it pins nothing new; the landed test already FAILS under a publish-before-write handler via assertion (b)). Suggested test #2 asserts `publish.call_count == 2` + `last_alert_sent_at == later_ts` on a persisting breach — that is the duplicate alert ADR 0012's edge-trigger explicitly suppresses; the test as written would (correctly) fail. The later-ts persisting-breach path is already pinned by `test_sns_no_republish_when_still_breached`. |
| 3. Pre-commit hook blocking `model/artifacts/` paths | Rejected as-proposed | The suggested hook refuses ALL artifact commits — including the PO-native canonical builds the policy requires committing. A correct hook needs a bypass flag, which is the complexity the packet already weighed against a two-file directory in a single-dev repo. The DEV_NORMS §7 `git diff --cached --name-status` review step stands; revisit if a second contributor ever joins. |
| 4. Pin `scikit-learn==1.9.0` + CI version check | Partially accepted | Kernel accepted: forward-unpickle (older validation env reading newer-built artifact) is the riskier direction — now documented in `model/artifacts/README.md` §Commit policy with suite-green as the acceptance bar. Exact-pin rejected: `requirements.txt` is runtime scope (Lambda bundle ships its own deps); an exact pin adds fragility and the sandbox image lags pip. CI check deferred — no CI exists; belongs to a future dev_workflow/CI session. |
