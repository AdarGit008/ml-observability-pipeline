# Development Norms — ML Observability Pipeline

How we work, who decides what, and where decisions are recorded. Read this once at the start of any session. Everything else loads on demand.

---

## 1. Roles

| Role | Who | Authority |
|---|---|---|
| **Product Owner (PO)** | User (Adar) | Sets priorities, approves architecture, accepts deliverables, signs off on merges. Final say on scope and trade-offs. |
| **Lead Architect** | Claude | Proposes design, writes code, drafts ADRs, prepares review packets, addresses Gemini feedback. Owns implementation quality. |
| **Code Reviewer** | Gemini (via CLI) | Reviews diffs, flags risks, suggests improvements, validates against project constraints. Adversarial-but-fair. |

**Single rule that resolves disputes:** PO has final say. Claude and Gemini debate openly in the review packet; PO reads the disagreement and picks. Never silently override Gemini — surface the disagreement.

---

## 2. Project north stars (do not violate)

These are the constraints every decision is checked against. Lifted from `HANDOFF.md` and `PLAN.md`. Pinned here so they always load.

1. **$0 lifetime cost.** Anything that risks ongoing AWS spend gets escalated to PO before merging.
2. **Single PC.** No spare hardware, no second machine assumptions.
3. **One polished repo, not five half-finished ones.** Scope creep is the enemy.
4. **AWS-specific differentiation.** Choices that have clean GCP/Azure analogues are weaker portfolio signals.
5. **Trade-off rationale visible everywhere** — README, ADRs, commit messages, session logs.
6. **Mode parity.** Local mode and AWS demo mode share scoring/drift logic. Any divergence is a bug or an ADR.

---

## 3. Workflow (per task)

Each task is a unit of work small enough to finish in one session. Roughly: one component change, one ADR-worthy decision, or one bug fix.

```
┌─────────────────────────────────────────────────────────────────┐
│  1. PO opens session → declares component + intent              │
│  2. Claude loads session brief (see §5) — only relevant context │
│  3. Claude proposes plan → PO approves or revises               │
│  4. Claude implements → tests pass locally                      │
│  5. Claude writes review packet → PO runs Gemini CLI            │
│  6. Claude addresses Gemini feedback (or pushes back w/ reason) │
│  7. PO approves merge → commit → session log written            │
│  8. ADR written if architectural decision was made              │
└─────────────────────────────────────────────────────────────────┘
```

**Hard stops:**
- No code without an approved plan (step 3).
- No merge without Gemini review (step 5) for anything touching scoring, drift, IaC, or interfaces between components.
- No session ends without a session log (step 7). Trivial typo-fix sessions can log in one line.

**Exceptions** (PO approval not required mid-flight):
- Comment fixes, formatting, README typos.
- Test-only additions that don't change interfaces.

---

## 4. Gemini review loop

Gemini participates via the Gemini REST API, called from `scripts/gemini_review.ps1` (Windows) or `scripts/gemini_review.sh` (bash). The script replaces the `@google/gemini-cli` package — see ADR 0001 for why.

**One-time setup (PO machine):**
```powershell
# Grab a key at https://aistudio.google.com/apikey, then:
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY", "<your-key>", "User")
# Open a new PowerShell window so the env var loads.
```

**Per-review flow:**

1. Claude writes `review_packets/YYYY-MM-DD-<slug>.md` using `templates/review_packet_template.md`.
2. PO runs (from the repo root):
   ```powershell
   .\scripts\gemini_review.ps1 -Slug <slug>
   # if Pro is over capacity (503):
   .\scripts\gemini_review.ps1 -Slug <slug> -Model gemini-2.5-flash
   ```
   …which writes `review_responses/YYYY-MM-DD-<slug>.md`.
3. Claude reads the response, addresses each point in the packet's "Resolution" section, commits.

**What goes in a review packet** (see template):
- One-paragraph summary of what changed and why.
- The diff (or links to changed files).
- Specific questions for Gemini (e.g., "is the PSI smoothing correct?").
- A "constraints reminder" pointing Gemini at the north stars.

**Gemini's job is not to rubber-stamp.** If Gemini agrees with everything, the packet probably asked the wrong questions. Push Gemini on trade-offs explicitly.

---

## 5. Modular context — no bleed between sessions

The biggest risk of multi-session work is context drift: loading irrelevant docs that crowd the model's attention. We mitigate this with a deliberate three-tier loading system.

### Tier 1 — Always load (~small)
- `DEV_NORMS.md` (this file)
- `context/_global.md` — constraints, conventions, locked decisions
- `MEMORY.md` — auto-loaded by Claude

### Tier 2 — Component context (load one)
- `context/<component>.md` — exactly one per session, matching the component being worked on
- Components mirror the `PLAN.md §1` repo layout: `simulator`, `model`, `lambda_scorer`, `lambda_s3_batcher`, `drift`, `local_runtime`, `infra`, `dashboards`, `dev_workflow`

### Tier 3 — Interfaces (load only if work crosses components)
- `context/_interfaces.md` — MQTT schema, DynamoDB schema, Lambda envelopes, etc.

### Session brief (start of every session)
The PO opens a session with a one-line declaration matching `templates/session_brief_template.md`:

```
Component: lambda_scorer
Intent:    Implement DynamoDB read+append for feature window
Loads:     _global, lambda_scorer, _interfaces
```

Claude reads exactly those files. Nothing else.

**Why this works:** PLAN.md is 380 KB; loading it every session burns context for no benefit after week 1. Component files stay <5 KB each, are kept fresh, and link out to ADRs for depth.

---

## 6. Documentation method

Three artifact types, each with a fixed home and purpose. No overlap.

### 6.1 ADRs — `docs/adr/NNNN-<slug>.md`
Architecture Decision Records. One per architectural decision. Numbered sequentially, immutable once accepted (supersede via new ADR; do not edit).

Use when: a choice has trade-offs that future-you (or a recruiter) would ask about.
Examples already planned in PLAN.md: `0003-dynamodb-instead-of-timestream.md`, `0004-lambda-batching-instead-of-firehose.md`.

Template: `templates/adr_template.md`.

### 6.2 Session logs — `docs/sessions/YYYY-MM-DD-<component>-<slug>.md`
Lightweight running log. One per development session. Captures: what was worked on, what changed, key trade-offs surfaced, Gemini's main feedback, anything the next session needs to know.

Use when: every session. Even the trivial ones (one-liner is fine).

Template: `templates/session_log_template.md`.

### 6.3 Review packets / responses — `review_packets/`, `review_responses/`
Inputs and outputs of the Gemini loop. Committed alongside code so the review trail is auditable.

Template: `templates/review_packet_template.md`.

### What goes where — quick lookup

| Question | Answer |
|---|---|
| "Why did we pick DynamoDB over Timestream?" | ADR 0003 |
| "What was decided about PSI smoothing on 2026-05-30?" | Session log for that date |
| "Did Gemini flag the cold-start risk?" | Review response for that session |
| "What is the current MQTT topic schema?" | `context/_interfaces.md` |
| "What's the project's hard cost ceiling?" | `context/_global.md` |

If a fact appears in more than one place, it's a bug. Pick one home and link.

---

## 7. Git conventions

- **Branching:** PRs to `main`. No direct pushes (matches PLAN.md §1).
- **Commit messages:** Imperative mood, ≤72 chars subject. Body explains *why*, not *what* (the diff shows what).
- **PR titles:** `<component>: <action>` — e.g., `lambda_scorer: add DynamoDB feature window read`.
- **PR description:** Link the session log + any new ADRs + the review response.
- **One PR per session** in the normal case. Stacked PRs allowed for refactors that genuinely fan out.
- **`git secrets` pre-commit hook required** (already in `ACCOUNT_SETUP.md §7`).

---

## 8. Definition of "session done"

Before closing a session, Claude verifies:

- [ ] All tests pass locally (`pytest`, `terraform validate`, whatever applies to the component).
- [ ] Review packet has been written and run through Gemini.
- [ ] Gemini's findings are either addressed or have a written "won't fix because…" in the packet.
- [ ] Session log written, dated, committed.
- [ ] ADR written if an architectural decision was made.
- [ ] `context/<component>.md` updated if interfaces or open questions changed.
- [ ] PO has approved the merge.

---

## 9. When norms conflict

If this document and `PLAN.md` disagree, `PLAN.md` wins on scope/architecture; this document wins on process. If both agree but reality differs, write an ADR and update both.

If something feels wrong about these norms in practice, that's a signal to revise — not to silently route around. Bring it up; we change the doc.
