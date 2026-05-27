"""
intersect_area_calc.py — CHA intersection and overlap percentage calculation.

Intersects designations_planarized and designations_overlapping with
Critical Habitat Area (CHA), calculates overlap area on each intersect
output, and computes per-feature CHA protection percentages (how much
of each CHA polygon is covered by each designation piece).

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
        overlapping_intersect.
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
    for label, path in [
        ("CHA", cha_fc),
        ("Planarized", planarized_fc),
        ("Overlapping", overlapping_fc),
        ("Output GDB", output_gdb),
    ]:
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
    print("\n[Step 1/4] Running Pairwise Intersect (planarized x CHA)...")
    LOG.info("PairwiseIntersect: planarized x CHA -> %s", planarized_intersect)

    arcpy.analysis.PairwiseIntersect([planarized_fc, cha_fc], planarized_intersect, "ALL")

    planarized_count = arcpy.management.GetCount(planarized_intersect)[0]
    print(f"  Created: {planarized_out_name} ({planarized_count} features)")
    LOG.info("  Planarized intersect: %s features", planarized_count)

    print("[Step 1/4] Running Pairwise Intersect (overlapping x CHA)...")
    LOG.info("PairwiseIntersect: overlapping x CHA -> %s", overlapping_intersect)

    arcpy.analysis.PairwiseIntersect([overlapping_fc, cha_fc], overlapping_intersect, "ALL")

    overlapping_count = arcpy.management.GetCount(overlapping_intersect)[0]
    print(f"  Created: {overlapping_out_name} ({overlapping_count} features)")
    LOG.info("  Overlapping intersect: %s features", overlapping_count)

    print("[Step 1/4] Pairwise Intersect complete.\n")

    # --------------------------------------------------
    # 2. ADD AREA FIELDS
    #    Add Overlap_Area_ha to BOTH intersect results.
    # --------------------------------------------------
    print("[Step 2/4] Adding area fields...")

    for label, fc in [
        ("overlapping intersect", overlapping_intersect),
        ("planarized intersect", planarized_intersect),
    ]:
        if "Overlap_Area_ha" not in [f.name for f in arcpy.ListFields(fc)]:
            arcpy.management.AddField(fc, "Overlap_Area_ha", "DOUBLE")
            print(f"  Added Overlap_Area_ha to {label}")
        else:
            print(f"  Overlap_Area_ha already exists in {label}")

    LOG.info("Area fields added")

    # --------------------------------------------------
    # 3. CALCULATE AREAS (HECTARES)
    #    Use AREA_GEODESIC for accurate area on the
    #    ellipsoid regardless of projection distortion.
    # --------------------------------------------------
    print("[Step 3/4] Calculating geodesic areas (hectares)...")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_intersect,
        [["Overlap_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )
    print("  Calculated Overlap_Area_ha on overlapping intersect")

    arcpy.management.CalculateGeometryAttributes(
        planarized_intersect,
        [["Overlap_Area_ha", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )
    print("  Calculated Overlap_Area_ha on planarized intersect")

    LOG.info("Geodesic area calculation complete")

    # --------------------------------------------------
    # 4. PER-FEATURE CHA PROTECTION PERCENTAGE
    #    CHA_Protected_Pct = (Overlap_Area_ha / Area_ha) * 100
    #    Shows what % of each original CHA polygon is covered
    #    by the intersecting designation piece.
    #    Area_ha is the original CHA polygon area carried
    #    through from the CHA source feature class.
    #    Wrapped in try/except so the pipeline continues
    #    even if this calculation fails.
    # --------------------------------------------------
    cha_pct_ok = True
    try:
        print("[Step 4/4] Calculating per-feature CHA protection percentage...")

        # Verify the Area_ha field carried through from the CHA source
        for label, fc in [
            ("overlapping intersect", overlapping_intersect),
            ("planarized intersect", planarized_intersect),
        ]:
            fc_fields = [f.name for f in arcpy.ListFields(fc)]
            if "Area_ha" not in fc_fields:
                msg = (
                    f"WARNING: 'Area_ha' field not found in {label}. "
                    "CHA protection percentage cannot be calculated. "
                    f"Available fields: {fc_fields}"
                )
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

            for label, fc in [
                ("overlapping intersect", overlapping_intersect),
                ("planarized intersect", planarized_intersect),
            ]:
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
        LOG.warning(
            "CHA_Protected_Pct calculation failed - pipeline continues",
            exc_info=True,
        )
        print(
            "  WARNING: CHA_Protected_Pct calculation failed. "
            "See log for details. Pipeline continues."
        )
        cha_pct_ok = False

    # --------------------------------------------------
    # Summary
    # --------------------------------------------------
    print("\n" + "=" * 60)
    print("  CHA INTERSECTION COMPLETE")
    print("=" * 60)
    print(f"  Planarized intersect      : {planarized_intersect}")
    print(f"  Overlapping intersect     : {overlapping_intersect}")
    if cha_pct_ok:
        print("  Per-feature CHA_Protected_Pct : calculated")
    else:
        print("  Per-feature CHA_Protected_Pct : SKIPPED (see warnings above)")
    print("=" * 60)

    LOG.info("CHA intersection complete - results in %s", output_gdb)

    return {
        "planarized_intersect": planarized_intersect,
        "overlapping_intersect": overlapping_intersect,
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
