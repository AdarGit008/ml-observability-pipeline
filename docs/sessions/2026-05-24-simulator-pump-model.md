# Session 2026-05-24 — simulator — pump-model-and-state-machine

- **PO:** Adar
- **Architect:** Claude
- **Reviewer:** Gemini (CLI) — pending
- **Context loaded:** `_global`, `simulator`, `_interfaces` (Tier 3 — telemetry dict is a cross-component contract)
- **Duration:** ~1h

## Intent
Implement the physical model + four-state lifecycle for `simulator/pump.py` per PLAN.md §2.2. Definition of done: `Pump` class with `.step()` returning a telemetry dict; unit tests cover all four states. No MQTT.

## What changed
- `.gitignore` — Python + Terraform + project secrets. First commit of the session per `context/dev_workflow.md` deferred-decisions.
- `simulator/__init__.py` — exports `Pump`, `PumpState`.
- `simulator/pump.py` — `Pump` class, `PumpState` enum, `StateProfile` dataclass, `DEFAULT_PROFILES`, ISO-8601-ms timestamp helper.
- `simulator/tests/__init__.py` — empty package marker.
- `simulator/tests/test_pump.py` — 29 tests, all passing in ~0.06s.
- `review_packets/2026-05-24-simulator-pump.md` — 7 sharp questions for Gemini.

PR: TBD — Adar opens after the Gemini loop.

## Decisions
None ADR-worthy yet. Six design choices made on top of §2.2's silence, all surfaced in the review packet for Gemini scrutiny:

1. **Degradation trajectory** — per-state `rate_per_tick + ceiling`, linear accumulation, clamped `[0, 1]`.
2. **FAILED behavior** — keep emitting telemetry with `degradation` pinned to 1.0 (so downstream scoring/drift sees the failure).
3. **State advancement** — both automatic dwell-based and manual `force_state()`.
4. **Seeded RNG** — pump owns `random.Random(seed)`. Deterministic tests.
5. **Tick order** — advance degradation → sample → check auto-transition. (Transition takes effect on the *next* tick.)
6. **Lockfile policy in `.gitignore`** — `.terraform.lock.hcl` is *not* ignored (HashiCorp guidance).

If any of these survives Gemini review and a follow-up session formalizes it, write an ADR then.

## Trade-offs surfaced
- **Linear-ramp degradation vs. data-calibrated profile.** Picked linear ramp because `simulator.md` Q2's default is first-principles. Calibration against NASA IMS / Case Western Reserve is deferred — flagged for Gemini as question #5.
- **RPM independent of degradation.** Strict §2.2 reading: `RPM = setpoint + N(0, 5)`. A FAILED pump still spins at 1800 RPM, which is physically odd. Stayed literal to §2.2 but flagged as question #3 — may warrant an ADR + spec update.
- **Tier-3 context load.** Brief declared `_global` + `simulator`. I added `_interfaces.md` because `.step()`'s return shape *is* the simulator→scorer contract. Asked PO before loading and got approval — process worked as intended.
- **FAILED emits telemetry rather than stops.** Decided based on what downstream needs: a stopped pump starves the scorer. Real failed pumps don't typically go silent — they vibrate, run hot, and trip protection. Defensible on physics grounds.

## Gemini review highlights
Pending. Run the loop next:

```
gemini -p "$(cat review_packets/2026-05-24-simulator-pump.md)" > review_responses/2026-05-24-simulator-pump.md
```

Then I'll fill in the Resolution table in the packet and update this section.

## State at end of session
- Tests: **29 passing** locally.
- Caveat: pytest cannot be run from the sandbox FUSE mount (cleanup of `.pytest_cache` infinite-recurses, same family as the git-on-Windows issue). Workaround used during this session: copy `simulator/` to `/tmp` and run from there. PO running pytest from Windows on `D:\` directly is unaffected.
- Open follow-ups (carry forward):
  - Run the Gemini review loop and address the 7 questions.
  - Question #7 calls out a dead-instantiation in `test_degrading_caps_at_ceiling` — clean up either way after Gemini's read.
  - `context/simulator.md` "current state" updated below; open questions on calibration + concurrency still stand.
- `context/<component>.md` updated: yes — see `context/simulator.md` change in this commit.

## Commits to run (PO-side, per [[ml-obs-pipeline-git-on-windows]])

**Commit 1 — `.gitignore` first, before any code (dev_workflow.md deferred-decision):**

```powershell
cd "D:\Claude\ML Observability Pipeline"
git add .gitignore
git commit -m "chore: add Python + Terraform .gitignore

First commit of the simulator session. Per context/dev_workflow.md
deferred-decisions, .gitignore lands before any code. Includes project
secrets path (simulator/.secrets/) and keeps .terraform.lock.hcl tracked
per HashiCorp guidance."
```

**Commit 2 — the pump model + tests + review packet + this session log + context update:**

```powershell
git add simulator/ review_packets/2026-05-24-simulator-pump.md docs/sessions/2026-05-24-simulator-pump-model.md context/simulator.md
git commit -m "simulator: add Pump physical model + 4-state machine

Implements PLAN.md §2.2 equations and the HEALTHY → DEGRADING →
FAILING → FAILED lifecycle. Pump.step() returns a telemetry dict
matching context/_interfaces.md; 29 pytest tests cover all four
states, transitions, RNG reproducibility, and ISO-8601-ms timestamp
formatting. No MQTT, no asyncio, no YAML config yet — separate
sessions. Six design choices surfaced for Gemini review (see
review_packets/2026-05-24-simulator-pump.md)."
git push
```

After push, run the Gemini review:

```powershell
gemini -p "$(cat review_packets/2026-05-24-simulator-pump.md)" > review_responses/2026-05-24-simulator-pump.md
```

## Note for next session
Pump model is in but **not Gemini-reviewed**. Before any *new* simulator work (config loading, MQTT publishing, scenario scripting), close out the Gemini loop on this packet and apply its findings. If Gemini pushes hard on question #3 (RPM-independent-of-degradation) or #5 (dwell-time calibration), those probably want ADRs. The dead instantiation in `test_degrading_caps_at_ceiling` (question #7) is a small cleanup — handle in the same follow-up commit.
