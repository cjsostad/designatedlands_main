import arcpy
import os

# --------------------------------------------------
# INPUTS (UPDATE THESE)
# --------------------------------------------------
gdb = r"\\spatialfiles.bcgov\srm\gss\sandbox\srahimi\designatedlands_main\designatedlands.gdb"

cha_fc = os.path.join(
    r"\\spatialfiles.bcgov\srm\gss\sandbox\srahimi\designatedlands_main\designatedlands.gdb",
    "critical_habitat_area"
)

# Existing layers
planarized_fc = os.path.join(gdb, "designations_planarized")
overlapping_fc = os.path.join(gdb, "designations_overlapping")

# Output layers (AOI-based intersect)
planarized_intersect = os.path.join(gdb, "designations_planarized_cha")
overlapping_intersect = os.path.join(gdb, "designations_overlapping_cha")

# --------------------------------------------------
# SETTINGS
# --------------------------------------------------
arcpy.env.workspace = gdb
arcpy.env.overwriteOutput = True
arcpy.env.parallelProcessingFactor = "100%"

print("Starting AOI intersect + CHA % calculation...")

# --------------------------------------------------
# 1. INTERSECT (FAST TEST STEP)
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
summary_table = os.path.join(gdb, "cha_overlap_summary")

arcpy.analysis.Statistics(
    overlapping_intersect,
    summary_table,
    [["CHA_Area_ha", "SUM"]],
    ["designation"]   
)

# --------------------------------------------------
# 5. SUM TOTAL AREA
# --------------------------------------------------
total_area_table = os.path.join(gdb, "designation_total_area")

arcpy.analysis.Statistics(
    overlapping_fc,
    total_area_table,
    [["Total_Area_ha", "SUM"]],
    ["designation"]   # <-- MUST MATCH ABOVE
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

print("DONE ✅ Check: cha_overlap_summary")