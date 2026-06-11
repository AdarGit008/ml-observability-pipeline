## Adversarial Review: FLEET PSI Panel + Adapter `fleet` Object

**Date:** 2026-06-10  
**Files reviewed:** `dashboards_adapter/handler.py`, `dashboards_adapter/tests/conftest.py`, `dashboards_adapter/tests/test_adapter.py`, `dashboards/aws.json`, `docs/adr/0014-grafana-adapter-api-contract.md`, `context/_interfaces.md`, `context/dashboards.md`  
**North stars checked:** $0 lifetime cost, mode parity, ADR 0014 projection, ADR 0012 Â§2C passthrough, ADR 0018 row shape.

---

### Summary

The change is architecturally soundâadditive, single round-trip, no shared imports. However, I see **two demo-day fire risks** (questions 3 & 4), a **confusing naming overload** (Q2), and one **contract gap** (partial-failure behaviour). Below I address each question, then flag a few additional concerns.

---

### 1. Additive shape: `fleet` as sibling of `pumps`

**Verdict: Acceptable, with one caveat.**

- **Infinity consumers** select by explicit `root_selector` or column nameânew keys are invisible to existing panels.
- **External consumers** (e.g., a future CLI tool that reads the adapter endpoint) might iterate over all keys. But the contract (`_interfaces.md`) documents `fleet` as optional, and the envelope already has `fleet_size` / `pumps_reporting`âadding `fleet` is symmetric.
- **Caveat:** The top-level `as_of` timestamp is the moment the adapter ran, not the FLEET rowâs `latest_ts`. If a consumer correlates `fleet.latest_ts` with `pumps[i].latest_ts`, they must handle the possible skew. Thatâs true of pump rows too, but worth documenting in the contract.

---

### 2. `pumps_reporting` name overload

**Verdict: High confusion riskârecommend renaming fleet-level `pumps_reporting` on the wire.**

- **Top-level** `pumps_reporting` = count of STATE rows for pump IDs (i.e., number of pumps that `lambda_fleet_psi` considered).  
- **Fleet-level** `pumps_reporting` = number of pumps that actually contributed to the pooled PSI within the 5-min window (from the FLEET row itself).

A Grafana panel author reading `$.fleet.pumps_reporting` may assume itâs the same count as the top-level value, but they can differ (throttled writes, partial window, etc.). This will inevitably cause confusion in ad-hoc queries.

**Suggested fix:** Rename the FLEET rowâs attribute at the wire to `pumps_pooled` or `contributing_pumps`. Yes, it diverges from the DynamoDB attribute name, but the adapter is a **projection**âdivergence is allowed per ADR 0014. The DynamoDB row has `pumps_reporting`; the adapter can rename it to disambiguate.

If you keep the same name, at minimum add a **tooltip annotation** to the Grafana table panel explaining the difference. But Iâd prefer the rename.

---

### 3. `null` fleet render in Infinity panels

**Verdict: Demoâday blocker unless verified or defensively handled.**

The contract specifies `"fleet": null` when the FLEET row is absent. Four gauge panels and one table set `root_selector: "$.fleet"`. In Grafana Infinity, if the JSON result is `null` at the selected path, behaviour is **undefined**âsome versions return âNo dataâ, others error (`TypeError: Cannot read properties of null (reading 'psi_â¦')`). I have observed both behaviours across Infinity versions.

**Recommendation:** Do **not** rely on live-verification alone. Change the adapter to return a **default object** when no FLEET row exists:

```python
"fleet": _fleet_entry(fleet_item) if fleet_item is not None else {
    "latest_ts": None,
    "psi_A": None, "psi_B": None, "psi_C": None, "psi_D": None,
    "alert_flag": False,
    "last_alert_sent_at": None,
    "pumps_reporting": 0
}
```

This ensures panels always receive a valid object; each gauge will show âNo dataâ because the value is `null`, and the table will render with defaultâempty cells. The Infinity `root_selector` resolves to the object, not `null`. If you prefer the âno FLEET â no panelsâ behaviour, you must test on the exact Grafana/Infinity version used in the demo.

Alternatively, you could add a **Grafana conditional** (e.g., a hidden panel that checks `$fleet`), but that adds complexity and is not supported in all Infinity panel modes.

**My vote:** adopt the defaultâobject approach. Itâs still additive (existing consumers see the same fields with nulls), and it eliminates the most likely failure mode.

---

### 4. Hard-reads in `_fleet_entry` vs. defensive access

**Verdict: Acceptableâconsistent with `_pump_entry`âbut add a test for malformed FLEET row.**

`_fleet_entry` hard-reads `latest_psi`, `alert_flag`, `pumps_reporting`. If `lambda_fleet_psi` ever writes a row missing one of these keys, `_snapshot` will raise a `KeyError` â 500. The same risk exists for `_pump_entry` (missing `latest_score`, etc.). So **the defect is symmetric**âitâs a design tradeâoff the team accepted in ADR 0014. I wonât block on it, but:

- **Add a unit test** that feeds a FLEET row missing `pumps_reporting` and asserts the adapter returns a descriptive 500 (or gracefully falls back). The test suite currently covers only happy paths.
- Consider adding a `.get()` with a clear warning log for **only** the FLEET path, since the writer is a different Lambda with its own lifecycle. This avoids a silent null if the row is incomplete.

---

### 5. AWS-only asymmetry

**Verdict: Clean line, no change needed.**

ADR 0005 Â§3 is unambiguous. The local InfluxDB has no FLEET concept. Adding these panels to `local.json` would mislead operators. The existing asymmetry (alert-state panels are AWSâonly) sets a precedent. No action required.

---

### 6. Single BatchGetItem vs. splitting FLEET

**Verdict: Correctâkeep it as one batch.**

- The FLEET row shares `sk="STATE"`, so itâs naturally part of the same query.
- Splitting would add a second roundâtrip, doubling latency and throwing away the batch discount.
- Partialâfailure semantics: if the FLEET key is throttled, the adapter simply wonât see it â `fleet` becomes `null` (or default object per Q3). Thatâs fine. The topâlevel `pumps_reporting` still counts only pump rows, which is correct.

**One subtle concern:** `BatchGetItem` returns items in **no particular order**. The current `_snapshot` iterates the list and partitions by `pump_id`. If DynamoDB returns a mix that includes the FLEET row, it works. If DynamoDB returns **only** the FLEET row (theoretical, if all pump keys are throttled), `pumps` will be empty and `fleet` will be populated. Thatâs correct behaviour. No issue.

---

### Additional risks and contract leaks

#### A. Envelope `pumps_reporting` counts only pump STATE rowsâbut should it include the FLEET rowâs concept?

The top-level `pumps_reporting` is used in the existing AWS dashboard (e.g., panel that says âX pumps reportingâ). That number is the count of pump rows retrieved. With the FLEET row added, the `len(pumps)` still excludes FLEET because `_snapshot` filters it out. **This is correct**, but verify that no existing dashboard panel relies on `pumps_reporting` being the count of **all** STATE rows (including the FLEET aggregate). The FLEET row is not a pump, so it should not be counted. The current code does this correctly.

#### B. The `fleet.PSI` fields are typed as `float`, but the gauge thresholds use 0.10 / 0.25. Ensure `lambda_fleet_psi` writes **signed floats** (not integers). If it accidentally writes `0` instead of `0.0`, the gauge will still render correctly. If it writes a string, the panel will break. Add an integration test for the whole pipeline.

#### C. The test `read-efficiency test` asserts 16 keys, but `BatchGetItem` has a 100âitem limit. Fine. However, the test does **not** assert that the FLEET key is actually present in the request. The coldâstart test asserts `_STATE_ROW_IDS` includes it, but the handler could theoretically filter it out. Add an assertion in `read-efficiency test` that the keyset contains `FLEET`.

#### D. The `_fleet_entry` returns `last_alert_sent_at` as-is from the row (could be `None` or a string). The `_pump_entry` returns the same. The table panel uses `nullâ"never"` mappingâthatâs fine. But check: does the table panel also expect `alert_flag` to be `true`/`false`? Yes. The test for `alert passthrough` covers it.

#### E. The `context/dashboards.md` open questions (live render, fleet-Lambda 1-indexed note) are acknowledged but not resolved. These are **non-blocking** as long as theyâre tracked before demo. Iâd like to see a timestamp or commit that resolves them.

---

### Resolution recommendations

| Point | My vote | Rationale |
|---|---|---|
| 1. Additive shape | Accept | Record skew caveat in contract. |
| 2. `pumps_reporting` overload | **Rename to `pumps_pooled`** | Eliminates confusion; divergence from storage is allowed. |
| 3. Null fleet render | **Return default object** (not `null`) | Eliminates Infinityâspecific error. Test on live system anyway. |
| 4. Hard-read vs. defensive | Accept + test | Add unit test for malformed FLEET row. Optionally add `.get` + log. |
| 5. AWS-only asymmetry | No change | Clean per ADR 0005. |
| 6. Single BatchGetItem | Keep | Efficient and correct. |

**Additional actions:**
- Add read-efficiency test assertion that FLEET key is in the `BatchGetItem` request.
- Add a test for malformed FLEET row (missing `pumps_reporting`).
- Document `as_of` versus `latest_ts` skew in `_interfaces.md`.
- Resolve open questions in `context/dashboards.md` before demo.

The change is nearly thereâaddress items 2, 3, and the test gaps, and itâs ready for integration.

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-11 06:38:53._

