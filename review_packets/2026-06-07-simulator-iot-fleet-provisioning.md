# Review Packet 2026-06-07 — simulator — iot-fleet-provisioning

> Run via: `.\scripts\gemini_review.ps1 -Slug 2026-06-07-simulator-iot-fleet-provisioning`

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
Terraform now provisions the simulator fleet's AWS IoT identity end-to-end (new `infra/modules/iot_fleet`): 15 Things named bare `P-NN` (locked equal to MQTT client_id/pump_id per ADR 0003), 15 AWS-generated certificates (**private keys land in the local-only, gitignored tfstate — the session's headline custody decision, ADR 0016**), ONE shared IoT policy scoped per-connection by `${iot:Connection.Thing.ThingName}`, and `local_sensitive_file` resources that write cert material under gitignored `simulator/.secrets/` (apply IS the cert pull; destroy deletes the files). Simulator side, the single `broker.tls` block gains a `{pump_id}` placeholder expanded per pump at `Fleet.from_config` time — closing the latent assumption that one cert serves the whole fleet, which the 2026-05-27 single-pump smoke never exposed. Teardown sweeps the new resources; `docs/runbooks/aws-demo-day.md` sequences demo day; ADR 0016 records all four decisions plus the verified IoT free-tier posture ($0 inside the account's 12 months, ~$0.018/demo after). Suite 411+1 → 427+1 (16 new tests). NOT committed yet — commit sequencing per `docs/next_session_brief.md` (dashboards #2 lands first).

## Diff
Full diff is in the working tree (uncommitted; reviewer sees files below). Changed surface:

**New:** `infra/modules/iot_fleet/{main,variables,outputs}.tf`, `simulator/tests/test_tls_per_pump.py` (16 tests), `docs/runbooks/aws-demo-day.md`, `docs/adr/0016-iot-fleet-provisioning-cert-custody.md`.
**Modified:** `infra/{main,outputs,variables,versions}.tf` (module wiring, `iot_policy_name` var, 3 outputs, `local`+`http` providers), `simulator/config.py` (+`PUMP_ID_PLACEHOLDER`, +`tls_for_pump`), `simulator/runner.py` (per-pump expansion in `_make_publisher` + shared-cert WARNING), `simulator/config.example.yaml` (comments only), `scripts/aws_teardown.sh` (iot-fleet sweep), `context/{simulator,infra}.md`.

Key excerpts:

```hcl
# infra/modules/iot_fleet/main.tf — the shared policy
resource "aws_iot_policy" "fleet" {
  name = var.policy_name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ConnectOnlyAsOwnThingName"
        Effect   = "Allow"
        Action   = ["iot:Connect"]
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:client/$${iot:Connection.Thing.ThingName}"
        Condition = { Bool = { "iot:Connection.Thing.IsAttached" = "true" } }
      },
      {
        Sid      = "PublishOnlyToOwnTelemetryTopic"
        Effect   = "Allow"
        Action   = ["iot:Publish"]
        Resource = "arn:aws:iot:${var.aws_region}:${data.aws_caller_identity.current.account_id}:topic/factory/pumps/$${iot:Connection.Thing.ThingName}/telemetry"
      }
    ]
  })
}
```

```python
# simulator/config.py
def tls_for_pump(tls: TlsConfig, pump_id: str) -> TlsConfig:
    return TlsConfig(
        cert_path=tls.cert_path.replace(PUMP_ID_PLACEHOLDER, pump_id),
        key_path=tls.key_path.replace(PUMP_ID_PLACEHOLDER, pump_id),
        ca_path=tls.ca_path.replace(PUMP_ID_PLACEHOLDER, pump_id),
    )

# simulator/runner.py — inside Fleet.from_config
def _make_publisher(pump_id: str) -> Publisher:
    tls = config.broker.tls
    if tls is not None:
        tls = tls_for_pump(tls, pump_id)
    return make_publisher(target=config.broker.target, url=config.broker.url,
                          client_id=pump_id, tls=tls)
```

Plus a `log.warning` in `from_config` when `pump_count > 1`, target aws-iot, and neither cert_path nor key_path carries the placeholder (all pumps would present ONE cert; the policy then denies CONNECT for N−1 pumps as *transient* errors → silent 30s-cap retry loop).

## Specific questions for the reviewer

1. **Policy completeness/correctness.** Does the two-statement thing-variable policy cover everything the simulator's QoS-0 publish path needs, and nothing more? Specifically: is the `IsAttached` condition on Connect correct (or does it accidentally block the legitimate path), and is any additional action (e.g. `iot:Receive`, `iot:RetainPublish`) needed for `aiomqtt` CONNECT/PUBLISH at QoS 0 with `retain=False`?
2. **Key-in-state custody (ADR 0016 §Decision 1).** The accepted-risk argument: local-only backend, gitignored state, git-secrets hook, demo-ephemeral lifecycle, CSR's marginal protection dominated by key files on the same disk. Anything rationalized past? (e.g. `terraform.tfstate.backup`, crash logs, `terraform output`/`plan` leaking `private_key` to console or CI logs later.)
3. **Placeholder mechanism failure modes.** `str.replace` of a literal `{pump_id}` token, expansion at runner-construction (loader stays shape-only). Is the multi-pump-no-placeholder WARNING the right strength, or should it be a `PublisherConfigError` (halt-the-fleet)? The counter-argument for warning-only: a non-AWS mTLS broker may legitimately share certs.
4. **Teardown sweep posture.** Things/policy absence are FAIL; ACTIVE certificates are a count-based WARN (certs have no stable names; non-fleet certs shouldn't block teardown); leftover local `*.private.key` is WARN. Is WARN too soft for ACTIVE certs in a dedicated demo account given north star #1?
5. **`data "http"` root-CA fetch.** Plan/apply now depends on amazontrust.com reachability (postcondition on HTTP 200, content written via `local_file`). Versus vendoring the public CA in-repo (needs a gitignore negation through the blanket `*.pem` rule). Wrong call?
6. **Mode parity check.** The simulator remains outside the ADR 0005 parity set (imports no `shared/` code) and the local path is untouched (`tls is None` short-circuit). Any way this change leaks behavioral divergence between local and AWS modes beyond the transport layer it's supposed to touch?

## What I'm NOT looking for in this review
- Grafana dashboard JSON / dashboards #2 soak — separate in-flight session; its uncommitted files in the tree are NOT part of this diff.
- Style/formatting; terraform fmt runs PO-side.
- Whether IoT Core itself is the right transport — locked since PLAN.md §2.2 / ADR 0003.

## Resolution (filled in by Claude after the reviewer responds)

| Reviewer point | Disposition | Notes |
|---|---|---|
| 1. | REJECT explicit-Deny add (PO call 2026-06-07) | IoT policies are default-deny; no other policy attaches to these certs, so explicit Denys change nothing and add maintenance noise. Reviewer confirmed `iot:Receive`/`iot:RetainPublish` are NOT needed — policy unchanged. |
| 2. | REJECT remote-state/extra controls; risks already covered | `.gitignore` `*.tfstate.*` covers `.backup`; no TF output exposes `private_key` (endpoint/names/ARNs only); no CI exists. S3 backend contradicts ADR 0016's local-only/$0 decision. |
| 3. | ACCEPT-AS-DONE (documentation) | `{pump_id}` placeholder + shared-cert behavior documented in `config.example.yaml` comment block; templating-engine suggestion rejected — literal-token `str.replace` is deliberate (loader stays shape-only). |
| 4. | ACCEPT — strengthened WARN→FAIL (PO call 2026-06-07) | `aws_teardown.sh` now FAILs on any ACTIVE cert post-destroy; dedicated demo account ⇒ expected count is always 0. Header comment + ADR 0016 §Follow-ups + `context/infra.md` updated. |
| 5. | ACCEPT-AS-DONE (documentation) | amazontrust.com reachability dependency already documented in `docs/runbooks/aws-demo-day.md`; vendoring rejected (blanket `*.pem` gitignore + stale-CA maintenance). |
| 6. | ACCEPT — no change | Reviewer concurs simulator stays outside the ADR 0005 parity set; local path untouched (`tls is None` short-circuit) and pinned by the 16 new tests. |
