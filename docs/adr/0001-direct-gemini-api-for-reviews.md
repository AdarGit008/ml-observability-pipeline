# ADR 0001 — Use the Gemini REST API Directly for Code Reviews; Bypass the Gemini CLI

- **Status:** Accepted
- **Date:** 2026-05-24
- **Deciders:** PO (Adar), Claude (architect)

## Context

`DEV_NORMS.md §4` specifies a Gemini-based code-review loop: Claude writes a `review_packets/<date>-<slug>.md`, the PO pipes it through `gemini -p "$(cat ...)"`, and the response lands in `review_responses/`. The first session that actually needed this was the 2026-05-24 simulator session.

Running it surfaced four discrete points of friction, all from the CLI rather than from the review need itself:

1. **Install ceremony.** `npm install -g @google/gemini-cli` plus API-key env-var setup is one-time but undocumented; PO hit "command not recognized" mid-session.
2. **Argument passing.** `gemini -p "$(cat ...)"` is bash idiom; PowerShell `Get-Content -Raw ... | gemini -p` confused the CLI into "Cannot use both a positional prompt and the --prompt flag together."
3. **Trusted-folders gating.** CLI refused to run in the project directory without `GEMINI_CLI_TRUST_WORKSPACE=true` or `--skip-trust`.
4. **Internal tool-call bug.** Once running, the CLI's *agentic* layer crashed with `Error executing tool update_topic: params must have required property 'strategic_intent'` — a bug in the CLI's own tool schema, not user-fixable.

The review use case is single-shot: prompt in → text out. None of the CLI's value-add (interactive sessions, tool use, file editing) is needed. The friction is paying for capability we don't use.

Constraint anchors that bear on this (per `context/_global.md`):
- **$0 lifetime cost.** Both paths hit the free tier of Generative Language API — no cost difference.
- **Single-PC dev.** Both paths run locally — no difference.

## Decision

Replace the Gemini CLI with a thin script that POSTs the review packet to the Generative Language REST API and writes the response. Two parity files: `scripts/gemini_review.ps1` (PowerShell, primary — PO is on Windows) and `scripts/gemini_review.sh` (bash, parity for CI / Git Bash).

The script takes `-Slug` (and optional `-Model`, `-Date`), reads `review_packets/<date>-<slug>.md`, POSTs to `https://generativelanguage.googleapis.com/v1beta/models/<model>:generateContent`, and writes the response to `review_responses/<date>-<slug>.md` as UTF-8.

`DEV_NORMS.md §4` is updated to point at the script. The only one-time setup remaining is `$env:GEMINI_API_KEY`.

## Alternatives considered

**A. Keep using the Gemini CLI; document the workarounds.** Add a `SETUP.md` describing the npm install, the `GEMINI_CLI_TRUST_WORKSPACE` env var, the PowerShell `Get-Content -Raw | gemini` invocation, and the model fallback for 503s. Rejected: doesn't fix friction point 4 (the `update_topic` bug), which is opaque to us and recurs on CLI updates. Documenting workarounds for a tool we don't fundamentally need is the wrong direction.

**B. Use the official Python SDK (`google-generativeai`) with a wrapper script.** Equivalent capability, mature SDK, good error messages. Rejected for marginal reasons: adds a Python dependency *just for the review path* (the rest of the project also uses Python, but adding `google-generativeai` to project requirements for one auxiliary script feels heavy), and offers nothing over a 40-line REST call for a single-shot use case. Easy to revisit if the script grows.

**C. Use a different LLM as reviewer (Claude API, OpenAI).** Out of scope for this ADR — the choice of *which* reviewer is orthogonal to *how* we invoke it. If we later switch reviewers, we rewrite the script; the rest of the workflow is reviewer-agnostic.

**D. Drop adversarial review entirely.** Rejected on principle — the review loop is the project's main defense against Claude rationalizing past trade-offs, per `DEV_NORMS.md §4` ("Gemini's job is not to rubber-stamp"). Single-PC, single-architect projects need *something* in this slot.

## Consequences

**Positive:**
- One install gone (no Node/npm requirement).
- One env var gone (`GEMINI_CLI_TRUST_WORKSPACE` not needed via REST).
- Argument-passing issue gone — script handles it.
- Capacity fallback baked in: 503 on Pro prints the exact command to retry with Flash.
- Review invocation is now self-documenting in `scripts/`; no `gemini --help` archaeology.
- Script is reviewer-agnostic enough that swapping providers is a 1-file change.

**Negative:**
- We own a small script (~50 lines × 2 shells). Future Gemini API changes (auth, request shape) are now our problem. Mitigated by: REST API is more stable than CLI tool schemas, and the script is small.
- No multi-turn — single shot only. Acceptable: review packets are designed as single-shot artifacts (see `templates/review_packet_template.md`).
- No streaming output. Acceptable: review responses are written to a file, not read live.

**Follow-ups:**
- One-time setup checklist (npm-free) belongs in a future `SETUP.md` or `README.md` when the repo first goes in front of a recruiter (deferred per `context/dev_workflow.md` known deferred decisions).
- If a second LLM provider gets evaluated, factor `gemini_review.ps1` / `.sh` into a thin provider-agnostic entry point.

## References

- `DEV_NORMS.md §4` — review loop (now references the script).
- `context/dev_workflow.md` — v1→v2 trigger conditions; this ADR records the v2 fix.
- Session log: `docs/sessions/2026-05-24-simulator-pump-model.md` — captures the four friction points in real time.
- Gemini REST API docs: https://ai.google.dev/api/generate-content
- Gemini API keys: https://aistudio.google.com/apikey

## Addendum 2026-06-02 — Multi-provider extension (ADR 0011)

The 2026-06-02 lambda_scorer MVP review hit a hard `429 RESOURCE_EXHAUSTED` from Gemini's free tier (daily quota, not minute-scoped) followed by a 503 UNAVAILABLE on the flash-model fallback. Both failure modes were anticipated abstractly by this ADR but didn't have a recovery path: the script would exit non-zero, the session-done workflow stalled.

**ADR 0011** closes the deferred line in §Alternatives C of this ADR ("Use a different LLM as reviewer — out of scope") by extending `scripts/gemini_review.ps1` with a cascading multi-provider chain: `gemini → openrouter → groq → cerebras`. All four providers serve as adversarial-but-fair reviewers (same role, same packet format, same "don't rubber-stamp" expectation per `DEV_NORMS §4`). The choice of which one ran on a given response is captured as a **provenance footer** on the response file (load-bearing audit signal, per ADR 0011 §Decision #3).

This Addendum does not supersede the original Decision. The Gemini REST API is still the default first attempt; the script's filename is preserved (`gemini_review.ps1`) because it's a well-known entrypoint named across every past session's commit-draft sequence. ADR 0001's Consequences §"Capacity fallback baked in" originally meant "fall to Flash on 503"; ADR 0011 generalises this to "fall through providers on any error" — same posture, broader scope.

See ADR 0011 for: the full provider chain, free-tier model defaults, env-var names, the provenance-footer convention, and the rationale for keeping the original filename.
