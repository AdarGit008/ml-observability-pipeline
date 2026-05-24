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

## Known deferred decisions (do not pre-optimize)
- No Makefile / `scripts/new_session.sh` yet — only add if scaffolding by hand becomes the bottleneck.
- No `.gitignore` in initial commit — add as a focused commit in the next session before any code lands.
- No LICENSE / README yet — add when the repo first goes in front of a recruiter (i.e. when there's something to look at).

## Interfaces (in / out)
- **In:** PO feedback about what's slowing sessions down.
- **Out:** Edits to `DEV_NORMS.md`, `context/*`, `templates/*`.

## Open questions
None blocking. Trigger conditions above cover when to revisit.

## Related ADRs
None yet. If we change the workflow significantly, write an ADR so future sessions know why.
