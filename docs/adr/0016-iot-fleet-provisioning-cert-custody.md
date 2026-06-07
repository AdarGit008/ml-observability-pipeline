# ADR 0016 — IoT Fleet Provisioning: Terraform-Generated Certs, Shared Thing-Variable Policy, Per-Pump Config Templating

- **Status:** Accepted (PO sign-off 2026-06-07; reviewer-cascade review pending)
- **Date:** 2026-06-07
- **Deciders:** PO (Adar), Claude (architect), reviewer cascade (pending)

## Principle (plain English)

**The fleet's identity is demo-ephemeral, so its custody can be too.**
`terraform apply` mints 15 IoT Things, 15 certificates, and one policy;
the private keys land in the local-only, gitignored Terraform state and
are written to a gitignored cert directory the simulator reads from.
`terraform destroy` (wrapped by `aws_teardown.sh`) deletes all of it —
cloud side and disk side. Nothing about this identity outlives a demo,
so the threat model is "what can leak from one gitignored directory on
one PC during one afternoon", not "how do we rotate production device
credentials". This ADR records that trade explicitly, because
key-in-state is the kind of decision that looks careless unless the
lifecycle that justifies it is written down next to it.

## Context

The hot path (IoT Rule → scorer → DynamoDB → adapter → Grafana) merged
2026-06-04, but the simulator can only reach it through the one
Console-provisioned Thing (`P-00`) from the 2026-05-27 smoke test. ADR
0003 locked the connection topology — one Thing = one client_id = one
pump, `client_id == pump_id` — and the `AwsIotPublisher` is wired and
tested. What's missing is fleet-scale provisioning: 15 Things, 15
certs, a policy, and a way for the simulator's config to name 15
different cert paths when today's schema holds exactly one `broker.tls`
block (a latent single-pump assumption the 2026-05-27 smoke never
exposed).

Four decisions fall out, settled with the PO via session walkthrough
(2026-06-07):

1. How certificates are minted (and where private keys live).
2. Policy topology and Thing naming.
3. Where cert/key material lands on disk.
4. How `config.yaml` expresses per-pump identity.

Anchors: $0 posture with honest free-tier accounting (north star #1,
ADR 0013's costing method), mode parity (north star #6 — the local
path must not change shape), single PC (north star #2 — local
Terraform state, per infra session #1).

## Decision

### 1. Certificate provisioning: Terraform-generated (`aws_iot_certificate`, no CSR)

AWS mints each keypair at apply time; Terraform receives
`certificate_pem` + `private_key` and writes them to disk via
`local_sensitive_file`. **Private keys exist in
`infra/terraform.tfstate`.** Accepted because every layer of that
sentence is already contained: the backend is local (versions.tf —
single-PC decision, infra session #1), `*.tfstate` is gitignored, the
git-secrets pre-commit hook (ACCOUNT_SETUP.md §7) backstops the
gitignore, and the certs themselves live only between apply and
teardown. The CSR alternative (keys never enter state) defends against
exfiltration of one gitignored file on a machine where the same keys
sit in a sibling gitignored directory — no marginal protection for a
real cost (a pre-apply openssl script that must track `fleet_size`).

### 2. Policy topology: ONE shared policy with thing-policy variables; Things named bare `P-NN`

A single `aws_iot_policy` scopes every connection with
`${iot:Connection.Thing.ThingName}`:

- `iot:Connect` only as `client/${iot:Connection.Thing.ThingName}`,
  conditioned on `iot:Connection.Thing.IsAttached: true` (the variable
  only resolves when the connecting cert is attached to a Thing —
  belt-and-braces against unattached-cert connects).
- `iot:Publish` only to
  `topic/factory/pumps/${iot:Connection.Thing.ThingName}/telemetry`.

No Subscribe/Receive — the simulator only publishes. The variable
trick requires **ThingName == client_id == pump_id**, which ADR 0003
already locks; Thing names are therefore bare `P-00`…`P-14`
(`format("P-%02d", count.index)`), unprefixed. Cross-pump publish
attempts (P-03 publishing to P-07's topic) are denied by IoT Core
itself — the policy doubles as the per-pump identity tripwire, the
same scoped-IAM-as-tripwire trick as ADR 0014/0015.

### 3. Cert layout: `simulator/.secrets/<pump_id>/`, shared root CA, Terraform-written

Already the documented convention (config.example.yaml since
2026-05-27); the module writes into it rather than inventing a new
home:

```
simulator/.secrets/
  AmazonRootCA1.pem            # shared — fetched by `data "http"` at apply
  P-00/P-00.cert.pem           # local_sensitive_file
  P-00/P-00.private.key        # local_sensitive_file
  …
  P-14/…
```

The directory is gitignored (plus blanket `*.pem` / `*.key` rules).
Amazon Root CA 1 is public material fetched from
`https://www.amazontrust.com/repository/AmazonRootCA1.pem` via the
`http` provider (postcondition: HTTP 200) — vendoring it in-repo would
need a gitignore negation carve-out through the `*.pem` rule, an
invitation for the NEXT .pem to slip through. `terraform destroy`
removes the files (they are resources), so teardown sweeps disk as
well as cloud. `local_sensitive_file` sets `0600` on the keys
(best-effort on Windows; acceptable — the directory is already the
custody boundary).

### 4. Config shape: `{pump_id}` placeholder in the existing `broker.tls` paths

```yaml
tls:
  cert_path: "simulator/.secrets/{pump_id}/{pump_id}.cert.pem"
  key_path:  "simulator/.secrets/{pump_id}/{pump_id}.private.key"
  ca_path:   "simulator/.secrets/AmazonRootCA1.pem"
```

`Fleet.from_config` expands the literal token `{pump_id}` per pump
(`simulator.config.tls_for_pump`, plain `str.replace` — NOT
`str.format`, so stray braces in paths can't raise) before
constructing each `AwsIotPublisher`. No placeholder → literal path →
exactly today's behavior, so the 2026-05-27 single-pump smoke configs
stay valid. The schema itself is unchanged (three non-empty strings);
the loader stays pure (ADR 0003 §Decision 5) — expansion happens at
runner construction, file-existence checks stay in the publisher.
Endpoint discovery: `data "aws_iot_endpoint"` (type `iot:Data-ATS`) →
root output `iot_endpoint` → PO pastes into `broker.url` (runbook
step). Port stays 8883; ALPN-on-443 remains out of scope (ADR 0003).

## Cost (free-tier posture, verified 2026-06-07)

IoT Core's free tier is **12-month, not Always-Free** — the project's
first dependency on account age, surfaced here per the session brief.
PO confirmed the account is inside its 12 months: free tier grants
2.25 M connection-minutes, 500 K messages, 225 K registry ops, 250 K
rules triggered + 250 K actions per month. A 30-minute demo uses
13,500 messages (15 pumps × 30 msg/min × 30 min; payloads ≪ 5 KB so
one metered message each), 450 connection-minutes, 13,500 rule
triggers + 13,500 actions, and ~50 registry operations at
apply/destroy — **2.7 % of the monthly message allowance; $0 while
the free tier holds.** Post-expiry honest price at published rates
(messaging $1.00/M first 1 B, connectivity $0.08/M-min, rules+actions
$0.15/M each): ≈ $0.0135 messaging + $0.004 rules engine + $0.00004
connectivity ≈ **$0.018/demo** — an order of magnitude under ADR
0013's accepted dimes, recorded in its residue ledger style. When the
account crosses its 12-month line, this number starts billing; no
redesign is triggered, the residue just becomes real.

## Alternatives considered

### 1. Certificate provisioning

**A. Terraform-generated (the decision).** Zero pre-apply steps; keys
in local-only gitignored state; lifecycle-bounded exposure.

**B. CSR-based.** Pre-apply script generates 15 keys + CSRs
(`openssl req` ×15); Terraform uploads CSRs
(`aws_iot_certificate.csr`); keys never enter state. The custody
story is strictly better on paper, but the script is a second
fleet-size-coupled artifact (drift risk), and the marginal threat it
closes — tfstate exfiltration — is dominated by the key files
themselves sitting on the same disk. Recorded as the upgrade path if
the project ever provisions anything non-ephemeral.

**C. Fleet provisioning (claim certificates / provisioning
templates).** AWS's at-scale onboarding machinery; designed for
devices that enroll themselves. At 15 simulated pumps on one PC it
adds a provisioning template, a claim cert, and an enrollment flow to
demo — all noise. Rejected.

### 2. Policy topology

**A. Shared policy + thing variables + bare names (the decision).**
One document, per-Thing scoping enforced by IoT Core at runtime.

**B. Per-Thing policies (15 rendered documents).** Same effective
permissions, no thing-variable indirection to explain, but 15
near-identical resources and no scoping the shared policy doesn't
already deliver. Would permit prefixed Thing names — not wanted
(below). Rejected.

**C. Prefixed Thing names (`ml-obs-P-00`).** Avoids registry name
collisions in a shared account, but breaks ThingName == client_id ==
pump_id (ADR 0003) unless client_ids change too — which would touch
broker logs, CloudWatch dimensions, and the MQTT contract for zero
demo value in a dedicated account. Rejected; the one real collision
(the Console-provisioned `P-00` from 2026-05-27) is handled as a
one-time pre-apply cleanup in the runbook.

### 3. Cert layout

**A. `simulator/.secrets/<pump_id>/` (the decision).** Already
documented + gitignored; one shared CA file at the directory root
(the CA is fleet-wide trust, not per-pump identity).

**B. `.local/iot_certs/`.** The brief's sketch. `.local/` is the
"host-specific glue, never required" convention — but cert material
IS required for an aws-iot run, and a second secrets directory
fragments the gitignore story. Rejected.

### 4. Config shape

**A. `{pump_id}` placeholder (the decision).** Schema-invisible,
backward compatible, explicit in the YAML the operator reads.

**B. `cert_dir` key + filename convention.** Terser but a second
conditional schema shape (`tls` vs `cert_dir`) and an implicit naming
contract the YAML never states. Rejected.

**C. Per-pump path list.** 15-entry YAML list that must match
`pump_count` — manual drift surface. Rejected.

## Consequences

**Positive:**

- Demo-day provisioning is one `terraform apply`: Things, certs,
  policy, attachments, endpoint discovery, AND on-disk cert material.
  Teardown is symmetric — destroy removes cloud resources and local
  key files.
- Per-pump identity is enforced server-side by the thing-variable
  policy; a mis-set client_id or cross-pump topic fails at CONNECT/
  PUBLISH, loudly, in the simulator's retry/halt machinery (ADR 0003).
- The local-mode path is untouched: no schema change, no new keys,
  `tls_for_pump` never runs for `target: local` (parity preserved
  without entering the parity set — the simulator still imports no
  `shared/` code).

**Negative:**

- **Private keys in tfstate.** Anyone who obtains
  `infra/terraform.tfstate` between apply and teardown holds the
  fleet's identity. Accepted for the lifecycle above; revisit via
  Alternatives 1B if any resource stops being demo-ephemeral.
- **`terraform plan` now needs internet beyond AWS** (the `http` data
  source fetches the root CA). Plan already requires AWS reachability;
  the marginal failure mode (amazontrust.com down) is small and
  loudly diagnosed by the postcondition.
- **Two new providers** (`hashicorp/local`, `hashicorp/http`) join
  the lockfile.
- **Bare `P-NN` registry names** assume a dedicated demo account; in
  a shared account they'd be collision-prone and unattributable.
  Documented here; acceptable for this project.

**Follow-ups:**

- Runbook pre-step: delete the Console-provisioned `P-00`
  Thing/cert/policy (2026-05-27 smoke) before first apply — Terraform
  `aws_iot_thing.pump[0]` would otherwise collide. (One-time.)
- `aws_teardown.sh` absence sweep: Things `P-00`…`P-NN`, the fleet
  policy, plus a FAIL-level ACTIVE-certificate count (this session;
  strengthened WARN→FAIL per the 2026-06-07 cascade review pt 4 —
  dedicated demo account, expected post-destroy count is 0).
- README cost table: cite this ADR for the IoT free-tier line next to
  ADR 0013/0015's residues.

## References

- Session brief: `docs/next_session_brief.md` (2026-06-07) — the four
  open calls; PO decisions via AskUserQuestion walkthrough.
- ADR 0003 — per-pump connection/Thing topology + `AwsIotPublisher`
  (the contract this provisioning serves); §Addendum 2026-05-27 for
  the Console-provisioned smoke this supersedes.
- ADR 0013 / ADR 0015 — cost-residue recording pattern; the
  $0.018/demo post-expiry figure sits beside their entries.
- ADR 0014 §Decision 5 / ADR 0015 — scoped-policy-as-tripwire pattern
  the thing-variable policy reuses.
- Implementation: `infra/modules/iot_fleet/`, root `infra/main.tf` +
  `outputs.tf` + `versions.tf`, `simulator/config.py::tls_for_pump`,
  `simulator/runner.py::Fleet.from_config`,
  `simulator/tests/test_tls_per_pump.py`, `scripts/aws_teardown.sh`,
  `docs/runbooks/aws-demo-day.md`.
- External (verified 2026-06-07): AWS IoT Core pricing —
  https://aws.amazon.com/iot-core/pricing/ (free-tier quantities +
  published rates above); thing policy variables —
  https://docs.aws.amazon.com/iot/latest/developerguide/thing-policy-variables.html
