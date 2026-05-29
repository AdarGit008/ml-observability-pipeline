# context/ — modular session context

This folder holds the per-component context files that Claude loads at the start of a session. Read `DEV_NORMS.md §5` for the loading model. TL;DR:

- `_global.md` — always loaded (Tier 1).
- `<component>.md` — load exactly one per session (Tier 2).
- `shared/` source + ADR 0005 — load ADDITIONALLY if the component touches the parity boundary (Tier 2b — see `DEV_NORMS.md §5` for the rule and the parity set).
- `_interfaces.md` — loaded when work crosses components (Tier 3).

## File contract

Every component file follows the same shape so they stay small and skimmable:

```
# <component>

## Purpose
One paragraph. What this component does, where it sits in the architecture.

## Current state
- [ ] Not started / In progress / Done
- Brief: what exists, what doesn't

## Interfaces (in / out)
What this component consumes and produces. Reference _interfaces.md for full schemas.

## Open questions
Bullet list of unresolved decisions. Each links to a tracking issue or PR.

## Related ADRs
- ADR 000X — <title>
```

## Rules

1. **Target ≤ 5 KB per file.** If a component context exceeds 5 KB, split out the noise into an ADR or move to `_interfaces.md`.
2. **Update at end of session** if anything in the file changed. Stale context is worse than no context.
3. **Link, don't duplicate.** Schemas live in `_interfaces.md`. Rationale lives in ADRs. Component files reference them.
4. **No prose dumps from PLAN.md.** PLAN.md is the source of truth for the overall plan; component files describe the *current state* of one component.
5. **Parity-touching components flag the load explicitly.** If the component imports from `shared/`, its file's "Related ADRs" section MUST link ADR 0005, and a "Mode parity invariant" section MUST exist (see `context/local_runtime.md` for the canonical example).
