This is a stellar analysis of the operational reference rollout. The carry-in from ADR 0007 has clearly paid off for the raw features—those PSI drops are massive and definitively prove the dwell-stretch was the culprit.

The rolling-features issue is a classic observability trap, and your diagnosis is spot on. Let's break down the questions.

## Q1 — Rolling-features PSI & Autocorrelation

The structural finding is 100% correct. Rolling statistics computed with a 149/150 overlap are massively autocorrelated. They violate the Independent and Identically Distributed (IID) assumption that PSI relies on. An 1800-tick window of a 5-minute rolling mean isn't a representative sample of the feature's entire probability distribution; it's a slow, narrow random walk through a tiny slice of it. When PSI compares this narrow, clustered test window against a broad, multi-pump reference distribution, it correctly detects that the shapes don't match, interpreting the test sample's lack of variance as a "shift." Adding more reference pumps just makes the reference broader, exacerbating the mathematical mismatch.

**Remediation: Option 2 is the definitive winner.**

**Drop the rolling features from the PSI surface entirely.**

- **Why:** PSI is a measure of data distribution shift. The underlying physical system's state is fully represented by the raw features. If the system drifts, the raw features drift, and PSI will catch it. The rolling features are deterministic mathematical derivatives injected strictly to give the inference model temporal context. Monitoring them for drift via PSI is redundant at best and, as you've seen, mathematically noisy at worst.
- **The Interim Posture:** The raw-strict / rolling-soft split is a perfectly pragmatic stopgap to unblock this session, but yanking the rolling features from the drift scorer's config in a follow-up session is the cleanest long-term architectural move.

## Q2 — Single-reference vs dual-reference architecture

**The single-reference choice is still correct.**

A dual-reference architecture would not fix the Q1 issue. Even if you compared the rolling features against a dedicated training baseline, the 1800-tick test window would still be a narrow autocorrelated walk compared to the global training distribution. You'd still get false WARNINGs. Stick to ADR 0008: one operational reference for drift detection, keep the architecture simple.

## Q3 — Operational sample: 5 pumps vs 15 pumps

While 5 pumps gives you a mathematically sufficient 9,000 samples, **bumping to 15 is the better call.**

It costs practically nothing for an offline generation script, and it tightly aligns the baseline with the explicit definition of your healthy demo fleet. When a future developer (or you, six months from now) asks, "What exactly does this reference represent?", the answer "The exact baseline output of the 15-pump demo fleet" requires zero mental translation. It doesn't solve Q1, but it's cleaner for narrative and operational parity.

## Q4 — Warm-up skip in `_generate_operational_samples`

**Your semantic is correct: skip the warm-up.**

The operational reference must define the steady-state. If you bake cold-start anomalies into the reference distribution, you artificially widen the reference bins. This "diluted" reference would become less sensitive to genuine, subtle steady-state drift down the line. It is far better to have a highly accurate steady-state reference and rely on your dashboard's `last` aggregator (ADR 0007 §5) to absorb the expected cold-start noise.

## Q5 — Two profile dicts in `model/train.py`

**It needs a banner.**

A module docstring will absolutely get lost in a 700-line file. Since this intentional asymmetry is the primary directive of ADR 0008, it needs to be unmissable. Place a loud, multi-line comment block (e.g., `### WARNING: ASYMMETRIC PROFILES BY DESIGN (ADR 0008) ###`) directly above the dictionary definitions.

## Q6 — Sandbox-runtime corpus (12 pumps) committed

**This is an acceptable workflow, but needs explicit documentation.**

Committing a functional, truncated artifact as a CI/CD proof-of-correctness is standard practice when constrained by sandbox timeouts. However, it absolutely needs a loud warning in the session log and ideally a `README.md` note in `model/artifacts/` stating: "Committed model is a 12-pump sandbox build for pipeline validation. Production canonical artifacts are regenerated natively at 30 pumps."

## Q7 — Old reference not deleted in this session

**Totally acceptable.**

FUSE mount limitations on Windows are a hard physical constraint of your current environment. Batching the `git rm` into the PO's native execution step is the correct pragmatic workaround. It doesn't block the ADR; it's just an operational sequencing detail.

---

## Follow-up from Gemini

Given that dropping rolling features from the PSI surface seems like the cleanest path forward for Q1, does your current dashboard architecture easily support rendering PSI metrics for a subset of features while ignoring others, or will that require a tweak to the UI layer as well?
