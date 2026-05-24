# Review Packet YYYY-MM-DD — <component> — <slug>

> Paste this entire file into Gemini via:
> `gemini -p "$(cat review_packets/YYYY-MM-DD-<slug>.md)" > review_responses/YYYY-MM-DD-<slug>.md`

## Role for Gemini
You are an adversarial-but-fair code reviewer for a portfolio project. Your job is not to rubber-stamp. Surface risks, design weaknesses, and trade-offs that the author may have rationalized past. Cite specific files and lines when possible.

## Project north stars (constraint anchors)
1. $0 lifetime AWS cost.
2. Single-PC development.
3. AWS-specific differentiation.
4. Mode parity between local and AWS demo paths.
5. One polished repo, not five half-finished ones.

Full constraint set: `context/_global.md`. Full plan: `PLAN.md`.

## Summary of the change
One paragraph. What was done, why, and which component it touched.

## Diff
Paste the unified diff inline, or list the changed files with one-line descriptions if the diff is too long. Prefer inline for diffs <500 lines.

```diff
<paste here>
```

## Specific questions for Gemini
Be explicit. Vague packets get vague reviews.

1. <Question 1 — e.g., "Is the PSI smoothing strategy correct under sparse-bin conditions?">
2. <Question 2 — e.g., "Does the DynamoDB write pattern risk hot partitions at 15-pump scale?">
3. <Question 3 — e.g., "Is anything in this change unnecessarily AWS-specific where local would suffice?">

## What I'm NOT looking for in this review
(Optional — prevents wasted critique.)
- E.g., "Style / formatting — handled by linter."
- E.g., "Test coverage — separate PR coming."

## Resolution (filled in by Claude after Gemini responds)

| Gemini point | Disposition | Notes |
|---|---|---|
| 1. <summarize> | Addressed / Deferred / Rejected | <where, why> |
| 2. ... | ... | ... |
