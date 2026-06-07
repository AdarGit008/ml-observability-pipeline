# Session 2026-06-07 — simulator — iot-fleet-provisioning

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** reviewer model from the cascade (see response file footer; ADR 0011)
- **Context loaded:** `_global`, `simulator`, `_interfaces` (§MQTT), `infra`, ADR 0003/0013/0014/0015, DEV_NORMS §5/§7/§8, memory (fuse-write-truncation, git-on-windows, infra-session1)
- **Duration:** ~1 session

## Intent
Make the simulator able to publish to AWS IoT Core at fleet scale: Terraform provisioning for 15 Things/certs/policy (`infra/modules/iot_fleet`), per-pump mTLS identity in the simulator config (`{pump_id}` placeholder), teardown coverage, and the demo-day runbook — unblocking the first AWS end-to-end run.

## What changed
- **NEW `infra/modules/iot_fleet/`** — 15 `aws_iot_thing` (bare `P-NN`), 15 `aws_iot_certificate` (AWS-generated, `active`), ONE `aws_iot_policy` scoped by `${iot:Connection.Thing.ThingName}` (Connect-as-self with `IsAttached` condition + Publish-to-own-topic only), policy/thing attachments, `data aws_iot_endpoint` (ATS), `data http` root-CA fetch (postcondition 200), `local_file`/`local_sensitive_file` writing `simulator/.secrets/{AmazonRootCA1.pem, P-NN/…}`.
- **Root `infra/`** — module wired into `main.tf`; `iot_policy_name` var (default `pump-fleet-policy`); outputs `iot_endpoint`, `iot_thing_names`, `iot_policy_name`; providers `hashicorp/local ~> 2.5` + `hashicorp/http ~> 3.4` added to `versions.tf` (PO must re-run `terraform init`).
- **`simulator/config.py`** — `PUMP_ID_PLACEHOLDER` + pure `tls_for_pump()` (literal `str.replace`, not `str.format`). Loader untouched (shape-only validation stands, ADR 0003 §Decision 5).
- **`simulator/runner.py`** — `Fleet.from_config._make_publisher` expands the placeholder per pump; the same closure is the stashed `publisher_factory`, so `FleetExpansion.add_pump` mints per-pump identity too. Also a self-caught guard: multi-pump aws-iot configs whose cert/key paths carry NO placeholder share one cert fleet-wide — the thing-variable policy then denies CONNECT for N−1 pumps as *transient* errors (silent 30s-cap retry loop, the buried-error UX ADR 0003 §Addendum 2026-05-28 exists to avoid). `from_config` now logs a loud WARNING for that shape (warning, not error — non-AWS mTLS brokers may legitimately share certs).
- **`simulator/config.example.yaml`** — placeholder-shaped tls example + `terraform output -raw iot_endpoint` pointer.
- **NEW `simulator/tests/test_tls_per_pump.py`** — 16 tests (pure expansion ×6, from_config ×5, add_pump ×2, shared-cert warning ×3). No filesystem/network/monkeypatching.
- **`scripts/aws_teardown.sh`** — iot-fleet sweep: Things `P-00..P-(FLEET_SIZE-1)` FAIL, policy FAIL, ACTIVE-cert count FAIL (shipped as WARN; strengthened WARN→FAIL post-cascade, pt 4), leftover local `*.private.key` WARN. `bash -n` clean.
- **NEW `docs/runbooks/aws-demo-day.md`** — apply → config → simulate → observe (incl. dashboards #2 verify-don't-assume checklist + cold-start canary note) → teardown. One-time pre-step: delete the Console-provisioned P-00 from the 2026-05-27 smoke (collides with `aws_iot_thing.pump[0]`).
- **`context/simulator.md` + `context/infra.md`** — current state, interfaces, resolved open questions, ADR 0016 links.
- **NEW ADR 0016** — all four decisions + cost section.

## Decisions
- **ADR 0016** (all PO-confirmed via AskUserQuestion walkthrough): (1) Terraform-generated certs — private keys in LOCAL-ONLY gitignored tfstate, accepted for the apply→demo→teardown lifecycle; (2) one shared policy + thing-policy variables + bare `P-NN` Thing names (ThingName == client_id == pump_id, ADR 0003 lock); (3) cert layout `simulator/.secrets/<pump_id>/` + shared root CA, Terraform-written; (4) `{pump_id}` placeholder in the existing `broker.tls` paths.
- Root CA fetched via `http` provider instead of vendored (would need a gitignore negation through the blanket `*.pem` rule).
- Cost (verified 2026-06-07 against aws.amazon.com/iot-core/pricing): IoT free tier is 12-month, account confirmed inside it → $0/demo now; honest post-expiry residue ≈ $0.018/demo (13.5K msgs ≈ $0.0135 + rules engine ≈ $0.004 + connection minutes ≈ $0.00004), recorded in ADR 0016 §Cost beside ADR 0013/0015's entries.

## Trade-offs surfaced
- Key-in-state vs CSR: CSR's custody win defends a threat (tfstate exfiltration) dominated by the key files on the same disk; rejected for a fleet-size-coupled pre-apply script. Recorded as the upgrade path.
- `terraform plan` now needs reach to amazontrust.com (root-CA fetch) — plan already needs AWS; marginal failure loudly diagnosed by the postcondition.
- Bare `P-NN` registry names assume a dedicated demo account (collision-prone in shared accounts) — documented in ADR 0016.
- ACTIVE-cert teardown check is count-based WARN, not FAIL — certs have no stable names and a non-fleet cert shouldn't block every teardown. *(Superseded post-cascade: reviewer pt 4 + PO call strengthened this to FAIL — in a dedicated demo account the expected count is always 0, so anything ACTIVE warrants investigation.)*

## Reviewer feedback highlights
Cascade ran 2026-06-07 (groq / llama-3.3-70b-versatile); response in
`review_responses/2026-06-07-simulator-iot-fleet-provisioning.md`, full
disposition table folded into the packet's §Resolution. One code change:

- **Pt 4 ACCEPTED (PO call): teardown ACTIVE-cert check WARN → FAIL.**
  Dedicated demo account ⇒ post-destroy expected count is always 0; any
  ACTIVE cert should block, not skim past. `scripts/aws_teardown.sh`
  (check + both comment blocks), ADR 0016 §Follow-ups, and
  `context/infra.md` updated.
- Pt 1 rejected (PO call): no explicit Deny statements — IoT default-deny
  suffices, nothing else attaches to these certs; reviewer confirmed no
  extra actions (`iot:Receive`/`iot:RetainPublish`) are needed.
- Pt 2 rejected: `.gitignore` already covers `*.tfstate.*` (incl.
  `.backup`); no TF output exposes `private_key`; no CI; S3 backend
  contradicts ADR 0016's local-only decision.
- Pts 3/5 accepted-as-done: placeholder + amazontrust.com dependency were
  already documented (config.example.yaml, runbook).
- Pt 6: reviewer concurs on parity posture; no change.

## State at end of session
- Tests: **427 passed + 1 skipped** (baseline 411+1 + 16 new), sandbox Python 3.10. HCL parse check green on all touched `.tf` (sandbox structural check only — `terraform validate` + `plan` are PO-side, all three build scripts first).
- BOM/CRLF scan: all 16 touched files BOM-free, LF.
- **Commit DEFERRED** per `docs/next_session_brief.md` §Commit sequencing: dashboards #2 commits FIRST (explicit-path staging, NOT `git add -A`); this session's commit draft is staged below and runs only after that lands.
- `context/simulator.md` + `context/infra.md` updated: yes.

## Commit draft (staged, NOT run — §Commit sequencing)

Subject: `simulator+infra: Terraform IoT fleet + per-pump mTLS identity`

Body:
```
One terraform apply now provisions the fleet's identity end-to-end:
15 Things (bare P-NN == client_id, ADR 0003), AWS-generated certs,
one thing-variable-scoped policy, and on-disk cert material under
gitignored simulator/.secrets/. The simulator's single broker.tls
block gains a {pump_id} placeholder expanded per pump at runner
construction — closing the latent single-cert assumption the
2026-05-27 single-pump smoke never exposed.

Private keys live in the local-only gitignored tfstate — custody
trade accepted and recorded in ADR 0016 (demo-ephemeral lifecycle).
IoT free-tier posture verified and priced honestly (12-month tier,
account inside it; ~$0.018/demo after expiry).

Teardown sweeps the new surface (Things, policy, ACTIVE-cert FAIL,
leftover local keys). Demo sequence: docs/runbooks/aws-demo-day.md.

ADR 0016. Suite 411+1 -> 427+1.
```

PowerShell sequence (DEV_NORMS §7 — run ONLY after the dashboards #2 commit lands; `git add -A` is then safe because the tree holds only this session's changes):
```powershell
git status; git diff --stat
git add -A
git status; git diff --cached --name-status
$msg = @"
simulator+infra: Terraform IoT fleet + per-pump mTLS identity

<body as above>
"@
$msg | Out-File -Encoding utf8 -NoNewline $env:TEMP\commit-msg.txt
git commit -F $env:TEMP\commit-msg.txt
git log -1 --stat
```

## Note for next session
The first live AWS end-to-end apply is now unblocked: follow `docs/runbooks/aws-demo-day.md` (mind the one-time Console-P-00 cleanup and the new `terraform init` for the local/http providers). The dashboards verify-don't-assume items and the cold-start canary are folded into the runbook's §3 checklist — close them in `context/dashboards.md` / `context/lambda_scorer.md` once observed green. CI cost guardrails remain the last infra TODO.
