# ADR 0011 — Multi-Provider Review Cascade with Per-Response Audit Provenance

- **Status:** Accepted (PO sign-off 2026-06-02; reviewer-model sign-off pending — first review under the new cascade itself)
- **Date:** 2026-06-02
- **Deciders:** PO (Adar), Claude (architect), reviewer model (see §References)

## Principle (plain English)

**The adversarial-review role is held by a pool of models, not a single
vendor.** Gemini was the original reviewer (ADR 0001) and remains the
default — but it can run out of free-tier quota, hit a 503 capacity
event, or have its model identifier drift, and when any of those
happens the review loop should not stop. So the script that runs the
review cycles through a chain of providers — Gemini, then OpenRouter,
then Groq, then Cerebras — picking the first one whose API key is
configured and whose call returns a non-error response.

The four providers are **classified identically** within the project:
all serve as adversarial-but-fair reviewers, all receive the same
review packet verbatim, all are bound by the same "do not rubber-
stamp" expectation (DEV_NORMS §4). The choice of which one ran on a
given packet is an audit detail, not a quality tier — the review
packet is the load-bearing artifact, and any of the four is permitted
to fill the reviewer slot.

To make the choice auditable, **every response file carries a
provenance footer** naming the provider and model that wrote it.
A future reader can grep `review_responses/` and answer "which model
reviewed the lambda_scorer MVP?" without rerunning anything. Sessions
where Gemini was unavailable and Groq stood in are visibly different
from sessions where Gemini wrote the response — the footer makes
this transparent rather than implicit.

The rest of this ADR captures: which providers, which default models,
the cascade behaviour, the provenance discipline, and the deliberate
choice to keep the script's filename (`gemini_review.ps1`) despite
the broadened scope.

## Context

ADR 0001 (2026-05-24) replaced the Gemini CLI with a direct REST-API
script (`scripts/gemini_review.ps1`). The script worked for two weeks
of sessions, then on the 2026-06-02 lambda_scorer MVP review hit a
hard `429 RESOURCE_EXHAUSTED` from Gemini's free tier:

```
Quota exceeded for metric: ...generate_content_free_tier_requests,
  limit: 0, model: gemini-3.1-pro
```

The retry-after delay was 22 s, but inspection of the response body
showed the quota was day-scoped, not minute-scoped — waiting wouldn't
help for the rest of the day. A flash-model fallback (the existing
"Pro is over capacity → try flash" hint from the original script)
also returned 503 UNAVAILABLE.

Two distinct failure modes surfaced in one session:

1. **Quota exhaustion** — daily free-tier limit hit (429). Waiting
   doesn't recover within the session.
2. **Capacity exhaustion** — model temporarily overloaded (503).
   Waiting *might* recover, but not predictably.

ADR 0001 §Consequences anticipated capacity events ("Capacity
fallback baked in: 503 on Pro prints the exact command to retry with
Flash") but didn't anticipate full-vendor unavailability. ADR 0001
§Alternatives C explicitly deferred multi-provider work: *"Use a
different LLM as reviewer (Claude API, OpenAI). Out of scope for
this ADR — the choice of which reviewer is orthogonal to how we
invoke it."* This ADR closes that deferred line.

Constraints driving the decision (per `context/_global.md`):

- **#1 $0 lifetime cost.** Whatever providers we add must offer a
  free tier sufficient for ~1–2 review packets per session,
  ~1–2 sessions per week.
- **#2 single PC.** Same machine the rest of the loop runs on; no
  CI orchestration.
- Cross-cutting: **the review packet itself must remain
  provider-agnostic** — the same content the Gemini API consumes
  must work without modification on an OpenAI-compatible
  chat-completions endpoint.

The four providers surveyed and their free-tier specifics:

| Provider | Endpoint | Free model (default) | Capacity pool |
|---|---|---|---|
| **Gemini** (Google) | `generativelanguage.googleapis.com` | `gemini-pro-latest` | Google free tier (day-scoped requests + tokens) |
| **OpenRouter** | `openrouter.ai/api/v1/chat/completions` | `deepseek/deepseek-r1:free` | Aggregator — single key unlocks many free models; rate-limited |
| **Groq** | `api.groq.com/openai/v1/chat/completions` | `llama-3.3-70b-versatile` | Groq's own LPU inference cluster; generous free daily token cap |
| **Cerebras** | `api.cerebras.ai/v1/chat/completions` | `llama-3.3-70b` | Cerebras wafer-scale cluster; separate capacity pool from Groq |

The three OpenAI-compatible providers share one request shape
(`{model, messages: [{role, content}]}`) and one response shape
(`choices[0].message.content`), so the handler code is a single
function with provider-specific endpoint + auth header.

## Decision

Adopt a **cascading multi-provider review pool** with the chain
order `gemini → openrouter → groq → cerebras`. All four classified
identically within the project as adversarial reviewers (same role,
same objectives, same packet format, same "don't rubber-stamp"
expectation per DEV_NORMS §4). The choice of which one ran on a
given response is captured as a **provenance footer on the response
file**.

Concretely:

1. **`scripts/gemini_review.ps1` runs the cascade.** Default
   `-Provider auto` tries the chain in order, skipping any provider
   whose env-var key is unset, picking the first that returns a
   non-error response. `-Provider <name>` (one of
   `gemini|openrouter|groq|cerebras`) overrides the cascade and
   runs exactly one provider.

2. **Per-provider env vars:**
   - `GEMINI_API_KEY`     — https://aistudio.google.com/apikey
   - `OPENROUTER_API_KEY` — https://openrouter.ai/keys
   - `GROQ_API_KEY`       — https://console.groq.com/keys
   - `CEREBRAS_API_KEY`   — https://cloud.cerebras.ai/?tab=api-keys

3. **Provenance footer (audit signature).** Every
   `review_responses/<date>-<slug>.md` file ends with:
   ```
   ---
   _Generated by **<provider>** (`<model>`) on YYYY-MM-DD HH:MM:SS._
   ```
   The footer is added by the script after the response text is
   written. It is **load-bearing for audit**: a future reader can
   `grep "^_Generated by" review_responses/` and reconstruct which
   model wrote which response without re-running the cascade.
   Removing or editing the footer in a committed file is a process
   violation — the response should be regenerated if it needs
   changes, not patched.

4. **`-Model` parameter applies only to the FIRST provider in the
   chain.** Models named with Gemini's vocabulary (e.g.
   `gemini-2.5-flash`) are invalid for Groq, and llama IDs are
   invalid for Gemini. If a specific non-default model from a
   non-default provider is wanted, pair `-Model` with `-Provider`.

5. **Filename preservation.** The script remains named
   `gemini_review.ps1` despite its broadened scope. Renaming would
   break every past session log's commit-draft section (each
   commands `.\scripts\gemini_review.ps1 -Slug …`), break PO's
   muscle memory, and edit-of-an-older-file in a sense that
   violates DEV_NORMS' "don't edit older generated files going
   forward" stance on the 2026-06-02 conceptual shift. The script
   is a *well-known entrypoint*; the name is incidental to its
   scope. DEV_NORMS §4 and this ADR document the mismatch
   explicitly.

6. **Same packet format for all providers.** The review packet
   template's "Role for Gemini" wording is broadened to "Role for
   the reviewer model" so a non-Gemini provider doesn't receive a
   Gemini-specific identity prompt. The role itself ("adversarial-
   but-fair code reviewer for a portfolio project") stays
   identical across providers — that's the load-bearing constraint
   anchor, not the model name.

## Alternatives considered

### 1. Cascade vs explicit-only

**A. Cascade by default (the decision).** Most invocations should
"just work" — the PO shouldn't have to think about which provider is
healthy today. The cascade picks the first healthy provider and
prints which one to the console, so the audit trail (response
footer + commit log) is preserved automatically.

**B. Explicit provider per call (no cascade).** Forces the PO to
guess which provider is up before each review. Rejected: that's the
exact friction ADR 0001 promised to remove ("review invocation is
self-documenting in `scripts/`; no `gemini --help` archaeology").

**C. Cascade with parallel fan-out (call all providers, return all
responses).** Free-tier rate limits would chew through every
provider's quota in one session, and we don't actually want four
opinions per packet — we want one adversarial reviewer holding the
floor. Rejected on cost and on review-loop semantics.

### 2. Which providers in the chain

**A. Gemini + OpenRouter + Groq + Cerebras (the decision).** Four
independent capacity pools, all free-tier, all OpenAI-compatible
shape (three of four; Gemini's native shape is the fourth). Covers
the three failure modes empirically seen: vendor-side quota
exhaustion, vendor-side capacity event, and individual model-ID
drift.

**B. Claude / OpenAI as paid fallbacks.** Strong models, but
paid-API access risks the project's $0-lifetime-cost north star.
Could be added later if free-tier coverage proves insufficient —
ADR 0011 is the natural amendment point.

**C. Single backup (Gemini + one other).** Cheaper to maintain but
brittle against multi-provider outages (which happen — large model
hosts share infrastructure dependencies). Three free fallbacks is
not meaningfully harder to maintain than one.

**D. Replace Gemini entirely with a non-Google primary.** Rejected
on continuity: ADR 0001's $0-cost + workflow-fit argument still
holds for Gemini, and the 11+ historical review responses provide a
baseline calibration for Gemini's style. Adding fallbacks while
keeping Gemini as the default preserves that baseline.

### 3. Provenance discipline

**A. Per-response footer (the decision).** Inline in the file the
PO reads. Survives `git mv`, `git rebase`, file copies. Greppable
without context. Trivial to add (one line in the script).

**B. Sidecar `.meta.json` next to each response.** Cleaner
separation but two-files-per-review doubles the artifact count and
the sidecar is easy to drift from the response. Rejected on
maintenance.

**C. Git commit message captures it.** Fragile — commits get
squashed, rebased, regenerated. Doesn't survive a clean checkout
review. Rejected.

**D. No provenance, response file alone.** Would have been
acceptable for Gemini-only era when the answer was always "Gemini".
Multi-provider means we have to be able to tell apart "Gemini
reviewed this rigorously" from "Groq's llama-3.3-70b skimmed past
it on a quota-exhaustion day." The provenance is the audit
backbone of the conceptual shift.

### 4. Script renaming

**A. Keep `gemini_review.ps1` (the decision).** Treat the filename
as a well-known entrypoint with historical baggage; document the
scope mismatch in this ADR + DEV_NORMS §4. Past session-log
commit-draft sections (which name the file in their PowerShell
sequences) remain runnable verbatim. Cost: one paragraph of
explanation; benefit: zero churn across the project's git history.

**B. Rename to `review.ps1` (or `reviewer_loop.ps1`).** Cleaner
name; matches the broadened scope. Cost: every past session log's
commit-draft instructions go stale (PO would hit "command not
found" replaying any historical merge sequence); the
DEV_NORMS §7 canonical PowerShell sequence would need a sed sweep
across `docs/sessions/`. Rejected on PO directive #4 ("don't edit
older generated files").

**C. Rename with a symlink/wrapper from the old name.** Possible on
POSIX but messy on Windows (PO's primary). Symlinks need elevation
or developer mode. PowerShell function wrappers in profile would
hide the rename. Over-engineered for a one-word filename concern.

## Consequences

**Positive:**

- **Review loop resilience.** Quota exhaustion or single-vendor
  outage no longer blocks the session-done workflow (DEV_NORMS §8
  checkbox: "Review packet has been written and run through the
  reviewer-model loop").
- **Audit transparency.** The provenance footer makes the reviewer
  choice visible in every response file. `git blame` answers
  "which model wrote the harshest critique on this design?" in
  one query.
- **Zero re-keying overhead for OpenRouter's catalog.** OpenRouter
  is an aggregator — one `OPENROUTER_API_KEY` unlocks DeepSeek R1,
  Llama 3.3, Gemini-flash-exp, Qwen, etc. Model-ID drift inside
  OpenRouter is a `-Model` override, not a re-key event.
- **OpenAI-compatibility lifts a debugging burden.** Three of four
  providers share a request/response shape, so the diagnostic
  output (`Write-ApiError`) handles all three with one code path.
  Future provider additions in the same shape are ~5 lines of
  config in `$providers`.
- **Doesn't violate north star #1 ($0 cost).** All four providers
  have permanent free tiers covering project volume; no provider
  charges-on-overage by default.

**Negative:**

- **Reviewer-style variance across providers.** Gemini-Pro, DeepSeek
  R1, and Llama-3.3-70b have measurably different review postures —
  R1 leans toward exhaustive enumeration, Gemini toward terse
  pointed critique, Llama-3.3 toward middle-ground. The provenance
  footer makes this visible but doesn't eliminate it. PO and Claude
  should weigh the response style against the model that produced
  it when deciding which points are load-bearing.
- **Free-tier model IDs drift.** `deepseek/deepseek-chat-v3.1:free`
  was the OpenRouter default for two hours of this session before
  returning 404 ("No endpoints found"). OpenRouter rotates which
  exact models hold a `:free` slot. Mitigated by: the cascade falls
  through 404 the same way it falls through 429/503, and the
  documented default is `deepseek/deepseek-r1:free` (which
  OpenRouter committed to keep on free tier "for the foreseeable
  future" per the 2026-04-24 announcement). A future drift means a
  one-line edit to `$providers.openrouter.DefaultModel`.
- **Provenance footer adds a markdown convention to maintain.** A
  future agent editing a response file might delete the footer
  during a "clean up trailing whitespace" pass. Mitigated by: the
  footer is the LAST content in the file, so a sloppy edit removes
  it visibly; DEV_NORMS §4 explicitly names it as a process anchor.
- **The script name lies.** `gemini_review.ps1` no longer means
  "review via Gemini." Documented as deliberate (Decision #5); a
  recruiter cloning the repo reads ADR 0011 and DEV_NORMS §4 to
  decode the mismatch. Acceptable tax for not breaking historical
  command lines.

**Follow-ups:**

- **`scripts/gemini_review.sh` (bash parity)** — has NOT been
  updated to the cascade in this ADR's session. The .sh path is
  parity-only (CI / Git Bash), not the PO's daily driver. A future
  session can port the cascade to bash when the .sh is next
  invoked. ADR 0001 §References pointed at both; this ADR's
  implementation is `.ps1`-only.
- **Provenance footer-based dashboard.** Once the project has
  ~20 responses, a quick `grep` + count gives a "review-coverage by
  model" view that's portfolio-interesting (shows the cascade
  resilience worked in practice). Not built; the data accumulates
  naturally.
- **A 5th provider if free-tier coverage degrades.** Together AI,
  Cloudflare Workers AI, GitHub Models, Hugging Face Inference —
  several candidates exist. The `$providers` config map in the
  script makes additions ~5 lines + one env var.

## References

- **ADR 0001** — direct Gemini API for reviews. This ADR extends
  0001's "swap the CLI for direct REST" decision with a cascade
  across multiple direct-REST endpoints. ADR 0001 §Alternatives C
  ("Use a different LLM as reviewer — out of scope") is the
  deferred line this ADR closes.
- `scripts/gemini_review.ps1` — cascade implementation (272 lines
  after this ADR's session; `$providers` map at the top is the
  per-provider config).
- `DEV_NORMS.md §4` — renamed/broadened to **Reviewer-model loop**
  in the same commit that lands this ADR. Captures the env-var
  table, the cascade behaviour, and the provenance-footer rule as
  operational anchors.
- `templates/review_packet_template.md` — "Role for Gemini" → "Role
  for the reviewer model" in the same commit.
- `templates/session_log_template.md` — "Reviewer: Gemini (CLI)"
  broadened in the same commit.
- `templates/adr_template.md` — "Deciders: …, Gemini (reviewer)"
  broadened to "reviewer model".
- `context/dev_workflow.md` §v2 changes shipped — append-only entry
  for 2026-06-02 multi-provider cascade.
- First response under the cascade:
  `review_responses/2026-06-02-lambda_scorer-mvp.md` (footer:
  Generated by **groq** (`llama-3.3-70b-versatile`) — Gemini
  unavailable due to free-tier quota exhaustion).
- External: https://openrouter.ai/deepseek (current DeepSeek free
  model availability on OpenRouter), https://groq.com/pricing
  (Groq free-tier limits), https://cloud.cerebras.ai (Cerebras
  free-tier limits).

## Addendum 2026-06-10 — Collapse to a single provider (DeepSeek) + generic script name

- **Status:** Accepted (PO call 2026-06-10). This Addendum sets the
  operative review mechanism going forward; the cascade Decision above is
  retained as the historical record of the multi-provider era.

**Context.** At the 2026-06-10 live-apply wrap-up review, all four cascade
keys (`GEMINI_API_KEY`, `OPENROUTER_API_KEY`, `GROQ_API_KEY`,
`CEREBRAS_API_KEY`) were rotated/invalid — every provider returned
400/401/404. Rather than re-key four free tiers, the PO consolidated on a
single DeepSeek API key under the PO's control.

**Decision (the new norm).**

1. **Single provider: DeepSeek.** `$providers` carries one entry —
   `deepseek` → model `deepseek-reasoner` (R1), endpoint
   `https://api.deepseek.com/chat/completions`, env `DEEPSEEK_API_KEY`.
   DeepSeek is OpenAI-compatible, so it reuses the existing
   `Invoke-OpenAICompat` helper; the Gemini-native path (`Invoke-Gemini`)
   and the openrouter/groq/cerebras entries are removed. `-Provider auto`
   resolves to `deepseek`.
2. **Script renamed generic.** `scripts/gemini_review.{ps1,sh}` →
   `scripts/run_review.{ps1,sh}`. This reverses Decision #5 / Alternative
   §4-A ("keep `gemini_review.ps1`"): with Gemini no longer even in the
   chain, the vendor name in the filename actively misleads. The
   §Consequences negative "the script name lies" is resolved. Past session
   logs and review packets that spell the old name are dated records, left
   unedited; their command lines won't replay verbatim (accepted — we
   don't re-run historical merge sequences).
3. **Bash sibling brought to parity.** `run_review.sh` is updated to the
   DeepSeek-only shape (provenance footer included), closing the ADR 0011
   §Follow-up "`gemini_review.sh` has NOT been updated to the cascade."
4. **Provenance footer retained** (Decision #3) — now always `deepseek`.
5. **Local key handling.** The key lives in `scripts/review_keys.local.*`
   (gitignored, auto-sourced by the script) — never committed, never in
   the tracked script, so the `git-secrets` hook stays clean.

**Consequences.**

- **Cascade resilience is given up.** A DeepSeek outage/quota now blocks
  the review loop — the exact failure ADR 0011 was created to prevent.
  Accepted as a key-management simplification (one key, not four);
  re-adding providers is a ~5-line `$providers` edit + env var, and the
  full cascade is recoverable from git history.
- **North star #1 ($0) caveat.** The cascade was all free-tier; DeepSeek's
  direct API is paid-per-token (~fractions of a cent per review packet). A
  second small, knowing exception to literal-$0 in the spirit of ADR 0013
  — flagged for the record; PO owns the DeepSeek billing.
- **The name no longer lies** — `run_review` is scope-accurate.

**References.** Operationally supersedes the cascade in this ADR's
Decision; updates ADR 0001's filename premise. Implementation:
`scripts/run_review.{ps1,sh}`, `scripts/review_keys.local.ps1` (gitignored).
DEV_NORMS §4/§7 + `context/dev_workflow.md` updated to the new name in the
same commit.
