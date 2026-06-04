# Next session brief — dashboards #2: the Grafana JSON pair

## Goal
Close the dashboards component: two committed dashboard JSON files —
`dashboards/local.json` (InfluxDB datasource) and `dashboards/aws.json`
(Infinity datasource → adapter Function URL) — rendering the SAME
panel concepts from the shared field vocabulary (ADR 0005 §3 /
ADR 0009 / ADR 0014), plus Grafana provisioning so `docker compose up`
loads them without manual import.

## PARITY-SET SESSION (Tier 2b loads mandatory)
`dashboards` is in the ADR 0005 parity set (DEV_NORMS §5). Even
though dashboard JSON calls no Python, the panel-level vocabulary IS
the parity surface here. Load: `shared/{features,score,drift}.py`
(read, don't re-derive), ADR 0005, and cite the enforcement tests
(`local_runtime/tests/test_service.py::test_structural_parity_no_vendoring`
+ siblings) by name. Do not start without these.

## Open questions to resolve
1. **Provisioning method.** Grafana provisioning dirs
   (`/etc/grafana/provisioning/{datasources,dashboards}` mounted in
   docker-compose) vs manual import. Provisioning-as-code is the
   expected leader (portfolio signal; reproducible demo).
2. **Panel set.** Suggested: fleet score heatmap/table, per-pump score
   timeseries, 4× PSI timeseries (`psi_<feature>`), alert-state table
   (`alert_flag` + `last_alert_sent_at` passthrough — NO re-derived
   thresholds, ADR 0012 §2C), `pumps_reporting / fleet_size` stat
   (AWS mode). Decide the exact pair-equivalent set.
3. **Infinity plugin install** — `GF_INSTALL_PLUGINS` env in
   docker-compose vs baked image. The adapter URL is a
   per-apply value: datasource provisioning needs a variable strategy
   (env substitution? placeholder + README note?).
4. **Refresh rate.** Adapter cost is noise (ADR 0014 ~$0.0003/demo),
   so choose on demo-story grounds (5 s? 10 s?).
5. **Datasource UIDs** — pin stable UIDs in provisioning so the JSON
   pair references them deterministically.

## In-scope (in order)
1. Plain-language walkthrough + AskUserQuestion for the PO calls above.
2. ADR if (and only if) the provisioning/variable-URL decision proves
   ADR-worthy; otherwise session-log decisions.
3. The two JSON files + provisioning YAML + docker-compose wiring.
4. Local-mode verification (Grafana in Docker against InfluxDB) —
   PO-side eyes-on; structural checks sandbox-side (both files parse,
   panel field names ⊆ ADR 0005 §3 vocabulary — consider a small test).
5. Update `context/dashboards.md` (+ `_interfaces.md` only if the wire
   contract gains anything — it shouldn't; ADR 0014 is locked).

## Loads
- Tier 1: `context/_global.md`, DEV_NORMS §7 + §8.
- Tier 2: `context/dashboards.md`.
- **Tier 2b (parity):** `shared/features.py`, `shared/score.py`,
  `shared/drift.py` + ADR 0005 + the structural-parity test names.
- Tier 3: `context/_interfaces.md` (§Grafana adapter, §PSI parameters).
- ADRs: 0014 (wire contract), 0009 (PSI surface), 0012 (alert
  passthrough literalism), 0005 §3 (field names).
- Memory: fuse-write-truncation (NEW Write files safe; existing-file
  changes bash-side; verify both views), git-on-windows, infra-session1.

## Constraints
- $0: Grafana OSS local Docker only (Managed Grafana is an
  anti-pattern); Infinity plugin is free/signed.
- FUSE rules as always; docker-compose.yml is an EXISTING file —
  bash-side rewrite, verify both views.
- Bash 45 s cap. Git PO-side. BOM-free commit sequence (DEV_NORMS §7).

## Definition of done
- Both JSON files + provisioning committed; `docker compose up` renders
  the local dashboard with zero manual steps (PO-verified).
- Structural vocabulary check green; suite stays ≥ 404+1.
- Session log + review packet → cascade → dispositions → commit.
- Close with AskUserQuestion: next focus (candidates: simulator IoT
  Thing/cert provisioning — unblocks AWS end-to-end; CI; README/portfolio
  polish) + prepared brief.

## Carried context
- Suite baseline: **404 passed + 1 skipped** (cold-path session).
- Verify the cold-path commit landed before starting
  (`git log --oneline -3`, subject `infra: add cold path — …`) and
  that its cascade dispositions were folded in pre-commit.
- PO-side `terraform validate` + `plan` with ALL THREE build scripts
  may still be pending from the cold-path session — if so, run that
  first (no new infra this session, but a green plan is the baseline).
- WATERMARK reserved SK now coexists with STATE (`_interfaces.md
  §Reserved-SK coexistence`) — irrelevant to panels, but any SK-aware
  change must check both.
- Cold-start latency measurement remains post-first-apply
  (boto3-runtime-version canary).
