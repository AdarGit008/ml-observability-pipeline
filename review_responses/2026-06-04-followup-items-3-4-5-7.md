This is an adversarial-but-fair review of the 2026-06-04 follow-up items.

### Summary Disposition
The changes successfully close the technical debt from ADR 0008/0009. The "loud" documentation strategy is a pragmatic response to the non-obvious design asymmetries required by the project constraints. The scope expansion in Item 7 was the correct engineering call over rigid adherence to a session brief.

---

### Response to Specific Questions

**1. Item 3 ΓÇö ADR 0008 amendment or session-log note?**
**Recommendation: ADR 0008 Amendment.**
The jump from 5 to 15 pumps is framed as "narrative alignment" (matching the demo fleet count). Since ADR 0008 ┬ºDecision.2 and ┬ºAlternatives.2.A explicitly codify the "5 pumps" decision, leaving the ADR at 5 while the code is 15 creates a "stale spec" smell.
*   **Action:** Add a one-line "Amended 2026-06-04" note to ADR 0008 ┬ºDecision.2 noting the shift to 15 pumps for fleet-count alignment. It preserves the "why" while keeping the "what" accurate.

**2. Item 4 ΓÇö Banner placement vs. existing docstrings.**
**Disposition: Keep banners as-is.**
While redundant, these specific asymmetries (`_training` vs `_operational` profiles and 8-feature vs 4-feature surfaces) are the most likely points of failure for a future "cleanup" pass.
*   **Rationale:** A module-level banner is too easily scrolled past. The current placement acts as a "tripwire" exactly where a developer would attempt to "harmonize" the logic.
*   **Risk Mitigation:** To prevent "banner drift," ensure the banners cite the ADRs (which you did). The ADR remains the authoritative source; the banner is the warning sign.

**3. Item 7 ΓÇö Scope expansion (shared/drift.py edits).**
**Disposition: Correct call.**
The constraint "no edits to `shared/`" was clearly intended to protect the parity contract (signatures/logic).
*   **Defense:** String-only updates to docstrings and error messages (`reference_distribution.json` ΓåÆ `operational_reference_distribution.json`) are **maintenance**, not **refactoring**.
*   **Consequence:** If you had punted this, the system would produce a `DriftError` that cites a non-existent file, causing a "where is my artifact?" tail-chase for the user. Pragmatism wins here.

---

### Adversarial Observations

#### 1. The "15 vs 30" Pump Ambiguity
In `model/train.py`, `OPERATIONAL_REFERENCE_PUMPS` is set to 15. The `model/artifacts/README.md` mentions the "production canonical is regenerated natively at 30 pumps."
*   **The Risk:** If the PO runs the script natively, they have to remember to pass `--n-pumps 30`. If they just run `python -m model.train`, they get a 15-pump reference.
*   **Recommendation:** If 30 is the canonical production count, why is the constant 15? If 15 is the "demo fleet" and 30 is the "training corpus," the constant name `OPERATIONAL_REFERENCE_PUMPS` is correct for 15, but the README's mention of 30-pump regeneration for artifacts is confusing. Clarify if the "canonical" artifact should be 15 or 30.

#### 2. Item 5 ΓÇö README.md "Sandbox vs Production"
The distinction in `model/artifacts/README.md` is excellent and satisfies North Star #2 (Single-PC development).
*   **Minor Critique:** You mention the "45-second bash cap" in the README. This is a very meta, session-specific constraint.
*   **Alternative:** Phrase it as "resource-constrained environments (e.g., CI/CD or sandbox mounts)" to make the README more durable as a portfolio piece, rather than citing the specific bash timeout of the current tool.

#### 3. HANDOFF.md Cleanup
Converting `HANDOFF.md.docx` to GFM is a major improvement. Binary files in a repo are a "portfolio smell."
*   **Observation:** The diff shows `HANDOFF.md` still contains section numbering like `6. Q4` and `8. Current execution status`. If this was a pandoc artifact, ensure the internal links (if any) still work in GFM.

### Final Verification Result
*   **North Star #4 (Parity):** Maintained. String edits in `shared/drift.py` do not affect the execution contract.
*   **North Star #5 (One polished repo):** Significantly improved by the artifact documentation and the `HANDOFF.md` recovery.
*   **Tests:** 350 passed + 1 skipped (confirmed constant).

**Proceed to resolution.** I recommend the ADR 0008 amendment mentioned in Point 1 as the final cleanup step.
