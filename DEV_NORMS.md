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

## 4. Reviewer-model loop

The adversarial-review role is held by a **pool of models**, not a single vendor. ADR 0001 set up the original Gemini-only path; **ADR 0011 (2026-06-02)** broadened it to a cascading multi-provider chain after a hard `429 RESOURCE_EXHAUSTED` from Gemini's free tier blocked a session-done workflow. All providers in the chain are classified identically — same role, same packet format, same "don't rubber-stamp" expectation. The choice of which one ran on a given response is captured as a **provenance footer** on the response file (load-bearing for audit; see below).

The script `scripts/gemini_review.ps1` retains its name despite the broadened scope — it's a well-known entrypoint, and renaming would invalidate every past session log's commit-draft PowerShell sequence (ADR 0011 §Decision #5). Read the filename as "the script that runs the review", not "the script that calls Gemini".

**One-time setup (PO machine) — set whichever provider keys you have:**
```powershell
[Environment]::SetEnvironmentVariable("GEMINI_API_KEY",     "<your-key>", "User")  # https://aistudio.google.com/apikey
[Environment]::SetEnvironmentVariable("OPENROUTER_API_KEY", "<your-key>", "User")  # https://openrouter.ai/keys
[Environment]::SetEnvironmentVariable("GROQ_API_KEY",       "<your-key>", "User")  # https://console.groq.com/keys
[Environment]::SetEnvironmentVariable("CEREBRAS_API_KEY",   "<your-key>", "User")  # https://cloud.cerebras.ai/?tab=api-keys
# Open a new PowerShell window so the env vars load.
```

At minimum one key is required. The cascade skips providers whose env var isn't set.

**Provider chain (default `-Provider auto`):** `gemini → openrouter → groq → cerebras`. Each provider's default free-tier model:

| Provider | Default model | Why this default |
|---|---|---|
| gemini | `gemini-pro-latest` | Project's original reviewer; matches ADR 0001 baseline |
| openrouter | `deepseek/deepseek-r1:free` | DeepSeek R1 is reasoning-optimised; OpenRouter committed to keeping it on free tier (2026-04-24 announcement) |
| groq | `llama-3.3-70b-versatile` | Fastest free-tier inference; capacity pool independent of OpenRouter |
| cerebras | `llama-3.3-70b` | Independent capacity pool from Groq; belt-and-suspenders |

**Per-review flow:**

1. Claude writes `review_packets/YYYY-MM-DD-<slug>.md` using `templates/review_packet_template.md`.
2. PO runs (from the repo root):
   ```powershell
   .\scripts\gemini_review.ps1 -Slug <slug>
   # Force a specific provider (no cascade):
   .\scripts\gemini_review.ps1 -Slug <slug> -Provider groq
   # Override the first-tried model:
   .\scripts\gemini_review.ps1 -Slug <slug> -Model gemini-2.5-flash
   ```
   …which writes `review_responses/YYYY-MM-DD-<slug>.md` with a provenance footer naming the provider + model that wrote it.
3. Claude reads the response, addresses each point in the packet's "Resolution" section, commits.

**Provenance footer (audit signature) — load-bearing.** Every response file ends with:
```
---
_Generated by **<provider>** (`<model>`) on YYYY-MM-DD HH:MM:SS._
```
This footer is the audit backbone of the multi-provider workflow (ADR 0011 §Decision #3). Do not delete it from a committed file. If a response needs fixing, regenerate it — don't patch.

**What goes in a review packet** (see template):
- One-paragraph summary of what changed and why.
- The diff (or links to changed files).
- Specific questions for the reviewer (e.g., "is the PSI smoothing correct?").
- A "constraints reminder" pointing the reviewer at the north stars.

**The reviewer's job is not to rubber-stamp.** If the reviewer agrees with everything, the packet probably asked the wrong questions. Push the reviewer on trade-offs explicitly. This applies to whichever provider in the chain ends up filling the slot.

---

## 5. Modular context — no bleed between sessions

The biggest risk of multi-session work is context drift: loading irrelevant docs that crowd the model's attention. We mitigate this with a deliberate four-tier loading system.

### Tier 1 — Always load (~small)
- `DEV_NORMS.md` (this file)
- `context/_global.md` — constraints, conventions, locked decisions
- `MEMORY.md` — auto-loaded by Claude

### Tier 2 — Component context (load one)
- `context/<component>.md` — exactly one per session, matching the component being worked on
- Components mirror the `PLAN.md §1` repo layout: `simulator`, `model`, `lambda_scorer`, `lambda_s3_batcher`, `drift`, `local_runtime`, `infra`, `dashboards`, `dev_workflow`

### Tier 2b — Parity-touching components (load IN ADDITION to Tier 2)

Some components share logic across the local/AWS boundary via the top-level `shared/` package (locked by ADR 0005). If the session's `Component:` is in the parity set below, OR the `Intent:` mentions scoring, drift, feature extraction, or anything that calls `extract_features` / `score` / `compute_psi`, the brief MUST also load:

- The on-disk source: `shared/features.py`, `shared/score.py`, `shared/drift.py` (read, don't re-derive).
- The locked contract: `docs/adr/0005-shared-mode-parity-package-and-subscriber-topology.md`.
- The enforcement test (cite by name; do NOT delete to "fix" a parity break): `local_runtime/tests/test_service.py::test_structural_parity_no_vendoring` (+ siblings for `score` and `compute_psi`).

**Parity set (as of 2026-05-29):** `lambda_scorer`, `model`, `drift`, `local_runtime`, `dashboards`. Any future component that calls into `shared/` joins the set; add it here in the same PR that adds the import.

**Why this is its own tier:** the parity contract is a hard cross-component invariant, not a component file. A session that touches the boundary without loading it risks silent divergence — and silent divergence between local and AWS modes violates north star #6 (mode parity). The PO and Claude both should refuse to start work if a session brief for a parity-set component omits these loads.

### Tier 3 — Interfaces (load only if work crosses components)
- `context/_interfaces.md` — MQTT schema, DynamoDB schema, Lambda envelopes, etc.

### Session brief (start of every session)
The PO opens a session with a one-line declaration matching `templates/session_brief_template.md`:

```
Component: lambda_scorer
Intent:    Implement DynamoDB read+append for feature window
Loads:     _global, lambda_scorer, _interfaces, shared/ + ADR 0005   # ← Tier 2b: parity-touching
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
Inputs and outputs of the reviewer-model loop (§4). Committed alongside code so the review trail is auditable. Each response file's provenance footer names which provider + model wrote it (ADR 0011).

Template: `templates/review_packet_template.md`.

### What goes where — quick lookup

| Question | Answer |
|---|---|
| "Why did we pick DynamoDB over Timestream?" | ADR 0003 |
| "Where does the mode-parity contract live?" | ADR 0005 + `shared/` |
| "What was decided about PSI smoothing on 2026-05-30?" | Session log for that date |
| "Did the reviewer flag the cold-start risk?" | Review response for that session (footer names the model) |
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
- **Review before commit** *(new 2026-06-04, PO call)*: the session's commit is staged only AFTER the reviewer-model cascade (§4 / ADR 0011) has run and the findings are dispositioned in the session log. That way the review response file, the dispositions table, and any diff-from-review all ride in the same commit as the code they reviewed. Canonical session order: tests green → review packet → cascade → fold dispositions into session log → commit.
- **`git secrets` pre-commit hook required** (already in `ACCOUNT_SETUP.md §7`).
- **Commit drafts ship with the staging command sequence** *(new 2026-06-04)*. Every commit-message draft Claude produces — in chat, in a session log, in a review packet — is paired with the full PowerShell sequence: `git status` + `git diff --stat` (sanity-check) → `git add -A` → `git status` + `git diff --cached --name-status` (verify staged) → here-string piped to `Out-File -Encoding utf8 -NoNewline $env:TEMP\commit-msg.txt` then `git commit -F` (UTF-8 commit message, no PowerShell `>` UTF-16 trap) → `git log -1 --stat` (confirm). Sequence is canonical pattern from `docs/sessions/2026-06-04-followup-items-3-4-5-7.md`. Rationale: avoid re-deriving the encoding/here-string pattern each commit, surface staging mismatches before commit, dodge the `gemini > response.md` UTF-16 issue's sibling at commit time.

---

## 8. Definition of "session done"

Before closing a session, Claude verifies:

- [ ] All tests pass locally (`pytest`, `terraform validate`, whatever applies to the component).
- [ ] Review packet has been written and run through the reviewer-model cascade (§4).
- [ ] Reviewer findings are either addressed or have a written "won't fix because…" in the packet. (Weight findings against the response's provenance footer — DeepSeek-R1's adversarial posture differs from Llama-3.3-70b's; see ADR 0011 §Consequences.)
- [ ] Session log written, dated, committed.
- [ ] ADR written if an architectural decision was made.
- [ ] `context/<component>.md` updated if interfaces or open questions changed.
- [ ] For parity-touching sessions (Tier 2b): structural parity tests still pass.
- [ ] PO has approved the merge.

---

## 9. When norms conflict

If this document and `PLAN.md` disagree, `PLAN.md` wins on scope/architecture; this document wins on process. If both agree but reality differs, write an ADR and update both.

If something feels wrong about these norms in practice, that's a signal to revise — not to silently route around. Bring it up; we change the doc.
