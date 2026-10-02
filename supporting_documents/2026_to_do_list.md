# 2026 Designated Lands Pipeline — To-Do List

**Context:** findings from comparing the live `erase_federal_from_cha` branch of `cjsostad/designatedlands_main` against the `update-2025` branch of `bcgov/designatedlands` (Oct 2026). Items are ordered by impact on output accuracy.

---

## Critical

**None outstanding.** The one critical candidate (WHA records silently excluded via `NOT LIKE` against NULL `FEATURE_NOTES`) was audited against the live BCGW endpoint on 2026-10-02 and confirmed not to be an issue — DataBC's GeoServer CQL evaluates `NOT LIKE` against NULL permissively (as true), not strictly per SQL three-valued logic, so the 206 NULL-notes WHA records that would be missed under strict SQL are already included in the current output. Full audit result is in Appendix B. Related defensive-coding note moved to item 1 below.

---

## Possible Improvements

### 1. Adopt bcgov's explicit `OR FEATURE_NOTES IS NULL` on the WHA filters (defensive)

The audit on 2026-10-02 confirmed the current filter and the broadened filter return the same count against today's GeoServer (NO HARVEST ZONE: 5,860 = 5,860; CONDITIONAL HARVEST ZONE: 1,107 = 1,107). No data is being missed. But:

- Adopting bcgov's broadened form makes the intent explicit in the CSV ("yes, we mean to include records with no notes").
- It protects against a future GeoServer upgrade that might tighten CQL semantics closer to standard SQL.
- One-line edit per row, zero runtime change, no re-run needed.

**Action:**
- [ ] Update `sources_designations.csv` row 15 (`wha_no_harvest`) to:
  ```
  (TIMBER_HARVEST_CODE = 'NO HARVEST ZONE' AND FEATURE_NOTES NOT LIKE '%not a legal boundary%') OR (TIMBER_HARVEST_CODE = 'NO HARVEST ZONE' AND FEATURE_NOTES IS NULL)
  ```
- [ ] Update row 31 (`wha_conditional_harvest`) to:
  ```
  (TIMBER_HARVEST_CODE = 'CONDITIONAL HARVEST ZONE' AND FEATURE_NOTES NOT LIKE '%not a legal boundary%') OR (TIMBER_HARVEST_CODE = 'CONDITIONAL HARVEST ZONE' AND FEATURE_NOTES IS NULL)
  ```
- [ ] Re-run the audit script in Appendix A whenever BCGW announces a GeoServer upgrade to confirm the behaviour hasn't changed.

---

### 2. `download_file()` has no retry logic

`_wfs_request()` and `_download_cha_zip()` both retry with exponential backoff (3 attempts). The generic `download_file()` in `designatedlands.py` (~line 328) runs a single `requests.get()` / `urllib.urlopen()` attempt. One transient 503 or FTP hiccup aborts the whole download step.

**Downloads currently exposed to this:**
- `fed_mask_nwa` — CPCAD 2025 GDB (~3 GB) from `data-donnees.az.ec.gc.ca`
- `national_wildlife_area` + `migratory_bird_sanctuary` — same CPCAD 2025 GDB
- `great_bear_grizzly_class1`, `great_bear_grizzly_class2`, `great_bear_fisheries_watersheds` — FTP GBR schedules
- `flathead` — FTP shapefile zip
- `bc_boundary_land` — FTP GDB zip

**Action:**
- [ ] Lift the retry pattern from `_download_cha_zip()` into `download_file()` (3 attempts, exponential backoff on `requests.ConnectionError` / `requests.Timeout` / `urllib.error.URLError` / 5xx).
- [ ] Add a `User-Agent` header to the HTTP branch to match `_download_cha_zip()`.

---

### 3. Planarize uses a centroid+area+perimeter hash as a stand-in for geometric identity

In `create_designations_planarized`, the composite key used to group Union output fragments is:

```python
key = (round(c.X, 7), round(c.Y, 7), round(geom.area, 2), round(geom.length, 2))
```

Two theoretical failure modes:
- **Collision:** two genuinely different polygons share this signature at the rounded precision → merged into one group, one designation lost, surviving row inherits `max()` restrictions from both inputs.
- **Drift:** two fragments that should be identical pick up tiny floating-point noise → stay as separate rows, overlap aggregation and process_order ranking never run.

In practice, probably fine for BC-scale designation data. But it's the kind of thing that would be invisibly wrong if it ever started failing, and planarization is the step that writes the authoritative non-overlapping layer the CHA intersect feeds from.

**Action:**
- [ ] Evaluate replacing the hash approach with `arcpy.management.Dissolve` on the Union output, using `statistics_fields` to carry `MIN(process_order)` and `MAX(forest_restriction)` / `MAX(og_restriction)` / `MAX(mine_restriction)`, dissolving on a composite grouping of the raw Union output's SHAPE. If licence tier allows, `PairwiseDissolve` is the pairwise equivalent and matches the `PairwiseErase` / `PairwiseIntersect` style used elsewhere.
- [ ] Low priority — defer until there's a reason to touch planarize.

---

### 4. `overlay_rasters` loads full-province arrays into memory, four at a time

Four full-BC uint8 arrays are held simultaneously (designation + three restrictions). At 25m resolution this is roughly 12 GB working set — fine on VDI. At 10m (listed in `DEFAULT_CONFIG`) it jumps to ~50 GB and the job won't complete.

bcgov's Jan 2026 rewrite processes the overlay in 5,000-row windows with `gc.collect()` between chunks.

**Action:**
- [ ] Only if a 10m run is ever on the table — port the chunked overlay pattern from bcgov's `update-2025` branch (`designatedlands.py` → `overlay_rasters()`).

---

### 5. `create_designations_overlapping` lacks per-source isolation

Two sub-items:

- No per-source `try/except` around the `SearchCursor` / `InsertCursor` block. One bad source halts the entire overlapping-stack build. Pipeline aborts cleanly (not silent) but one failing source means re-running all sources.
- `clip_tmp` is a single shared feature class name across every source. If a Clip fails and the Delete on the next iteration can't remove it (SMB lock — real failure mode per `learnings.md`), the next Clip raises on name collision.

**Action:**
- [ ] Change `clip_tmp` to be per-source unique (`clip_tmp_{source_id}` or an `in_memory\clip_{source_id}` workspace) — removes cross-source lock interference.
- [ ] Optionally wrap each source's `SearchCursor`/`InsertCursor` block in `try/except` with per-source `LOG.error`, matching bcgov's Jan 2026 pattern. Keeps fail-fast semantics on licence/data errors but one bad polygon doesn't tank the whole stack.

---

### 6. BC gov staff restriction rating revisions (Dec 2025)

The `update-2025` branch has three forestry-restriction rating changes made by the same team who authored the original ratings (karharker, Dec 10 2025). These are policy judgments, not technical fixes, but they're from the authoritative source and should be a deliberate accept/reject rather than defaulting to whatever we have:

| Designation | Current (ours) | bcgov Dec 2025 |
|---|---|---|
| `creston_valley_wma` forestry | Full | High |
| `migratory_bird_sanctuary` forestry | Medium | High |
| `ogma_legal` forestry | High | Full |

**Action:**
- [ ] Review the three ratings against bcgov `update-2025` branch and either accept, reject, or document the divergence.
- [ ] Note: `creston_valley_wma` is more restrictive in our version; the other two are less restrictive.

---

### 7. MBS designation CQL missing `BIOME = 'T'`

Row 12 of `sources_designations.csv` currently:
```
LOC IN (2) And TYPE_E = 'Migratory Bird Sanctuary'
```

bcgov's version adds `BIOME = 'T'` (terrestrial-only). Any marine portions of BC MBS layers currently load into `src_12_migratory_bird_sanctuary` and get silently stripped later by the `bc_boundary_land` clip — final planarized output is the same today, but if the clipping stage ever changes or a marine analysis path is added, those marine rows become a latent correctness risk.

**Action:**
- [ ] Add `AND BIOME = 'T'` to the MBS designation query, matching bcgov.
- [ ] (Sidebar — no action: your `fed_mask_nwa` and `national_wildlife_area` sources are both on CPCAD 2025, which is ahead of bcgov's 2024. Good.)

---

### 8. GBR schedules are from 2016

Rows 23, 36, 39 (`great_bear_grizzly_class1`, `great_bear_grizzly_class2`, `great_bear_fisheries_watersheds`) pull from `GBRO_Schedules_20160120.gdb.zip`. bcgov upgraded these to the 2022 draft schedules in Dec 2025 (`2022_GBRO_Draft_Schedules/2022 Draft GBRO Schedule Data GDB.zip`).

**Action:**
- [ ] Decide whether to track the 2022 draft schedules or wait for a non-draft release. Data-currency decision, not a code fix.
- [ ] If updating: the FC names inside the GDB change from `GBRSchD_GB_20151105` to `GBRSchD_GB_20220310` and from `GBRSchE_IFW_20160104` to `GBRSchE_IFW_20220506` — update `layer_in_file` in the sources CSV accordingly.

---

## Appendix A — Copilot prompt for re-running the WHA NULL-notes audit

Kept on file in case BCGW announces a GeoServer upgrade and the CQL `NOT LIKE`-vs-NULL behaviour needs re-verification. The script writes to `logs/wha_null_notes_audit_YYYYMMDD.csv`.

```
Write a standalone Python script `scripts/wha_null_notes_audit.py` that measures how many Wildlife Habitat Area records are silently excluded from our current CQL filters because `FEATURE_NOTES` is NULL.

Context:
- Our pipeline uses the BCGW WFS endpoint at https://openmaps.gov.bc.ca/geo/pub/wfs
- The layer is WHSE_WILDLIFE_MANAGEMENT.WCP_WILDLIFE_HABITAT_AREA_POLY
- Current filters in sources_designations.csv:
    row 15 wha_no_harvest:          TIMBER_HARVEST_CODE = 'NO HARVEST ZONE' AND FEATURE_NOTES NOT LIKE '%not a legal boundary%'
    row 31 wha_conditional_harvest: TIMBER_HARVEST_CODE = 'CONDITIONAL HARVEST ZONE' AND FEATURE_NOTES NOT LIKE '%not a legal boundary%'
- The script must NOT download any feature geometries. Use the probe pattern already in date_filter.py (count=1, read numberMatched from the GeoServer response). Reuse _count_features from date_filter if importable; otherwise reimplement the same tiny HTTP call.

For each of the two layers (NO HARVEST ZONE and CONDITIONAL HARVEST ZONE), issue three WFS probes and report the numberMatched for each:
    A) current filter:        TIMBER_HARVEST_CODE = '<zone>' AND FEATURE_NOTES NOT LIKE '%not a legal boundary%'
    B) null-notes delta:      TIMBER_HARVEST_CODE = '<zone>' AND FEATURE_NOTES IS NULL
    C) proposed broadened:    (A) OR (B)

For each layer, assert A + B == C (within a tolerance of 0). If the assertion fails, print a clear warning — it means the GeoServer is behaving differently than SQL's three-valued logic would predict, and the audit result is suspect.

Output format: a single table printed to stdout, columns [zone, numberMatched_current, numberMatched_null_notes, numberMatched_broadened, pct_missed]. Also write the same table to logs/wha_null_notes_audit_YYYYMMDD.csv.

Requirements:
- Uses requests, verify=False (BCGW uses a cert chain the arcgispro-py3 env can't always validate — same as our existing WFS calls).
- Reasonable timeout (60s per probe).
- Logs each probe URL and the raw numberMatched value at INFO level so a run is auditable.
- Returns exit code 0 on clean run, 1 if either A+B!=C assertion fails.

Do NOT modify the pipeline or sources_designations.csv. This script is read-only measurement.
```

**Note for future re-runs:** On the 2026-10-02 run, the `A + B == C` assertion did fail silently (numbers were 5860 + 206 ≠ 5860), and the `pct_missed` column was computed as B/A rather than (C − A)/C. If you regenerate the script, consider having Copilot (a) emit an explicit WARNING line to stdout when the assertion fails and (b) relabel the column as `pct_of_current_that_is_null` to avoid the misleading `pct_missed` wording. Neither affected the interpretation of the Oct 2 result, but worth tightening on the next run.

---

## Appendix B — WHA NULL-notes audit result (2026-10-02)

| zone | numberMatched_current (A) | numberMatched_null_notes (B) | numberMatched_broadened (C) | current == broadened? |
|---|---:|---:|---:|:---:|
| NO HARVEST ZONE | 5,860 | 206 | 5,860 | ✅ |
| CONDITIONAL HARVEST ZONE | 1,107 | 0 | 1,107 | ✅ |

**Interpretation:** `current == broadened` for both layers, confirming DataBC GeoServer's CQL evaluator returns `NOT LIKE` against NULL as true (not SQL-strict null). The 206 NULL-notes NO-HARVEST records are already captured by the current filter. No data is being silently excluded; no re-run required. Adopting bcgov's broadened form is still recommended as a defensive-coding note (item 1 above).

Raw CSV preserved at `logs/wha_null_notes_audit_20261002.csv`.

---

*Generated 2026-10-02. Based on `cjsostad/designatedlands_main@erase_federal_from_cha` (commit 66fc564) vs `bcgov/designatedlands@update-2025` (commit b7b2a21). Updated after WHA audit result on 2026-10-02.*
