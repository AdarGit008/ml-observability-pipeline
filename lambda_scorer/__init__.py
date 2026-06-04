"""AWS-mode hot-path scorer — IoT Rule → Lambda → DynamoDB → SNS.

Counterpart to ``local_runtime``. Same scoring + drift logic (mode
parity per ADR 0005), different runtime: one invocation per MQTT
message instead of a long-running subscriber loop, DynamoDB instead
of an in-memory deque for the per-pump rolling window.

MVP scope (2026-06-02 session): cold-start (model + reference load,
version-match validation per ADR 0007) + per-pump score path
(parse → query window from DynamoDB → extract → score → write
reading row + STATE row per ADR 0010). PSI compute + SNS publish are
deferred to a follow-on session; the reference is loaded at
cold-start so the follow-on plugs PSI in without re-touching
cold-start.

Entry point: ``lambda_scorer.handler.handler`` — wired to the IoT
Rule trigger in the IaC session.
"""
