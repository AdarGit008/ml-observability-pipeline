# ADR 0014 — Grafana Adapter API Contract (Function URL + Infinity)

- **Status:** Accepted (PO sign-off 2026-06-04; reviewer-cascade review pending)
- **Date:** 2026-06-04
- **Deciders:** PO (Adar), Claude (architect), reviewer cascade (pending)

## Principle (plain English)

**The adapter is a projection, not a brain.** Grafana asks one
question — "what is the fleet's latest state?" — and the answer
already exists, fully computed, in the 15 STATE rows the scorer
maintains. The adapter's whole job is to fetch those rows in one
`BatchGetItem`, rename the attributes to the names the local-mode
panels already use, and hand back JSON. It computes nothing, decides
nothing, and thresholds nothing: `alert_flag` and
`last_alert_sent_at` pass through literally (ADR 0012 §Alternatives
2C — consumers never re-derive breach state). If the adapter ever
grows an `if psi > ...` branch, this ADR is the document to wave at
it.

## Context

HANDOFF §6 Q1 left the Grafana ↔ DynamoDB adapter unspecified, with
"Lambda Function URL + JSON datasource plugin" as the sketched
leader. Everything upstream is now resolved: ADR 0010 locked the
STATE row + `BatchGetItem` access pattern and promised the adapter
would be "~10 lines of boto3"; ADR 0012 gave the STATE row the
two-attribute alert state and named the adapter its second consumer;
ADR 0009 fixed the PSI surface at four keys; ADR 0005 §3 pinned the
InfluxDB field names local-mode panels query (`psi_vibration_amp`,
…). What remained was the wire contract: endpoint shape, response
JSON, plugin, and auth mode. This session (the dashboards session)
resolves all four; the Grafana dashboard JSON itself comes later.

Constraints in play: $0 posture (Function URLs are free; ADR 0013
prices the reads at ~7 RRUs per panel refresh — noise), mode parity
at the *panel* level (the same dashboard concepts must render from
InfluxDB locally and from this adapter in AWS mode), and
single-PC demo ergonomics (every credential added to local Grafana
is one more demo-day moving part).

## Decision

1. **One read-only Lambda (`pump-dashboard-adapter`), separate from
   the scorer,** behind a **Lambda Function URL, `AuthType = NONE`**
   (public-with-obscurity; rationale in Alternatives §3). GET only;
   any other method gets `405`. The path is ignored — the Function
   URL serves exactly one resource, the fleet snapshot.

2. **Response: a JSON envelope around a flat per-pump array.**

   ```json
   {
     "fleet_size":      15,
     "pumps_reporting": 13,
     "as_of":           "2026-06-04T14:32:01.123Z",
     "pumps": [
       {
         "pump_id":            "P-01",
         "latest_ts":          "2026-06-04T14:32:00.971Z",
         "latest_score":       0.04,
         "psi_vibration_amp":  0.02,
         "psi_bearing_temp":   0.01,
         "psi_motor_current":  0.03,
         "psi_rpm":            0.02,
         "alert_flag":         false,
         "last_alert_sent_at": null
       }
     ]
   }
   ```

   - **PSI keys are flattened** to `psi_<feature>` — exactly the
     InfluxDB field names from ADR 0005 §3 / ADR 0009. A panel built
     against local mode and one built against this adapter use the
     same field vocabulary; zero Grafana transforms either side.
   - **`alert_flag` and `last_alert_sent_at` are literal
     passthroughs** of the STATE-row attributes (ADR 0012). The
     storage-side "absent until first publish" convention maps to an
     explicit **JSON `null`** on the wire: storage has no null
     sentinel, but a wire format with a stable key set is kinder to
     Grafana's column inference than keys that appear mid-demo.
   - **Pumps without a STATE row are omitted** (not null-filled);
     `pumps_reporting` vs `fleet_size` lets a stat panel show
     "13/15 reporting" instead of a heatmap of fake zeros.
   - `as_of` is the adapter's own invocation time — it timestamps
     the *snapshot*, while each pump's `latest_ts` timestamps that
     pump's last scoring.

3. **Fleet membership comes from a `FLEET_SIZE` env var** (default
   15), expanded to `P-01..P-NN` per the `_interfaces.md` pump-id
   format. The `BatchGetItem` requests exactly those 15 STATE keys;
   `UnprocessedKeys` (theoretical at 15 keys ≈ 6 KB) is retried up
   to 3 passes, then `500`. Reads are eventually consistent —
   dashboards are the textbook eventual-consistency-tolerant
   consumer (ADR 0010 §Consequences).

4. **Grafana side: the Infinity datasource plugin** (signed, actively
   maintained), root selector `$.pumps`, against the Function URL.
   AWS-mode dashboard ships as a second JSON file (the
   `context/dashboards.md` default), not a datasource toggle.

5. **The adapter does not import `shared/`** — it extracts no
   features, scores nothing, computes no PSI. It therefore stays
   OUTSIDE the ADR 0005 parity test surface. A future change that
   adds a `shared/` import puts it in the parity set (DEV_NORMS
   §5 Tier 2b) in the same PR.

## Alternatives considered

### 1. Response shape

**A. Envelope + flat array (the decision).** The envelope carries
fleet-level facts (`fleet_size`, `pumps_reporting`, `as_of`) that
belong to no single pump; the array entries stay flat so table and
heatmap panels bind columns directly. Infinity's `$.pumps` root
selector is one config field.

**B. Bare top-level array.** Marginally simpler Infinity config (no
root selector) but loses the only sane home for
`pumps_reporting` — a "how complete is this snapshot?" panel would
have to count rows and hardcode 15. Rejected: the envelope costs one
selector, the bare array costs a magic number in a dashboard.

**C. Nested `latest_psi` map (mirror the STATE row exactly).**
Truest to storage, but every PSI panel needs an extract-fields
transform, and the field names would diverge from the local-mode
InfluxDB names — the panels stop being mode-symmetric. Rejected on
the panel-parity argument; the adapter exists precisely to absorb
this rename once, server-side.

**D. SimpleJSON `/search` + `/query` + `/annotations` protocol.**
Three endpoints implementing a deprecated plugin's protocol,
shaping data into Grafana's legacy timeseries/table frames
server-side. Most code, least future. Rejected.

### 2. Datasource plugin

**A. Infinity (the decision).** Signed, actively maintained,
flexible JSON parsing (JSONPath root selector + column mapping),
and — decisive for the future — native SigV4 support, so the
auth-mode upgrade path needs no plugin change.

**B. JSON API plugin (marcusolsson) — the HANDOFF sketch's leader.**
Works for this contract, but no AWS-auth story at all (locks the
Function URL into staying public forever) and a slower maintenance
cadence than Infinity. Rejected: same integration effort, fewer
exits.

**C. Export DynamoDB → InfluxDB during the AWS demo (HANDOFF Q1's
third option).** Kills the adapter entirely — but adds a sync
process whose lag the demo has to explain, and the AWS-mode
dashboard would silently be reading local infrastructure. The
portfolio story ("Grafana reads the cloud hot store live") is the
point. Rejected.

### 3. Function URL auth mode

**A. `AuthType = NONE`, public-with-obscurity (the decision).**
The URL embeds a random 32-char subdomain; the Lambda is read-only
over 15 rows of *synthetic* pump telemetry; the blast radius of
disclosure is "someone sees fake PSI values" plus request-count
cost — and per ADR 0013 math, even a hostile refresh loop costs
cents before the $1 budget alert fires. Zero credentials in local
Grafana on demo day. PO call, 2026-06-04.

**B. `AuthType = AWS_IAM` + Infinity SigV4.** The
production-correct posture, and Infinity supports it. Costs a
dedicated IAM user + access key wired into Grafana, key rotation
story, and one more thing to break live. Rejected *for the demo*;
explicitly recorded as the one-config-flip upgrade (flip the
Terraform `authorization_type`, add the key to the datasource —
no code change, no plugin change).

### 4. Fleet membership source

**A. `FLEET_SIZE` env var → generated key list (the decision).**
One integer, set by Terraform, matching the simulator's fleet size.

**B. `Scan` with `sk = "STATE"` filter.** Self-discovering (no
config) but a `Scan` reads every item in the table — ~27 K reading
rows per pump-hour of demo — to find 15 rows. The filter happens
*after* the read; ADR 0013's cost math would be obliterated by the
panel refresh loop. Rejected: textbook Scan-vs-known-keys case.

**C. Hardcoded 15-key list in the handler.** No env plumbing, but
the fleet size already lives in the simulator config; hardcoding it
a second place guarantees eventual drift. The env var at least
makes the coupling a visible, Terraform-set knob. Rejected.

## Consequences

**Positive:**

- **Panels are mode-symmetric.** Same field names from InfluxDB
  (local) and the adapter (AWS); the dashboards session builds one
  panel vocabulary.
- **Alert surfacing is literal** — the third consumer in a row
  (handler, now adapter, later panels) that never re-derives
  thresholds. ADR 0012's two-attribute design pays off as intended.
- **Cost is noise.** ~7 RRUs per refresh; a 5-second-refresh,
  30-minute demo adds ~360 BatchGetItems ≈ $0.0003 on top of
  ADR 0013's ~$0.15/demo.
- **The adapter stays out of the parity set** — no `shared/`
  import, no Tier 2b ceremony for future adapter-only sessions
  (Decision #5 is the tripwire if that changes).
- **Auth upgrade is config-only** in both Terraform and Grafana.

**Negative:**

- **A public, unauthenticated endpoint exists while the stack is
  up.** Bounded by: synthetic read-only data, obscure URL, the
  apply→teardown lifecycle (the URL dies with every
  `aws_teardown.sh`), and budget alerts as backstop. Accepted
  knowingly; revisit if the project ever serves non-synthetic data.
- **`FLEET_SIZE` duplicates the simulator's fleet size** as a
  Terraform variable. Drift here = silently short snapshots
  (`pumps_reporting` would expose it, but nothing fails loudly).
  Mitigation: both default to 15; a fleet-size change is already a
  multi-file event (simulator config + this variable).
- **JSON `null` for never-alerted differs from the storage
  convention** (absent attribute). Two representations of one fact,
  each idiomatic for its medium; documented here and in
  `_interfaces.md` to keep the mapping deliberate.

**Follow-ups:**

- Dashboards session (later): Grafana dashboard JSON pair (local +
  AWS-mode), Infinity datasource provisioning, panel band colors
  per `_interfaces.md §PSI parameters`.
- `infra/outputs.tf` exports `adapter_function_url` (promised by
  `context/infra.md §Interfaces` "Out (later sessions)").
- `aws_teardown.sh` covers the adapter Lambda, log group, Function
  URL, and IAM role (landed this session).

## References

- HANDOFF.md §6 Q1 — the open question this ADR resolves.
- ADR 0005 §3 + Addendum 2026-06-03 — the InfluxDB field names the
  flattened PSI keys mirror.
- ADR 0009 — 4-key PSI surface (`latest_psi` shape).
- ADR 0010 — STATE row + `BatchGetItem` access pattern; eventual-
  consistency tolerance argument.
- ADR 0012 — two-attribute alert state; §Alternatives 2C is the
  no-client-side-re-derivation rule this contract enforces.
- ADR 0013 — cost posture the per-refresh reads ride on.
- `context/_interfaces.md §Grafana → DynamoDB adapter` — updated
  from TBD to this contract in the same session.
- Implementation: `dashboards_adapter/handler.py`,
  `dashboards_adapter/tests/test_adapter.py`,
  `infra/modules/dashboards_adapter/`.
- Session log: `docs/sessions/2026-06-04-dashboards-adapter-contract.md`.
- Infinity plugin: https://grafana.com/grafana/plugins/yesoreyeram-infinity-datasource/
- Lambda Function URLs: https://docs.aws.amazon.com/lambda/latest/dg/urls-configuration.html


## Addendum 2026-06-10 — FLEET object (ADR 0018 dashboards follow-up)

ADR 0018 built `lambda_fleet_psi`, which writes a pooled plant-wide PSI
to a reserved `pump_id="FLEET", sk="STATE"` row, and parked the
dashboard surfacing as a follow-up ("the adapter's `BatchGetItem` reads
the 15 pump STATE keys; a FLEET panel is a small follow-on adapter
change", ADR 0018 §Follow-ups). This addendum folds that change. It is
**additive** — the `pumps[]` array and every existing key are untouched,
so no consumer (Infinity panel or otherwise) breaks. Two points were
adjusted after the DeepSeek review (2026-06-11, `deepseek-reasoner`):
the absent-row representation (§Decision 3 below) and the wire name of
the pooled count (§Decision 2 below).

### Contract bump (additive)

1. **One extra BatchGetItem key.** The adapter now requests
   `FLEET_PUMP_IDS + ("FLEET",)` — the 15 pump STATE rows plus the one
   FLEET aggregate row — in the SAME single `BatchGetItem` (still one
   round trip; ADR 0010 access pattern + ADR 0013 cost posture hold at
   16 keys ≈ 6 KB). `FLEET` is a separate partition (ADR 0018
   §Decision 4), so this disturbs no per-pump access pattern.

2. **New top-level `fleet` object** (sibling of `pumps`):

   ```json
   "fleet": {
     "latest_ts":          "2026-06-10T14:30:00.000Z",
     "psi_vibration_amp":  0.30,
     "psi_bearing_temp":   0.12,
     "psi_motor_current":  0.20,
     "psi_rpm":            0.15,
     "alert_flag":         true,
     "last_alert_sent_at": "2026-06-10T14:25:00.000Z",
     "pumps_pooled":       15
   }
   ```

   - **Same projection rules as a pump entry** — `latest_psi` Map
     flattens to the `psi_<feature>` ADR 0005 §3 vocabulary, `Decimal`
     → `float`, absent `last_alert_sent_at` → JSON `null`, alert fields
     are literal passthroughs (ADR 0012 §Alternatives 2C — still no
     client-side threshold re-derivation).
   - **MINUS `latest_score`** — the fleet path runs no model (ADR 0018
     §5), so the FLEET row never carries a score; the `fleet` object
     omits the key entirely (not null) to make "there is no model here"
     explicit. This is why the FLEET row is projected by a dedicated
     `_fleet_entry`, never through `_pump_entry` (which hard-reads
     `latest_score`).
   - **PLUS `pumps_pooled`** — how many pumps fed the pooled 5-minute
     window (ADR 0018 §Decision 4). The FLEET **row** stores this as
     `pumps_reporting`; the adapter **renames it on the wire** to
     `pumps_pooled` to disambiguate from the envelope's **top-level**
     `pumps_reporting` (pumps that have a STATE row) — the two counts
     differ (throttled writes, partial window) and the shared name was a
     confusion risk (DeepSeek review §2). Projection-divergence from a
     storage attribute name is allowed — the adapter is a projection.
   - **`pump_id` is dropped** — it is the constant `"FLEET"` (the
     scope is the object's identity, not a column).

3. **`fleet` is an empty object `{}` when the FLEET row is absent** —
   the fleet Lambda has not run yet, or its empty-fleet no-op skipped the
   write (ADR 0018 §Decision 7). `{}` is the single-object analogue of
   the per-pump omit-the-row rule (a missing pump is dropped from
   `pumps[]`), and it keeps a JSON `null` off Infinity's `$.fleet` root
   selector — null-at-root behaviour is version-dependent in Infinity
   and was a demo-day risk (DeepSeek review §3). The key is always
   present; its value is `{}` (absent) or the populated object. Gauges
   and the alert table render "No data" over `{}` and — crucially — the
   adapter fabricates NO `alert_flag: false` "all-clear" for a fleet
   that simply has not been measured (PO call 2026-06-11).

### Timestamp semantics (skew)

`as_of` is the adapter's invocation time; `fleet.latest_ts` and each
`pumps[i].latest_ts` are when those rows were last written by their
producers — the fleet Lambda (5-min EventBridge cadence) and the
per-pump scorer (per-reading) run on different clocks. A consumer
correlating `fleet.latest_ts` against a pump's `latest_ts` must tolerate
skew (DeepSeek review §1). Documented in `_interfaces.md` + the handler
docstring.

### Mode-parity note (FLEET is AWS-only)

The FLEET row + alert fields exist only in the AWS hot path; local
InfluxDB carries the 13-field per-pump schema with no FLEET concept
(ADR 0005 §3). So the FLEET panel is added to `dashboards/aws.json`
**only** — there is no local-mode counterpart, the same asymmetry the
alert-state / `pumps_reporting` panels already have (ADR 0014
§Follow-ups, `context/dashboards.md`). The panel-vocabulary parity test
reuses the `psi_<feature>` names derived from
`shared.features.PSI_FEATURE_NAMES`; the one genuinely new wire token,
`pumps_pooled`, is added to the test's `_ADAPTER_KEYS` allowed-set in
the same change.

### Still NOT a brain

The adapter still imports no `shared/`, evaluates no threshold, and
computes nothing — surfacing the FLEET row is a pure passthrough of a
value `lambda_fleet_psi` already computed. `test_adapter_does_not_import_shared`
and `test_no_threshold_logic_in_module` remain the tripwires; both stay
green. The Principle ("a projection, not a brain") is unchanged.

### Tests

`dashboards_adapter/tests/test_adapter.py` gains a `§FLEET row` section
(surfaced-as-object, excluded-from-`pumps`, absent→`{}`, alert
passthrough, never-alerted→null, malformed-row→500) and updates the
read-efficiency test to the 16-key set; `conftest.py` gains a
`put_fleet_state_row` helper. Adapter package: 16 → 23 tests.
`dashboards/tests/test_dashboard_vocabulary.py` (7) green with
`pumps_pooled` added to the contract allowed-set.

### Panels (`dashboards/aws.json`)

Five AWS-only panels (ids 9–13): four `gauge` panels (one per pooled
`psi_<feature>`, `root_selector: "$.fleet"`, the standard 0.10/0.25 band
thresholds) and a `table` of the FLEET alert state (`pumps_pooled`,
`alert_flag` OK/ALERT-mapped, `last_alert_sent_at` null→"never" — the
same display mappings as the per-pump alert-state table, NO threshold
re-derivation).
