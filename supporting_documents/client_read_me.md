# Reading the Designated Lands Pipeline Report

This guide explains what's in `designated_lands_pipeline_report_<date>.xlsx` and which
parts of it are actually relevant to policy/conservation analysis versus which parts
are internal GIS/pipeline bookkeeping.

The workbook has **7 tabs**. The first 5 are run bookkeeping for the GIS analyst — you
generally don't need them. The last 2 are the actual species/land-designation results.

> **What changed in the October 2026 edition.** Before the pipeline measures how much
> of each species' Critical Habitat is covered by provincial protections, it now
> **removes federal land** (National Parks, National Wildlife Areas, Indian Reserves,
> and any other federally-owned parcels) from the Critical Habitat polygons.
> This matters because the federal portions of Critical Habitat are **not** B.C.'s
> reporting responsibility under SARA s. 63 — they belong to the federal
> government. By cutting them out first, every percentage in Tabs 6 and 7 answers
> the right question: *of the Critical Habitat on **non-federal** land in B.C.,
> how much is already protected by a provincial designation?* Any species whose
> Critical Habitat sits **entirely** on federal land is flagged separately and
> drops out of the overlap tables.

> **Note — "Entirely federal" Critical Habitat.** When federal land is cut
> out of a Critical Habitat polygon, most polygons shrink but still have
> some non-federal portion left behind. A small number may have **nothing
> left** — i.e. the entire polygon sat on federal land. These are set aside
> into a separate side-table (not shown in Tabs 6 and 7) because they do
> not contribute to B.C.'s SARA s. 63 reporting responsibility.
>
> If you need the list of species / polygons that landed in this category
> for a given run, just ask the GIS analyst — the information is retained
> alongside the main outputs and can be exported as a table.

---

## Tabs 1–5: Pipeline bookkeeping (GIS analyst — you can skip these)

| # | Tab | What it is |
|---|-----|------------|
| 1 | **Changes** | A list of designations (e.g. new parks, park additions) that came into effect within the date window used for this run. Only populated if the pipeline was run with a date filter on. |
| 2 | **Excluded Layers** | Layers that were left out of this run and why (e.g. "no date field available," "non-BCGW source," or federal layers excluded by choice). |
| 3 | **Summary** | Run metadata: when it ran, which options were selected, how many of the 42 source layers were included/excluded, and a log of any warnings/errors the pipeline hit. |
| 4 | **Pipeline Options** | The exact settings used for this run — date range, whether federal layers were excluded, and the specific database query used to pull each of the 42 designation layers. |
| 5 | **Designation Categories** | A reference table of all 42 designation types, their process order (used to resolve overlaps when designations stack), and their restriction ratings (Forest / Old Growth / Mine). |

**Why you don't need these:** they describe *how the run was configured and what data went in*, not the conservation results themselves. They exist so the GIS analyst can reproduce or audit a run later. If a number in Tabs 6–7 looks off, this is where we'd go to check what settings produced it — but you shouldn't need to interpret them day-to-day.

---

## Tabs 6–7: Critical Habitat Area (CHA) Results — 

These two tabs show, species by species, where provincial/federal land designations overlap with federally-designated Critical Habitat for species at risk. Each row is one overlap between one designation polygon and one Critical Habitat polygon for one
species.

### Tab 6: CHA Planarized

This tab uses the **planarized** layer, where overlapping designations have been merged into non-overlapping pieces of ground. Each row represents a unique patch of land and lists **all** the designations that stack on top of it (see `overlapping_designations` column), plus the *strongest* restriction rating found among them (`forest_restriction_max`, `og_restriction_max`, `mine_restriction_max`).

**Use this tab when you want to know:** "for this piece of land inside a species'
critical habitat, what is the strongest protection in place, accounting for the fact that several designations might overlap here?"

### Tab 7: CHA Overlapping

This tab uses the **overlapping (un-planarized)** layer — each individual designation polygon is listed as its own row, even where multiple designations stack on the same
ground. A single patch of land with 3 stacked designations will appear as 3 separate rows here, each with its own restriction rating (`forest_restriction`, `og_restriction`,
`mine_restriction`).

**Use this tab when you want to know:** "Which specific designation(s) are present here?" 

### Key columns in both tabs

| Column | Meaning |
|---|---|
| `SciName` / `CommName_E` | Scientific / common name of the species |
| `SARA_Status` | Species at Risk Act listing status |
| `SiteName_E` | Name of the critical habitat site |
| `designation` (and `overlapping_designations` in Tab 6) | Which land designation(s) are present |
| `ECCC_Cha_Area_Ha` | Original area of the Critical Habitat polygon as published by ECCC (hectares). Shown for reference only. |
| `Cha_Area_Fed_Removed` | Area of the Critical Habitat polygon **after** federal land has been cut out (hectares). This is the "non-federal B.C." portion — the denominator used for the protection percentage. |
| `Overlap_Area_Ha` | Area (hectares) where this provincial designation overlaps the non-federal portion of the Critical Habitat polygon. |
| `Pct_of_Cha_Prot_by_LandDes` | Percentage of the **non-federal** Critical Habitat covered by this designation. Previously called `CHA_Protected_Pct`. |



## Quick reference: which tab answers which question?

| Question | Tab |
|---|---|
| "What changed recently?" | Changes (1) |
| "What wasn't included in this run and why?" | Excluded Layers (2) |
| "How was this run configured?" | Summary (3) / Pipeline Options (4) |
| "What's the restriction rating for a given designation type?" | Designation Categories (5) |
| "What's the strongest protection on a piece of land within critical habitat?" | **CHA Planarized (6)** |
| "Which individual designations overlap a species' critical habitat?" | **CHA Overlapping (7)** |
| "What % of a species' critical habitat is protected on non-federal land?" | CHA Planarized or Overlapping (6/7) — look at `Pct_of_Cha_Prot_by_LandDes`; **confirm with GIS analyst before citing** |

---

*This client_readme describes the report structure as of the pipeline version reviewed in October 2026 (federal-erase edition). Column names and tab contents may change as the pipeline is updated. If something here doesn't match what you're looking at, check with the GIS analyst.*
