"""
intersect_area_calc.py — CHA intersection and overlap percentage calculation.

Intersects designations_planarized and designations_overlapping with
Critical Habitat Area (CHA), then calculates per-designation overlap
percentages via Statistics + JoinField.

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
        overlapping_intersect, summary_table.
    """

    # --------------------------------------------------
    # Setup: build output paths and configure environment
    # --------------------------------------------------
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"

    planarized_intersect = os.path.join(output_gdb, planarized_out_name)
    overlapping_intersect = os.path.join(output_gdb, overlapping_out_name)

    # Derive a suffix from the output name for summary tables
    # e.g. "designations_planarized_cha_03_19" → suffix = "_03_19"
    suffix = planarized_out_name.replace("designations_planarized_cha", "")
    summary_name = f"cha_overlap_summary{suffix}"
    total_area_name = f"designation_total_area{suffix}"

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
    print("\n[Step 1/7] Running Pairwise Intersect (planarized × CHA)...")
    LOG.info("PairwiseIntersect: planarized × CHA → %s", planarized_intersect)

    arcpy.analysis.PairwiseIntersect(
        [planarized_fc, cha_fc],
        planarized_intersect,
        "ALL"
    )

    planarized_count = arcpy.management.GetCount(planarized_intersect)[0]
    print(f"  Created: {planarized_out_name} ({planarized_count} features)")
    LOG.info("  Planarized intersect: %s features", planarized_count)

    print("[Step 1/7] Running Pairwise Intersect (overlapping × CHA)...")
    LOG.info("PairwiseIntersect: overlapping × CHA → %s", overlapping_intersect)

    arcpy.analysis.PairwiseIntersect(
        [overlapping_fc, cha_fc],
        overlapping_intersect,
        "ALL"
    )

    overlapping_count = arcpy.management.GetCount(overlapping_intersect)[0]
    print(f"  Created: {overlapping_out_name} ({overlapping_count} features)")
    LOG.info("  Overlapping intersect: %s features", overlapping_count)

    print("[Step 1/7] Pairwise Intersect complete.\n")

    # --------------------------------------------------
    # 2. ADD AREA FIELDS
    #    Add CHA_Area_ha to the intersect result and
    #    Total_Area_ha to the original overlapping layer.
    # --------------------------------------------------
    print("[Step 2/7] Adding area fields...")

    if "CHA_Area_ha" not in [f.name for f in arcpy.ListFields(overlapping_intersect)]:
        arcpy.management.AddField(overlapping_intersect, "CHA_Area_ha", "DOUBLE")
        print("  Added CHA_Area_ha to intersect result")
    else:
        print("  CHA_Area_ha already exists in intersect result")

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
    print("[Step 3/7] Calculating geodesic areas (hectares)...")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_intersect,
        [["CHA_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )
    print("  Calculated CHA_Area_ha on intersect result")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_fc,
        [["Total_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )
    print("  Calculated Total_Area_ha on overlapping layer")

    LOG.info("Geodesic area calculation complete")

    # --------------------------------------------------
    # 4. SUM CHA AREA by designation
    #    Aggregate the intersected CHA area per designation
    #    category using Summary Statistics.
    # --------------------------------------------------
    print("[Step 4/7] Summarizing CHA area by designation...")

    summary_table = os.path.join(output_gdb, summary_name)

    if arcpy.Exists(summary_table):
        arcpy.management.Delete(summary_table)
        print(f"  Deleted existing {summary_name}")

    arcpy.analysis.Statistics(
        overlapping_intersect,
        summary_table,
        [["CHA_Area_ha", "SUM"]],
        ["designation"]
    )

    summary_rows = arcpy.management.GetCount(summary_table)[0]
    print(f"  Created {summary_name} ({summary_rows} designation groups)")
    LOG.info("CHA area summary: %s rows → %s", summary_rows, summary_table)

    # --------------------------------------------------
    # 5. SUM TOTAL AREA by designation
    #    Aggregate the total area of each designation from
    #    the original overlapping layer (before intersection).
    # --------------------------------------------------
    print("[Step 5/7] Summarizing total designation area...")

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
    #    table so both SUM_CHA_Area_ha and SUM_Total_Area_ha
    #    are side by side for the percentage calculation.
    # --------------------------------------------------
    print("[Step 6/7] Joining total area into CHA summary table...")

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
    #    CHA_Percent = (SUM_CHA_Area_ha / SUM_Total_Area_ha) * 100
    #    Shows what % of each designation overlaps with CHA.
    # --------------------------------------------------
    print("[Step 7/7] Calculating CHA overlap percentage...")

    if "CHA_Percent" not in [f.name for f in arcpy.ListFields(summary_table)]:
        arcpy.management.AddField(summary_table, "CHA_Percent", "DOUBLE")

    arcpy.management.CalculateField(
        summary_table,
        "CHA_Percent",
        "(!SUM_CHA_Area_ha! / !SUM_Total_Area_ha!) * 100",
        "PYTHON3"
    )

    print(f"  CHA_Percent calculated in {summary_name}")
    LOG.info("CHA_Percent calculation complete")

    # --------------------------------------------------
    # Summary
    # --------------------------------------------------
    print("\n" + "=" * 60)
    print("  CHA INTERSECTION COMPLETE")
    print("=" * 60)
    print(f"  Planarized intersect : {planarized_intersect}")
    print(f"  Overlapping intersect: {overlapping_intersect}")
    print(f"  Summary table        : {summary_table}")
    print("=" * 60)

    LOG.info("CHA intersection complete — results in %s", output_gdb)

    return {
        "planarized_intersect": planarized_intersect,
        "overlapping_intersect": overlapping_intersect,
        "summary_table": summary_table,
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

    if not arcpy.Exists(output_gdb):
        print(f"Creating output GDB: {output_gdb}")
        arcpy.management.CreateFileGDB(os.path.join(script_dir, "outputs"), "designatedlands_output.gdb")

    run_cha_intersection(
        cha_fc=os.path.join(
            script_dir, "source_data",
            "critical_habitat_area.gdb", "critical_habitat_area"
        ),
        planarized_fc=os.path.join(gdb, "designations_planarized"),
        overlapping_fc=os.path.join(gdb, "designations_overlapping"),
        output_gdb=output_gdb,
    )