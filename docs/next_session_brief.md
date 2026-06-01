# Next session — opening prompt

> Paste this as the FIRST message of the next session, after pasting the standard session brief.

## Step 1 — Quiz first

Before writing any code, quiz me on essential project knowledge. Rules:

- 10 questions total. Ask one at a time.
- After each answer, tell me whether it's correct, partially correct, or wrong, and supply the right answer. Do not move to the next question until I respond.
- Be strict on architectural / parity / cost questions; lenient on phrasing for definitions.
- After all 10, give a total score (X/10) and a one-sentence summary: "context loaded clean" vs. "the following gaps surfaced: …".
- Do NOT touch tools (Read, Edit, Bash, etc.) during the quiz — answer only from memory + the auto-loaded MEMORY.md / DEV_NORMS / `context/_global.md`. If I push back on a wrong-judgement, then verify against the repo.

### The 10 questions (ask in this order)

1. **Cost & guardrails.** What's the project's hard lifetime AWS spend ceiling, and what AWS service categories are on the never-deploy list? Why is each excluded?

2. **North stars.** Name the six north stars from `context/_global.md`. Which one is the "if violated, treat as a bug" rule that drove ADR 0005?

3. **Tech locks.** Why was Timestream replaced with DynamoDB, and why was Kinesis Firehose replaced with a Lambda + EventBridge batcher? (Cost reason for each.)

4. **Mode parity — semantics.** State the mode-parity invariant. Be precise about what it IS (output correctness under same inputs) and what it is NOT (concurrency model). Cite the source.

5. **Mode parity — location.** Where does the parity-shared logic physically live in the repo? Name the three files and the package. Why isn't it under `lambda_scorer/` or `local_runtime/`? What does ADR 0005 say about the alternative layouts I considered?

6. **The 8 features.** List the 8 features extracted by `shared.features.extract_features` (per PLAN.md §2.3). Which 4 are raw signals from the simulator? Which 2 rolling stats are computed, and over what window?

7. **PSI thresholds.** What are the three PSI threshold bands (per PLAN.md §2.7) and what triggers an SNS alert?

8. **Context-loading tiers.** Name all four tiers in DEV_NORMS §5. What specifically triggers Tier 2b loading? Which components are currently in the parity set?

9. **MQTT topology.** Why does each pump get its own MQTT connection in the simulator (ADR 0003), and why does the local_runtime subscriber have just ONE connection on a wildcard topic (ADR 0005)? What's the asymmetry?

10. **Current blockers.** What's the open HANDOFF.md §6 question that blocks the lambda_scorer session from starting? What's the open drift-session question that ADR 0005 surfaced during Gemini review?

## Step 2 — Continue development

After the quiz, ask me which component is next. Options on the table (in rough order of "most natural next step"):

- **model** — implement `shared.score.score` with the real `HistGradientBoostingClassifier`. Removes a stub. Mode-parity safe by construction (interface locked).
- **dashboards** — Grafana panels against the InfluxDB schema pinned by ADR 0005. Uses the data local_runtime is now writing live. Visible recruiter signal.
- **drift** — implement `shared.drift.compute_psi` with real binned percentages + Laplace smoothing. Removes a stub. Resolves the PSI write-cadence open question.
- **lambda_scorer** — BLOCKED on HANDOFF.md §6 Q5 (DynamoDB schema). Resolve Q5 first or pick another component.

Once I pick, I'll write a session brief in the standard template (DEV_NORMS §5 Tier 2b applies to model/drift/dashboards/lambda_scorer — make sure `Loads:` includes `shared/` source + ADR 0005). You'll plan-then-approve before any code.

## Reminder of process

- Per DEV_NORMS §3: no code without an approved plan.
- Per the parity-load check in auto-memory: if my brief is for a parity-touching component and omits Tier 2b loads, STOP and ask me to revise.
- Per [[ml_obs_pipeline_git_on_windows]]: all git ops on my side (Windows).
