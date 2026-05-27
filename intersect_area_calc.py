"""
intersect_area_calc.py — CHA intersection and overlap percentage calculation.

Intersects designations_planarized and designations_overlapping with
Critical Habitat Area (CHA), then calculates per-designation overlap
percentages via Statistics + JoinField, and computes per-feature
CHA protection percentages (how much of each CHA polygon is covered
by each designation).

Can be run standalone or imported as a module:

    Standalone:
        python intersect_area_calc.py

    As module (from pipeline scripts):
        from intersect_area_calc import run_cha_intersection
        run_cha_intersection(cha_fc, planarized_fc, overlapping_fc, output_gdb)
"""

import arcpy
import logging
import os
from gdb_utils import ensure_file_gdb, is_file_gdb

LOG = logging.getLogger(__name__)


def run_cha_intersection(
    cha_fc,
    planarized_fc,
    overlapping_fc,
    output_gdb,
    planarized_out_name="designations_planarized_cha",
    overlapping_out_name="designations_overlapping_cha",
):
    """
    Intersect designation layers with CHA and calculate overlap percentages.

    Parameters
    ----------
    cha_fc : str
        Path to the Critical Habitat Area feature class.
    planarized_fc : str
        Path to the designations_planarized feature class.
    overlapping_fc : str
        Path to the designations_overlapping feature class.
    output_gdb : str
        Path to the output geodatabase for results.
    planarized_out_name : str
        Name for the planarized intersect output feature class.
    overlapping_out_name : str
        Name for the overlapping intersect output feature class.

    Returns
    -------
    dict
        Paths to the created outputs: planarized_intersect,
        overlapping_intersect, summary_table, cha_protection_summary.
    """

    # --------------------------------------------------
    # Setup: build output paths and configure environment
    # --------------------------------------------------
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    ensure_file_gdb(output_gdb, recreate_invalid=True, logger=LOG)
    if not is_file_gdb(output_gdb):
        raise RuntimeError(f"Output path is not a valid File Geodatabase: {output_gdb}")

    planarized_intersect = os.path.join(output_gdb, planarized_out_name)
    overlapping_intersect = os.path.join(output_gdb, overlapping_out_name)

    # Derive a suffix from the output name for summary tables.
    # The planarized output name follows the pattern:
    #   "designations_planarized[_date_filter]_cha[_MM_DD]"
    # We split at "_cha" to separate the filter tag from the date portion.
    # Examples:
    #   "designations_planarized_cha_03_19"              → filter_tag="", date_part="_03_19"
    #   "designations_planarized_date_filter_cha_03_20"  → filter_tag="_date_filter", date_part="_03_20"
    #   "designations_planarized_cha"                    → filter_tag="", date_part=""
    cha_idx = planarized_out_name.find("_cha")
    if cha_idx != -1:
        filter_tag = planarized_out_name[len("designations_planarized"):cha_idx]
        date_part = planarized_out_name[cha_idx + len("_cha"):]
    else:
        filter_tag = planarized_out_name[len("designations_planarized"):]
        date_part = ""

    summary_name = f"cha_overlap_summary{filter_tag}{date_part}"
    total_area_name = f"designation_total_area{filter_tag}{date_part}"
    cha_protection_name = f"cha_protection_summary{filter_tag}{date_part}"

    print("=" * 60)
    print("  CHA INTERSECT + OVERLAP % CALCULATION")
    print("=" * 60)

    # Log and print all input/output paths for traceability
    print(f"  CHA feature class     : {cha_fc}")
    print(f"  Planarized input      : {planarized_fc}")
    print(f"  Overlapping input     : {overlapping_fc}")
    print(f"  Output GDB            : {output_gdb}")
    print(f"  Planarized output     : {planarized_out_name}")
    print(f"  Overlapping output    : {overlapping_out_name}")
    print(f"  Summary table         : {summary_name}")
    print("=" * 60)

    LOG.info("CHA intersection starting")
    LOG.info("  CHA FC      : %s", cha_fc)
    LOG.info("  Planarized  : %s", planarized_fc)
    LOG.info("  Overlapping : %s", overlapping_fc)
    LOG.info("  Output GDB  : %s", output_gdb)

    # --------------------------------------------------
    # Validate inputs exist before proceeding
    # --------------------------------------------------
    print("[Validate] Checking input datasets exist...")
    for label, path in [("CHA", cha_fc), ("Planarized", planarized_fc),
                        ("Overlapping", overlapping_fc), ("Output GDB", output_gdb)]:
        if not arcpy.Exists(path):
            msg = f"ERROR: {label} not found at: {path}"
            print(msg)
            LOG.error(msg)
            raise FileNotFoundError(msg)
        else:
            count = ""
            if label != "Output GDB":
                count = f" ({arcpy.management.GetCount(path)[0]} features)"
            print(f"  [OK] {label}{count}")
            LOG.info("  [OK] %s%s", label, count)

    # --------------------------------------------------
    # 1. PAIRWISE INTERSECT
    #    Intersect each designation layer with the CHA
    #    polygons. Output retains ALL fields from both inputs.
    # --------------------------------------------------
    print("\n[Step 1/9] Running Pairwise Intersect (planarized × CHA)...")
    LOG.info("PairwiseIntersect: planarized × CHA → %s", planarized_intersect)

    arcpy.analysis.PairwiseIntersect(
        [planarized_fc, cha_fc],
        planarized_intersect,
        "ALL"
    )

    planarized_count = arcpy.management.GetCount(planarized_intersect)[0]
    print(f"  Created: {planarized_out_name} ({planarized_count} features)")
    LOG.info("  Planarized intersect: %s features", planarized_count)

    print("[Step 1/9] Running Pairwise Intersect (overlapping × CHA)...")
    LOG.info("PairwiseIntersect: overlapping × CHA → %s", overlapping_intersect)

    arcpy.analysis.PairwiseIntersect(
        [overlapping_fc, cha_fc],
        overlapping_intersect,
        "ALL"
    )

    overlapping_count = arcpy.management.GetCount(overlapping_intersect)[0]
    print(f"  Created: {overlapping_out_name} ({overlapping_count} features)")
    LOG.info("  Overlapping intersect: %s features", overlapping_count)

    print("[Step 1/9] Pairwise Intersect complete.\n")

    # --------------------------------------------------
    # 2. ADD AREA FIELDS
    #    Add Overlap_Area_ha to BOTH intersect results and
    #    Total_Area_ha to the original overlapping layer.
    # --------------------------------------------------
    print("[Step 2/9] Adding area fields...")

    for label, fc in [("overlapping intersect", overlapping_intersect),
                      ("planarized intersect", planarized_intersect)]:
        if "Overlap_Area_ha" not in [f.name for f in arcpy.ListFields(fc)]:
            arcpy.management.AddField(fc, "Overlap_Area_ha", "DOUBLE")
            print(f"  Added Overlap_Area_ha to {label}")
        else:
            print(f"  Overlap_Area_ha already exists in {label}")

    if "Total_Area_ha" not in [f.name for f in arcpy.ListFields(overlapping_fc)]:
        arcpy.management.AddField(overlapping_fc, "Total_Area_ha", "DOUBLE")
        print("  Added Total_Area_ha to overlapping layer")
    else:
        print("  Total_Area_ha already exists in overlapping layer")

    LOG.info("Area fields added")

    # --------------------------------------------------
    # 3. CALCULATE AREAS (HECTARES)
    #    Use AREA_GEODESIC for accurate area on the
    #    ellipsoid regardless of projection distortion.
    # --------------------------------------------------
    print("[Step 3/9] Calculating geodesic areas (hectares)...")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_intersect,
        [["Overlap_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )
    print("  Calculated Overlap_Area_ha on overlapping intersect")

    arcpy.management.CalculateGeometryAttributes(
        planarized_intersect,
        [["Overlap_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )
    print("  Calculated Overlap_Area_ha on planarized intersect")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_fc,
        [["Total_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )
    print("  Calculated Total_Area_ha on overlapping layer")

    LOG.info("Geodesic area calculation complete")

    # --------------------------------------------------
    # 3b. PER-FEATURE CHA PROTECTION PERCENTAGE
    #     CHA_Protected_Pct = (Overlap_Area_ha / Area_ha) * 100
    #     Shows what % of each original CHA polygon is covered
    #     by the intersecting designation piece.
    #     Area_ha is the original CHA polygon area carried
    #     through from the CHA source feature class.
    #     Wrapped in try/except so the pipeline continues
    #     even if this calculation fails.
    # --------------------------------------------------
    cha_pct_ok = True
    try:
        print("[Step 3b/9] Calculating per-feature CHA protection percentage...")

        # Verify the Area_ha field carried through from the CHA source
        for label, fc in [("overlapping intersect", overlapping_intersect),
                          ("planarized intersect", planarized_intersect)]:
            fc_fields = [f.name for f in arcpy.ListFields(fc)]
            if "Area_ha" not in fc_fields:
                msg = (f"WARNING: 'Area_ha' field not found in {label}. "
                       "CHA protection percentage cannot be calculated. "
                       f"Available fields: {fc_fields}")
                print(f"  {msg}")
                LOG.warning(msg)
                cha_pct_ok = False
                break

        if cha_pct_ok:
            safe_pct_codeblock = (
                "def safe_pct(overlap, original):\n"
                "    if original is None or original <= 0 or overlap is None:\n"
                "        return None\n"
                "    return min((overlap / original) * 100, 100.0)\n"
            )

            for label, fc in [("overlapping intersect", overlapping_intersect),
                              ("planarized intersect", planarized_intersect)]:
                if "CHA_Protected_Pct" not in [f.name for f in arcpy.ListFields(fc)]:
                    arcpy.management.AddField(fc, "CHA_Protected_Pct", "DOUBLE")

                arcpy.management.CalculateField(
                    fc,
                    "CHA_Protected_Pct",
                    "safe_pct(!Overlap_Area_ha!, !Area_ha!)",
                    "PYTHON3",
                    safe_pct_codeblock,
                )
                print(f"  Calculated CHA_Protected_Pct on {label}")

            LOG.info("Per-feature CHA_Protected_Pct calculation complete")

    except Exception:
        LOG.warning("CHA_Protected_Pct calculation failed — pipeline continues",
                    exc_info=True)
        print("  WARNING: CHA_Protected_Pct calculation failed. "
              "See log for details. Pipeline continues.")
        cha_pct_ok = False

    # --------------------------------------------------
    # 4. SUM OVERLAP AREA by designation
    #    Aggregate the intersected overlap area per designation
    #    category using Summary Statistics.
    # --------------------------------------------------
    print("[Step 4/9] Summarizing overlap area by designation...")

    summary_table = os.path.join(output_gdb, summary_name)

    if arcpy.Exists(summary_table):
        arcpy.management.Delete(summary_table)
        print(f"  Deleted existing {summary_name}")

    arcpy.analysis.Statistics(
        overlapping_intersect,
        summary_table,
        [["Overlap_Area_ha", "SUM"]],
        ["designation"]
    )

    summary_rows = arcpy.management.GetCount(summary_table)[0]
    print(f"  Created {summary_name} ({summary_rows} designation groups)")
    LOG.info("Overlap area summary: %s rows → %s", summary_rows, summary_table)

    # --------------------------------------------------
    # 5. SUM TOTAL AREA by designation
    #    Aggregate the total area of each designation from
    #    the original overlapping layer (before intersection).
    # --------------------------------------------------
    print("[Step 5/9] Summarizing total designation area...")

    total_area_table = os.path.join(output_gdb, total_area_name)

    if arcpy.Exists(total_area_table):
        arcpy.management.Delete(total_area_table)
        print(f"  Deleted existing {total_area_name}")

    arcpy.analysis.Statistics(
        overlapping_fc,
        total_area_table,
        [["Total_Area_ha", "SUM"]],
        ["designation"]
    )

    total_rows = arcpy.management.GetCount(total_area_table)[0]
    print(f"  Created {total_area_name} ({total_rows} designation groups)")
    LOG.info("Total area summary: %s rows → %s", total_rows, total_area_table)

    # --------------------------------------------------
    # 6. JOIN TABLES
    #    Join total designation area into the CHA summary
    #    table so both SUM_Overlap_Area_ha and SUM_Total_Area_ha
    #    are side by side for the percentage calculation.
    # --------------------------------------------------
    print("[Step 6/9] Joining total area into CHA summary table...")

    arcpy.management.JoinField(
        summary_table,
        "designation",
        total_area_table,
        "designation",
        ["SUM_Total_Area_ha"]
    )

    print(f"  Joined SUM_Total_Area_ha into {summary_name}")
    LOG.info("JoinField complete: SUM_Total_Area_ha → %s", summary_name)

    # --------------------------------------------------
    # 7. CALCULATE CHA OVERLAP PERCENTAGE
    #    CHA_Percent = (SUM_Overlap_Area_ha / SUM_Total_Area_ha) * 100
    #    Shows what % of each designation overlaps with CHA.
    # --------------------------------------------------
    print("[Step 7/9] Calculating CHA overlap percentage...")

    if "CHA_Percent" not in [f.name for f in arcpy.ListFields(summary_table)]:
        arcpy.management.AddField(summary_table, "CHA_Percent", "DOUBLE")

    arcpy.management.CalculateField(
        summary_table,
        "CHA_Percent",
        "(!SUM_Overlap_Area_ha! / !SUM_Total_Area_ha!) * 100",
        "PYTHON3"
    )

    print(f"  CHA_Percent calculated in {summary_name}")
    LOG.info("CHA_Percent calculation complete")

    # --------------------------------------------------
    # 8. CHA PROTECTION SUMMARY TABLE
    #    Aggregate total protected area per CHA polygon,
    #    grouped by CHA_Source_ID — the original ECCC OBJECTID
    #    stamped into the CHA feature class before export in
    #    create_cha.py. Using CHA_Source_ID (rather than the
    #    auto-reassigned FID_critical_habitat_area) means the
    #    key in this table can be joined directly back to the
    #    national CriticalHabitat.gdb on OBJECTID. (2026-05-27)
    #    Wrapped in try/except so the pipeline continues
    #    even if this step fails.
    # --------------------------------------------------
    cha_protection_table = os.path.join(output_gdb, cha_protection_name)
    try:
        print("[Step 8/9] Creating CHA protection summary table...")

        # Verify the required fields exist in the overlapping intersect
        oi_fields = [f.name for f in arcpy.ListFields(overlapping_intersect)]
        required = ["CHA_Source_ID", "Overlap_Area_ha", "Area_ha"]
        missing = [r for r in required if r not in oi_fields]
        if missing:
            msg = (f"WARNING: Fields {missing} not found in overlapping intersect. "
                   "CHA protection summary cannot be created. "
                   f"Available fields: {oi_fields}")
            print(f"  {msg}")
            LOG.warning(msg)
            raise ValueError(msg)

        if arcpy.Exists(cha_protection_table):
            arcpy.management.Delete(cha_protection_table)
            print(f"  Deleted existing {cha_protection_name}")

        arcpy.analysis.Statistics(
            overlapping_intersect,
            cha_protection_table,
            [["Overlap_Area_ha", "SUM"], ["Area_ha", "FIRST"]],
            ["CHA_Source_ID"]
        )

        protection_rows = arcpy.management.GetCount(cha_protection_table)[0]
        print(f"  Created {cha_protection_name} ({protection_rows} CHA polygons)")
        LOG.info("CHA protection summary: %s rows → %s",
                 protection_rows, cha_protection_table)

        # --------------------------------------------------
        # 9. CALCULATE TOTAL CHA PROTECTION PERCENTAGE
        #    Total_CHA_Protected_Pct =
        #        (SUM_Overlap_Area_ha / FIRST_Area_ha) * 100
        #    Shows what % of each CHA polygon is covered by
        #    all overlapping designations combined.
        # --------------------------------------------------
        print("[Step 9/9] Calculating total CHA protection percentage...")

        if "Total_CHA_Protected_Pct" not in [
            f.name for f in arcpy.ListFields(cha_protection_table)
        ]:
            arcpy.management.AddField(
                cha_protection_table, "Total_CHA_Protected_Pct", "DOUBLE"
            )

        safe_pct_codeblock = (
            "def safe_pct(overlap, original):\n"
            "    if original is None or original <= 0 or overlap is None:\n"
            "        return None\n"
            "    return min((overlap / original) * 100, 100.0)\n"
        )

        arcpy.management.CalculateField(
            cha_protection_table,
            "Total_CHA_Protected_Pct",
            "safe_pct(!SUM_Overlap_Area_ha!, !FIRST_Area_ha!)",
            "PYTHON3",
            safe_pct_codeblock,
        )

        print(f"  Total_CHA_Protected_Pct calculated in {cha_protection_name}")
        LOG.info("Total_CHA_Protected_Pct calculation complete")

    except Exception:
        LOG.warning("CHA protection summary table creation failed — pipeline continues",
                    exc_info=True)
        print("  WARNING: CHA protection summary table creation failed. "
              "See log for details. Pipeline continues.")
        cha_protection_table = None

    # --------------------------------------------------
    # Summary
    # --------------------------------------------------
    print("\n" + "=" * 60)
    print("  CHA INTERSECTION COMPLETE")
    print("=" * 60)
    print(f"  Planarized intersect      : {planarized_intersect}")
    print(f"  Overlapping intersect     : {overlapping_intersect}")
    print(f"  Summary table             : {summary_table}")
    if cha_protection_table:
        print(f"  CHA protection summary    : {cha_protection_table}")
    if cha_pct_ok:
        print(f"  Per-feature CHA_Protected_Pct : calculated")
    else:
        print(f"  Per-feature CHA_Protected_Pct : SKIPPED (see warnings above)")
    print("=" * 60)

    LOG.info("CHA intersection complete — results in %s", output_gdb)

    return {
        "planarized_intersect": planarized_intersect,
        "overlapping_intersect": overlapping_intersect,
        "summary_table": summary_table,
        "cha_protection_summary": cha_protection_table,
    }


# --------------------------------------------------
# Standalone mode
# --------------------------------------------------
if __name__ == "__main__":
    # Configure basic logging to console when running standalone
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    gdb = os.path.join(script_dir, "designatedlands.gdb")
    output_gdb = os.path.join(script_dir, "outputs", "designatedlands_output.gdb")

    print(f"Working GDB : {gdb}")
    print(f"Output GDB  : {output_gdb}")

    ensure_file_gdb(output_gdb, recreate_invalid=True, logger=LOG)

    run_cha_intersection(
        cha_fc=os.path.join(
            script_dir, "source_data",
            "critical_habitat_area.gdb", "critical_habitat_area"
        ),
        planarized_fc=os.path.join(gdb, "designations_planarized"),
        overlapping_fc=os.path.join(gdb, "designations_overlapping"),
        output_gdb=output_gdb,
    )