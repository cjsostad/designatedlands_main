"""
perform_area_calculation.py — CHA intersection and overlap percentage calculation.

Intersects designations_planarized and designations_overlapping with
Critical Habitat Area (CHA), then calculates per-designation overlap
percentages via Statistics + JoinField.

Can be run standalone or imported as a module:

    Standalone:
        python perform_area_calculation.py

    As module (from pipeline scripts):
        from perform_area_calculation import run_cha_intersection
        run_cha_intersection(cha_fc, planarized_fc, overlapping_fc, output_gdb)
"""

import arcpy
import os


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
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"

    planarized_intersect = os.path.join(output_gdb, planarized_out_name)
    overlapping_intersect = os.path.join(output_gdb, overlapping_out_name)

    # Derive a suffix from the output name for summary tables
    suffix = planarized_out_name.replace("designations_planarized_cha", "")
    summary_name = f"cha_overlap_summary{suffix}"
    total_area_name = f"designation_total_area{suffix}"

    print("Starting CHA intersect + overlap % calculation...")

    # --------------------------------------------------
    # 1. PAIRWISE INTERSECT
    # --------------------------------------------------
    print("Running Pairwise Intersect...")

    arcpy.analysis.PairwiseIntersect(
        [planarized_fc, cha_fc],
        planarized_intersect,
        "ALL"
    )

    arcpy.analysis.PairwiseIntersect(
        [overlapping_fc, cha_fc],
        overlapping_intersect,
        "ALL"
    )

    print("Intersect complete.")

    # --------------------------------------------------
    # 2. ADD AREA FIELDS
    # --------------------------------------------------
    if "CHA_Area_ha" not in [f.name for f in arcpy.ListFields(overlapping_intersect)]:
        arcpy.management.AddField(overlapping_intersect, "CHA_Area_ha", "DOUBLE")

    if "Total_Area_ha" not in [f.name for f in arcpy.ListFields(overlapping_fc)]:
        arcpy.management.AddField(overlapping_fc, "Total_Area_ha", "DOUBLE")

    # --------------------------------------------------
    # 3. CALCULATE AREAS (HECTARES)
    # --------------------------------------------------
    arcpy.management.CalculateGeometryAttributes(
        overlapping_intersect,
        [["CHA_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )

    arcpy.management.CalculateGeometryAttributes(
        overlapping_fc,
        [["Total_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES"
    )

    # --------------------------------------------------
    # 4. SUM CHA AREA
    # --------------------------------------------------
    summary_table = os.path.join(output_gdb, summary_name)

    if arcpy.Exists(summary_table):
        arcpy.management.Delete(summary_table)

    arcpy.analysis.Statistics(
        overlapping_intersect,
        summary_table,
        [["CHA_Area_ha", "SUM"]],
        ["designation"]
    )

    # --------------------------------------------------
    # 5. SUM TOTAL AREA
    # --------------------------------------------------
    total_area_table = os.path.join(output_gdb, total_area_name)

    if arcpy.Exists(total_area_table):
        arcpy.management.Delete(total_area_table)

    arcpy.analysis.Statistics(
        overlapping_fc,
        total_area_table,
        [["Total_Area_ha", "SUM"]],
        ["designation"]
    )

    # --------------------------------------------------
    # 6. JOIN TABLES
    # --------------------------------------------------
    arcpy.management.JoinField(
        summary_table,
        "designation",
        total_area_table,
        "designation",
        ["SUM_Total_Area_ha"]
    )

    # --------------------------------------------------
    # 7. CALCULATE %
    # --------------------------------------------------
    if "CHA_Percent" not in [f.name for f in arcpy.ListFields(summary_table)]:
        arcpy.management.AddField(summary_table, "CHA_Percent", "DOUBLE")

    arcpy.management.CalculateField(
        summary_table,
        "CHA_Percent",
        "(!SUM_CHA_Area_ha! / !SUM_Total_Area_ha!) * 100",
        "PYTHON3"
    )

    print(f"DONE — Check: {summary_name}")

    return {
        "planarized_intersect": planarized_intersect,
        "overlapping_intersect": overlapping_intersect,
        "summary_table": summary_table,
    }


# --------------------------------------------------
# Standalone mode
# --------------------------------------------------
if __name__ == "__main__":
    gdb = r"\\spatialfiles.bcgov\srm\gss\sandbox\srahimi\designatedlands_main\designatedlands.gdb"

    run_cha_intersection(
        cha_fc=os.path.join(gdb, "critical_habitat_area"),
        planarized_fc=os.path.join(gdb, "designations_planarized"),
        overlapping_fc=os.path.join(gdb, "designations_overlapping"),
        output_gdb=gdb,
    )