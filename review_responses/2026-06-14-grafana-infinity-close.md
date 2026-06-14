# Adversarial Review: Infinity Relative-URL & `$.fleet` Fixes

## Overview

This change closes two open items from `dashboards.md` and fixes two real render bugs exposed by live mock observation. The core fixes are correct in intent and the mock-based closure is logically sound, but several contract and stability risks remain. I will address your specific questions first, then flag additional failure modes.

---

## 1ï¸â£ `root_selector: "$"` for topâlevel scalars

**Short answer:** The fix is correct and idiomatic for Infinityâs JSON parser. There is no regression risk to the emptyâroot autoâdescend behaviour, because `"$"` is an explicit path â the parser will never treat it as empty.

**But there is a versionâstability risk:** Infinityâs JSON parser changed in v2.0.0 to support `"$"` as the root. If the deployed Grafana image uses an older Infinity plugin (preâ2.0, though I doubt anyone runs that today), `"$"` could be interpreted as a JSONPath starting at root, which would still work. Iâd like to see the Grafana Infinity plugin version pinned in a `README` or `docker-compose` to avoid future regression. If pinned via a menu, document it. Without a pin, a future update could break `"$"` if they drop JSONPath support. This is a lowârisk but real demoâday surprise.

**Alternative idiom:** You could use `root_selector: ""` with a **UQL pipe** like `parse-json | project pumps_reporting, fleet_size | limit 1`. That would be more explicit and versionâresistant (UQL is stable). However, your fix works and is simpler. Iâd accept `"$"` with a comment/pin.

---

## 2ï¸â£ `null â "never"` via value mapping + special mapping

**The value mapping is necessary; the special mapping is dead code and should be removed.**

**Why:** Infinityâs parser converts a JSON `null` into the **string** `"null"` for columns of `type: string`. The `special: match "null"` mapping fires on the **raw JSON value** (i.e., `null`), not the postâconversion string. After conversion, the column holds `"null"` (string), and `special` does not match a string. The value mapping `type: value` with `options: { "null": ... }` correctly matches the string `"null"`.

**Is the string `"null"` a legitimate timestamp?** No. `last_alert_sent_at` is an ISOâ8601 timestamp or `null`. The string `"null"` would be syntactically invalid ISOâ8601 and would never appear from the mock or real Lambda. So the value mapping is safe.

**Should you keep both?** No â the special mapping is silently ineffective. It will never fire; keeping it is misleading. Future maintainers may wonder why itâs there or may copy it to another nullable column and assume it works. **Remove the special mapping** entirely. The value mapping alone suffices.

**Contract leak:** The fact that we learned this only after live observation means the âclosed by contractâ from 2026â06â07 was wrong. This is precisely the risk you identified in Q4. Iâll address that there.

---

## 3ï¸â£ Panel 2 scope (Fleet snapshot) â creep or necessary?

**Correct call.** Panel 2 also displays `last_alert_sent_at` and was rendering the literal `"null"`. Adding the mapping is **not** scope creep â itâs fixing the same bug in all affected panels. The open items were âtwo latent render bugsâ; panel 2 was one of them (the unspoken third bug). Including it is the minimal, consistent resolution. I approve.

---

## 4ï¸â£ Contractâvsârender lesson: ADR 0014 / dashboards.md guard

**Yes, you should add a explicit guard.** The current dashboards.md Â§Open questions already listed the ânullâneverâ item as closed by contract; it turned out false. That section should now contain a **widely applicable rule**, something like:

> *âFor `type: string` nullable columns, wire `null` appears as the string `"null"` after parsing. To display `"never"` (or any replacement), a `type: value` mapping on the string `"null"` is required. The `special: match "null"` mapping is ineffective for string columns and must not be used. Apply this rule to every `last_alert_sent_at` column and any future nullable string column.â*

I also recommend adding a oneâline note to ADR 0014 (if you consider it the architectural record) or at least to the internal dev guidelines. Without it, the same bug will recur when someone adds a new nullable column and copies an old panelâs special mapping.

---

## 5ï¸â£ Mockâvsâlive equivalence

**The equivalence is safe to CLOSE on.** Infinityâs datasource behaviour is identical when the base URL points at a local HTTP server vs. a Lambda Function URL: the plugin sends a GET, receives JSON, parses it, and applies selectors/mappings. The only differences would be network or authentication errors, which are irrelevant to the render fix. I see no scenario where a live lambda would produce a different dashboard output than the local mock, given identical JSON payload. **Closing on mock is fine.**

That said, the **demoâday presentation** should mention that the JSON was verified with a liveâshaped mock to avoid any implicit promise of endâtoâend live testing. As long as thatâs documented, itâs low risk.

---

## 6ï¸â£ Untested nullâfleet path (`fleet: {}`)

**Acceptable to leave untested for now**, but **track it** as a lowâpriority demoâday gate. The contract analysis (DeepSeek review) already covers the rendering: empty `{}` will produce a âNo dataâ message for gauges and an empty table row. The code path is identical to the populated case, just with one fewer object in the array. The Infinity pluginâs behaviour for an empty array/object is deterministic.

If you have time before demo, a quick mock test with `fleet: {}` would be a cheap reassurance, but I would not hold the closure on it. Mark it as âverified by contract, not reâtested 2026â06â14â in the session log.

---

## Additional renderâtime failure modes

* **`root_selector: "$"` on a nonâobject root type** (e.g., if the mock accidentally returns an array at the top level). Check that the mock always returns a JSON object. If it ever returns `[]`, the root will be an array and `"$"` will still work (the array will be treated as one row? Actually, `"$"` on an array returns the array itself, which Infinity may expand into multiple rows. That would break the singleârow expectation. Ensure the mock data envelope is an object (it is, per ADR 0014). But a deployâtime error that changes the structure could cause regression. This is unlikely but could be caught by a schema test.
* **Value mapping ordering**: The value mapping (`index: 0`) and special mapping (`index: 1`) have different indices. If both are kept, Infinity likely applies value mappings first, then special mappings. Since the special never fires, it doesnât matter. But if you remove the special mapping, the index stays at 0 â fine. Ensure no other mappings in the same overrides list conflict.
* **Panel 1: stat with `root_selector: "$"` and two number columns**. Stat panels in Infinity treat the first column as the value and the second as the suffix/prefix. Your columns are `pumps_reporting` (value) and `fleet_size` (suffix). This is a correct layout. But verify that the stat panelâs value mapping order is compatible with the mapping on `last_alert_sent_at` in the same panel? Panel 1 does **not** have that mapping â only panels 2,8,13 do. So no conflict.

---

## Summary of required actions (in order of importance)

1. **Remove the dead `special: match "null"` mapping** from all three panels. It is ineffective and misleading. The `type: value` mapping alone is correct.
2. **Add a guard rule to `context/dashboards.md`** (or ADR 0014) documenting the stringânull behaviour and forbidding `special` for string columns.
3. **Pin the Infinity plugin version** in the Docker setup to avoid future parser changes breaking `"$"` or mapping ordering.
4. **Optionally test `fleet: {}`** before demo for peace of mind, but not blocking closure.

The diff is clean, the mock test is solid, and the core fixes are correct. With the above three actions, Iâd consider the verificationâgap items fully closed.

---
_Generated by **deepseek** (`deepseek-reasoner`) on 2026-06-14 11:09:28._

