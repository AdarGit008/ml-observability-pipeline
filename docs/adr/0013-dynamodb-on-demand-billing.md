# ADR 0013 — DynamoDB On-Demand Billing for pump_hot_state

- **Status:** Accepted (PO sign-off 2026-06-04; reviewer-cascade review pending)
- **Date:** 2026-06-04
- **Deciders:** PO (Adar), Claude (architect), reviewer cascade (pending)

## Principle (plain English)

**$0 at rest, cents per demo — knowingly.** The hot-state table bills
on-demand (`PAY_PER_REQUEST`). It exists only between `terraform
apply` and `aws_teardown.sh`; between demos nothing is provisioned
and nothing bills. During a demo, the per-request charges come to
roughly $0.10–0.20 per 30-minute run. This is the project's first
knowingly non-$0 line item, and this ADR exists to own that on the
record: the literal-$0 alternative was computed, found to require a
redesign of the hot path plus a smaller demo, and rejected by the PO
with the math in hand.

## Context

ADR 0010 deferred the capacity-mode decision to the IaC session with
a recommendation of on-demand. The brief flagged the math worth
re-doing because the Always-Free 25 RCU/WCU applies to *provisioned*
tables only, while on-demand bills per request — and at 13.5 K
invocations/demo that is not obviously negligible.

The session ran the math. A reading row is ~130–150 bytes, so the
single hot-path Query (`Limit=1800`, the PSI window per the
2026-06-02 follow-on) reads ~230–270 KB ≈ 30–35 eventually-consistent
read units per invocation. At the demo's fleet rate (15 pumps ×
30 msg/min = 7.5 invocations/sec) that is a sustained **~220–260 RCU
of read demand — roughly 9–10× over the 25 RCU Always-Free
provisioned ceiling**. A provisioned-free-tier table throttles
continuously and kills the hot path mid-demo; burst credits cover
seconds, not 30 minutes. Writes (2 PutItems/invocation ≈ 15 WCU
sustained) fit the free 25 WCU; reads are the binding constraint.

Per-demo on-demand cost: ~430 K read units + ~34 K write units ≈
**$0.10–0.20 per 30-minute demo** at post-Nov-2024 on-demand prices
(eu-central-1 runs slightly above us-east-1). Ten demos ≈ $1–2
lifetime — at the $1 budget-alert line, two orders of magnitude
under the $5 alert. Storage is inside the 25 GB Always-Free
regardless of billing mode.

## Decision

`pump_hot_state` uses **on-demand (`PAY_PER_REQUEST`)** billing. The
~$0.10–0.20 per-demo request cost is accepted and documented here as
a deliberate, bounded exception to north star #1's literal reading —
the table's existence is already bounded by the apply → demo →
teardown lifecycle, so the cost has no standing component.

## Alternatives considered

**A. On-demand (the decision).** Zero standing cost, zero redesign,
the demo runs at full design scale (15 pumps, 1800-row PSI window,
per-invocation PSI). Cents per run.

**B. Provisioned within the 25 RCU Always-Free — literal $0.**
Infeasible as-is: 9–10× over read capacity (math above). Reachable
only via a redesign chain: compute PSI every ~30th invocation per
pump instead of every invocation (reading the 150-row scoring slice
otherwise, dropping average read cost ~7×) — which still lands at
~34 RCU at 15 pumps, so the fleet must also shrink to ~10 pumps to
fit under 25. Total price of literal-$0: a lambda_scorer redesign
session (parity-set loads), an ADR for the local/AWS PSI-cadence
divergence (local computes per-tick — north star #6 says divergence
is a bug or an ADR), a thinner demo, and the loss of the
"every-invocation PSI is the simpler stateless shape" property the
2026-06-02 session deliberately chose. Rejected by PO with the
numbers in hand: the architecture's simplicity is worth more to the
portfolio story than the dimes. Recorded as reachable if
circumstances change.

**C. Defer via a billing_mode variable.** Costs nothing today but
defers a decision the session had full information to make, and
leaves a foot-gun default in the repo (whichever default is chosen
is the decision, undocumented). Rejected: decide now, document why.

**D. Cheaper reads via projection or caching.** Computed and
discarded: `ProjectionExpression` does not reduce Query RCU
consumption (capacity is charged on items accessed, not attributes
returned); in-Lambda window caching breaks the statelessness ADR
0010 locked; storing the window as one blob item reads the same
bytes for the same cost; incrementally-maintained histogram counters
on the STATE row would fork PSI computation away from
`shared.drift.compute_psi` — a mode-parity break (north star #6),
the one cost the project never pays.

## Consequences

**Positive:**

- Demo runs at full design scale with zero throttling risk; no
  code changes anywhere.
- No standing cost surface — billing is exactly coextensive with
  the demo lifecycle the teardown script already bounds.
- The $1/$5 budget alerts (ACCOUNT_SETUP.md) now have a known,
  predictable consumer; an alert email after ~7–10 demos is
  expected, not a leak signal.

**Negative:**

- North star #1 ("$0 lifetime cost") now carries one documented
  exception (~$0.15/demo, ~$1–2 over the project's life). Any
  future "is this really $0?" question lands here.
- Re-running demos freely is no longer literally free — rehearsals
  count. Mitigation: local mode (InfluxDB) remains the $0 rehearsal
  path; AWS demos are for the real thing.

**Follow-ups:**

- README cost table (portfolio framing) should cite this ADR for
  the per-demo cost line.
- If demo cadence ever makes the dimes matter, alternative B's
  redesign chain is the recorded path back to literal $0.

## References

- ADR 0010 §Capacity mode — the deferred recommendation this ADR
  resolves.
- 2026-06-02 PSI follow-on session log — the Limit=1800 single-Query
  decision that drives the read math.
- `infra/modules/dynamodb/main.tf` — implementation.
- Session log: `docs/sessions/2026-06-04-infra-terraform-hot-path.md`.
- External: AWS DynamoDB on-demand pricing (post-Nov-2024 halved
  rates); AWS Always-Free tier (25 GB + 25 provisioned RCU/WCU).
