# dev_workflow

## Purpose
The meta-component. Use this when the session is about the development process itself — updating `DEV_NORMS.md`, revising templates, evolving the context system, retro on what's working and what isn't.

## Current state
- [x] Initial framework in place (this folder + `DEV_NORMS.md` + `templates/`).
- [x] **v1 locked.** Approved by PO 2026-05-24 after first end-to-end exercise (this session brief loaded `_global` + `dev_workflow` only; PLAN/HANDOFF/ACCOUNT_SETUP correctly skipped).
- [x] Scaffolding dirs created (`docs/adr/`, `docs/sessions/`, `review_packets/`, `review_responses/`).
- [x] Git initialized; remote pushed to GitHub (public, `ml-observability-pipeline`).
- Established: 2026-05-24.

## v1 → v2 trigger conditions
Bring this back to a `dev_workflow` session when **any** of these fire:
- After session #5: planned retro (see open questions).
- A session goes >2x over time and you can point at the framework as the cause (e.g. wrong tier of context loaded, template missing a field, Gemini loop felt like ceremony).
- An ADR-worthy decision is made about *how we work* (not what we build) — write the ADR, then update DEV_NORMS in the same session.

## v2 changes shipped (chronological)
- **2026-05-24 — Gemini loop:** Replaced `@google/gemini-cli` invocation with `scripts/gemini_review.{ps1,sh}` direct-API wrappers. Triggered mid-simulator-session by four CLI friction points (install, arg passing, trust folders, internal tool-call bug). See **ADR 0001**. PowerShell script then took four further debugging passes (encoding + cwd issues on PS 5.1) before running clean — all defended inline in the script.
- **2026-05-24 — `.gitignore` deferred-decision retired:** Landed Python + Terraform `.gitignore` as the first commit of the 2026-05-24 simulator session.
- **2026-05-25 — RPM-coupling spec deviation:** Not a dev_workflow change per se, but the first time we exercised the "PLAN.md ↔ ADR divergence" path from DEV_NORMS §9. See **ADR 0002**.
- **2026-06-02 — Multi-provider review cascade:** Extended the Gemini-only review path to a cascading provider chain `gemini → openrouter → groq → cerebras`, with per-response provenance footers as the audit signature. Triggered mid-lambda_scorer-MVP-review by a hard `429 RESOURCE_EXHAUSTED` from Gemini's free tier + a 503 on flash fallback. See **ADR 0011**. `scripts/gemini_review.ps1` keeps its filename despite the broadened scope (ADR 0011 §Decision #5).
- **2026-06-10 — Review trimmed to DeepSeek-only + script renamed:** Collapsed the cascade to a single provider (DeepSeek `deepseek-reasoner`, OpenAI-compatible) after all four free-tier keys were rotated/invalid; renamed `scripts/gemini_review.{ps1,sh}` → `scripts/run_review.{ps1,sh}` (the vendor name no longer fit). Key in gitignored `scripts/review_keys.local.ps1`. See **ADR 0011 §Addendum 2026-06-10**.

## Known deferred decisions (do not pre-optimize)
- No Makefile / `scripts/new_session.sh` yet — only add if scaffolding by hand becomes the bottleneck.
- No LICENSE / README yet — add when the repo first goes in front of a recruiter (i.e. when there's something to look at). Also fold the one-time reviewer-model setup (API keys for whichever providers in the ADR 0011 cascade the PO has accounts on) into this when it lands.

## Interfaces (in / out)
- **In:** PO feedback about what's slowing sessions down.
- **Out:** Edits to `DEV_NORMS.md`, `context/*`, `templates/*`.

## Open questions
None blocking. Trigger conditions above cover when to revisit.

## Related ADRs
- **ADR 0001** — direct Gemini API for reviews (workflow change).
- **ADR 0011** — multi-provider review cascade + provenance audit (2026-06-02 extension of ADR 0001).
