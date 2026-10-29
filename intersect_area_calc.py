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
    return_rows=False,
    row_limit=None,
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
    return_rows : bool
        If True, after the intersects are written, read the two output
        feature classes (excluding geometry) into lists of dicts and
        include them in the return value. Used by the pipeline xlsx
        report so the analyst does not need to open the GDB to inspect
        the result tables.
    row_limit : int or None
        When ``return_rows`` is True, cap each table at this many rows.
        ``None`` returns all rows. The returned dict reports both the
        actual count and whether the result was truncated.

    Returns
    -------
    dict
        Always contains ``planarized_intersect`` and ``overlapping_intersect``
        paths. When ``return_rows`` is True, also contains:
          - planarized_rows, overlapping_rows : list[dict]
          - planarized_total_rows, overlapping_total_rows : int
          - planarized_truncated, overlapping_truncated : bool
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

    # Point 1 (2026-10-01): the CHA denominator is now Cha_Area_Fed_Removed
    # (geodesic hectares of the post-federal-erase polygon, written by
    # create_cha.prepare_cha). Validate up front so SKIP_DOWNLOAD=True runs
    # against a stale cha_exported.gdb fail fast with an actionable message.
    cha_fields = [f.name for f in arcpy.ListFields(cha_fc)]
    if "Cha_Area_Fed_Removed" not in cha_fields:
        raise RuntimeError(
            "CHA input predates the federal-erase change (e.g. SKIP_DOWNLOAD=True "
            "reused an old cha_exported.gdb) \u2014 rerun with SKIP_DOWNLOAD=False."
        )

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
    #    Add Overlap_Area_Ha to BOTH intersect results.
    # --------------------------------------------------
    print("[Step 2/4] Adding area fields...")

    for label, fc in [
        ("overlapping intersect", overlapping_intersect),
        ("planarized intersect", planarized_intersect),
    ]:
        if "Overlap_Area_Ha" not in [f.name for f in arcpy.ListFields(fc)]:
            arcpy.management.AddField(fc, "Overlap_Area_Ha", "DOUBLE")
            print(f"  Added Overlap_Area_Ha to {label}")
        else:
            print(f"  Overlap_Area_Ha already exists in {label}")

    LOG.info("Area fields added")

    # --------------------------------------------------
    # 3. CALCULATE AREAS (HECTARES)
    #    Use AREA_GEODESIC for accurate area on the
    #    ellipsoid regardless of projection distortion.
    # --------------------------------------------------
    print("[Step 3/4] Calculating geodesic areas (hectares)...")

    arcpy.management.CalculateGeometryAttributes(
        overlapping_intersect,
        [["Overlap_Area_Ha", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )
    print("  Calculated Overlap_Area_Ha on overlapping intersect")

    arcpy.management.CalculateGeometryAttributes(
        planarized_intersect,
        [["Overlap_Area_Ha", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )
    print("  Calculated Overlap_Area_Ha on planarized intersect")

    LOG.info("Geodesic area calculation complete")

    # --------------------------------------------------
    # 4. PER-FEATURE CHA PROTECTION PERCENTAGE
    #    Pct_of_Cha_Prot_by_LandDes = (Overlap_Area_Ha / Cha_Area_Fed_Removed) * 100
    #    Shows what % of each post-federal-erase CHA polygon is covered
    #    by the intersecting designation piece.
    #    Cha_Area_Fed_Removed is the geodesic hectares of the CHA polygon
    #    AFTER federal land has been erased (written by create_cha).
    #    FID_critical_habitat_area_post_fed_erase now references post-erase OIDs;
    #    CHA_Source_ID remains the stable lineage key back to the
    #    original ECCC OBJECTID.
    # --------------------------------------------------
    cha_pct_ok = True
    try:
        print("[Step 4/4] Calculating per-feature CHA protection percentage...")

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
            if "Pct_of_Cha_Prot_by_LandDes" not in [f.name for f in arcpy.ListFields(fc)]:
                arcpy.management.AddField(fc, "Pct_of_Cha_Prot_by_LandDes", "DOUBLE")

            arcpy.management.CalculateField(
                fc,
                "Pct_of_Cha_Prot_by_LandDes",
                "safe_pct(!Overlap_Area_Ha!, !Cha_Area_Fed_Removed!)",
                "PYTHON3",
                safe_pct_codeblock,
            )
            print(f"  Calculated Pct_of_Cha_Prot_by_LandDes on {label}")

        LOG.info("Per-feature Pct_of_Cha_Prot_by_LandDes calculation complete")

    except Exception:
        LOG.warning(
            "Pct_of_Cha_Prot_by_LandDes calculation failed - pipeline continues",
            exc_info=True,
        )
        print(
            "  WARNING: Pct_of_Cha_Prot_by_LandDes calculation failed. "
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
        print("  Per-feature Pct_of_Cha_Prot_by_LandDes : calculated")
    else:
        print("  Per-feature Pct_of_Cha_Prot_by_LandDes : SKIPPED (see warnings above)")
    print("=" * 60)

    LOG.info("CHA intersection complete - results in %s", output_gdb)

    result = {
        "planarized_intersect": planarized_intersect,
        "overlapping_intersect": overlapping_intersect,
    }

    if return_rows:
        for key_prefix, fc_path in [
            ("planarized", planarized_intersect),
            ("overlapping", overlapping_intersect),
        ]:
            total = int(arcpy.management.GetCount(fc_path)[0])
            field_names = [
                f.name for f in arcpy.ListFields(fc_path)
                if f.type not in ("Geometry", "Blob", "Raster")
                and f.name.upper() not in ("SHAPE", "SHAPE_LENGTH", "SHAPE_AREA")
            ]
            rows = []
            limit = row_limit if row_limit is not None else total
            with arcpy.da.SearchCursor(fc_path, field_names) as cursor:
                for i, row in enumerate(cursor):
                    if i >= limit:
                        break
                    rows.append(dict(zip(field_names, row)))
            truncated = total > len(rows)
            result[f"{key_prefix}_rows"] = rows
            result[f"{key_prefix}_field_names"] = field_names
            result[f"{key_prefix}_total_rows"] = total
            result[f"{key_prefix}_truncated"] = truncated
            LOG.info(
                "CHA report rows captured for %s: %d of %d (truncated=%s)",
                key_prefix, len(rows), total, truncated,
            )

    return result


# arcpy Field.type -> AddField field_type, so a mirrored field keeps its lineage type.
_FIELD_TYPE_MAP = {
    "SmallInteger": "SHORT",
    "Integer": "LONG",
    "BigInteger": "BIGINTEGER",
    "Single": "FLOAT",
    "Double": "DOUBLE",
    "String": "TEXT",
    "Date": "DATE",
    "OID": "LONG",
    "GUID": "GUID",
    "Guid": "GUID",
    "GlobalID": "GUID",
}


def run_cha_unprotected_summary(
    cha_fc,
    designations_fc,
    overlapping_intersect_fc,
    output_gdb,
    summary_out_name="cha_unprotected_summary",
    remainder_out_name="cha_prov_unprotected_geom",
):
    """
    Per-CHA 'unprotected by anyone' summary.

    cha_fc : post-federal-erase CHA (critical_habitat_area_post_fed_erase),
             one row per surviving CHA_Source_ID, carrying CHA_Source_ID,
             ECCC_Cha_Area_Ha, Cha_Area_Fed_Removed.
    designations_fc : designations_planarized{suffix}. Planarized is already
             non-overlapping and covers exactly the same ground as the union
             of ALL designations (any designation = protection), so dissolving
             it is the full provincial-protection footprint and is far cheaper
             than dissolving the overlapping stack.
    overlapping_intersect_fc : designations_overlapping_cha — used only to find
             which CHA_Source_IDs have ANY designation overlap (bucket B vs C).
    """
    arcpy.env.overwriteOutput = True
    arcpy.env.parallelProcessingFactor = "100%"
    ensure_file_gdb(output_gdb, recreate_invalid=True, logger=LOG)
    if not is_file_gdb(output_gdb):
        raise RuntimeError(f"Output path is not a valid File Geodatabase: {output_gdb}")

    summary_table = os.path.join(output_gdb, summary_out_name)
    remainder_fc = os.path.join(output_gdb, remainder_out_name)
    mask_tmp = os.path.join(output_gdb, "cha_prov_protection_mask_tmp")

    print("=" * 60)
    print("  CHA UNPROTECTED (PROVINCIAL) SUMMARY")
    print("=" * 60)
    print(f"  CHA (post-fed-erase)  : {cha_fc}")
    print(f"  Designations (planar) : {designations_fc}")
    print(f"  Overlapping intersect : {overlapping_intersect_fc}")
    print(f"  Output GDB            : {output_gdb}")
    print(f"  Summary table         : {summary_out_name}")
    print(f"  Remainder geometry    : {remainder_out_name}")
    print("=" * 60)

    LOG.info("CHA unprotected summary starting")
    LOG.info("  CHA FC          : %s", cha_fc)
    LOG.info("  Designations FC : %s", designations_fc)
    LOG.info("  Overlapping FC  : %s", overlapping_intersect_fc)
    LOG.info("  Output GDB      : %s", output_gdb)

    for label, path in [
        ("CHA", cha_fc),
        ("Designations", designations_fc),
        ("Overlapping intersect", overlapping_intersect_fc),
    ]:
        if not arcpy.Exists(path):
            msg = f"ERROR: {label} not found at: {path}"
            print(msg)
            LOG.error(msg)
            raise FileNotFoundError(msg)

    # --------------------------------------------------
    # 1. PROTECTION MASK
    #    Dissolve the planarized stack (already non-overlapping) into a
    #    single multipart "all provincial protection" polygon.
    # --------------------------------------------------
    print("\n[Step 1/5] Dissolving planarized designations into protection mask...")
    LOG.info("PairwiseDissolve: planarized -> %s", mask_tmp)
    if arcpy.Exists(mask_tmp):
        arcpy.management.Delete(mask_tmp)
    arcpy.analysis.PairwiseDissolve(designations_fc, mask_tmp)

    # --------------------------------------------------
    # 2. SECOND ERASE
    #    Erase all provincial protection from the post-federal-erase CHA.
    #    The remainder is the unprotected-by-anyone geometry, one multipart
    #    row per CHA_Source_ID that still has any area left.
    # --------------------------------------------------
    print("[Step 2/5] Erasing protection mask from CHA (unprotected remainder)...")
    LOG.info("PairwiseErase: CHA - mask -> %s", remainder_fc)
    if arcpy.Exists(remainder_fc):
        arcpy.management.Delete(remainder_fc)
    arcpy.analysis.PairwiseErase(cha_fc, mask_tmp, remainder_fc)

    # --------------------------------------------------
    # 3. REMAINDER AREA (geodesic hectares)
    # --------------------------------------------------
    print("[Step 3/5] Calculating geodesic area of unprotected remainder...")
    if "Prov_Unprotect_Cha_Area" not in [f.name for f in arcpy.ListFields(remainder_fc)]:
        arcpy.management.AddField(remainder_fc, "Prov_Unprotect_Cha_Area", "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        remainder_fc,
        [["Prov_Unprotect_Cha_Area", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )

    # --------------------------------------------------
    # 4. REMAINDER AREA BY CHA_Source_ID
    # --------------------------------------------------
    unprot_by_id = {}
    with arcpy.da.SearchCursor(
        remainder_fc, ["CHA_Source_ID", "Prov_Unprotect_Cha_Area"]
    ) as cur:
        for cid, area in cur:
            unprot_by_id[cid] = area if area is not None else 0.0
    LOG.info("Unprotected remainder rows: %d", len(unprot_by_id))

    # --------------------------------------------------
    # 5. CHA_Source_IDs with ANY designation overlap (bucket B vs C)
    # --------------------------------------------------
    overlapped_ids = set()
    with arcpy.da.SearchCursor(overlapping_intersect_fc, ["CHA_Source_ID"]) as cur:
        for (cid,) in cur:
            overlapped_ids.add(cid)
    LOG.info("CHA_Source_IDs with >=1 designation overlap: %d", len(overlapped_ids))

    # --------------------------------------------------
    # 6. CREATE SUMMARY TABLE (standalone, no geometry)
    #    Fully-covered CHAs have no remainder geometry, so the summary is a
    #    table; the mappable geometry lives in remainder_fc.
    # --------------------------------------------------
    print("[Step 4/5] Creating summary table...")
    if arcpy.Exists(summary_table):
        arcpy.management.Delete(summary_table)
    arcpy.management.CreateTable(output_gdb, summary_out_name)

    # Mirror CHA_Source_ID type from cha_fc (its ECCC-OBJECTID lineage key).
    cha_id_field = next(
        f for f in arcpy.ListFields(cha_fc) if f.name == "CHA_Source_ID"
    )
    id_add_type = _FIELD_TYPE_MAP.get(cha_id_field.type, "LONG")
    if id_add_type == "TEXT":
        arcpy.management.AddField(
            summary_table, "CHA_Source_ID", "TEXT",
            field_length=cha_id_field.length,
        )
    else:
        arcpy.management.AddField(summary_table, "CHA_Source_ID", id_add_type)

    arcpy.management.AddField(summary_table, "ECCC_Cha_Area_Ha", "DOUBLE")
    arcpy.management.AddField(summary_table, "Cha_Area_Fed_Removed", "DOUBLE")
    arcpy.management.AddField(summary_table, "Prov_Unprotect_Cha_Area", "DOUBLE")
    arcpy.management.AddField(
        summary_table, "Fed_Prov_Unprotect_Cha_Pct", "DOUBLE",
        field_alias="Fed_Prov_Unprotect_Ch_%",
    )
    arcpy.management.AddField(summary_table, "No_Protection_Area_Ha", "DOUBLE")
    arcpy.management.AddField(summary_table, "No_Protection_Area_Pct", "DOUBLE")

    # --------------------------------------------------
    # 7. POPULATE, driven by cha_fc (one row per surviving CHA)
    # --------------------------------------------------
    print("[Step 5/5] Populating summary rows...")
    insert_fields = [
        "CHA_Source_ID", "ECCC_Cha_Area_Ha", "Cha_Area_Fed_Removed",
        "Prov_Unprotect_Cha_Area", "Fed_Prov_Unprotect_Cha_Pct",
        "No_Protection_Area_Ha", "No_Protection_Area_Pct",
    ]
    inserted = 0
    with arcpy.da.InsertCursor(summary_table, insert_fields) as i_cur:
        with arcpy.da.SearchCursor(
            cha_fc, ["CHA_Source_ID", "ECCC_Cha_Area_Ha", "Cha_Area_Fed_Removed"]
        ) as s_cur:
            for cid, eccc, fed_removed in s_cur:
                prov_unprot = unprot_by_id.get(cid, 0.0)  # 0 when fully provincially covered
                # Denominator is the FULL original ECCC_Cha_Area_Ha (not
                # Cha_Area_Fed_Removed): the federal slice sits in the
                # denominator as protected area. Cap at 100 to absorb
                # geodesic-vs-ECCC method differences (same reason create_cha's
                # audit allows post > pre on individual rows).
                pct = min((prov_unprot / eccc) * 100, 100.0) if eccc else None
                if cid not in overlapped_ids:  # bucket C: no designation overlap at all
                    no_prot_ha, no_prot_pct = prov_unprot, pct
                else:  # bucket B: leave No_Protection_* null
                    no_prot_ha, no_prot_pct = None, None
                i_cur.insertRow([
                    cid, eccc, fed_removed, prov_unprot, pct,
                    no_prot_ha, no_prot_pct,
                ])
                inserted += 1

    LOG.info("Summary rows inserted: %d", inserted)

    # Clean up the temporary protection mask (remainder_fc is kept).
    try:
        arcpy.management.Delete(mask_tmp)
    except Exception:
        LOG.warning("Could not delete temp mask %s", mask_tmp)

    # --------------------------------------------------
    # Return rows for the next prompt to consume (same shape as
    # run_cha_intersection's *_rows), plus the join keys it needs.
    # --------------------------------------------------
    summary_field_names = [
        f.name for f in arcpy.ListFields(summary_table)
        if f.type not in ("Geometry", "Blob", "Raster")
        and f.name.upper() not in ("OBJECTID", "SHAPE", "SHAPE_LENGTH", "SHAPE_AREA")
    ]
    summary_rows = []
    with arcpy.da.SearchCursor(summary_table, summary_field_names) as cur:
        for row in cur:
            summary_rows.append(dict(zip(summary_field_names, row)))

    print("\n" + "=" * 60)
    print("  CHA UNPROTECTED SUMMARY COMPLETE")
    print("=" * 60)
    print(f"  Summary table   : {summary_table} ({inserted} rows)")
    print(f"  Remainder geom  : {remainder_fc}")
    print("=" * 60)
    LOG.info("CHA unprotected summary complete - %d rows", inserted)

    return {
        "summary_table": summary_table,
        "remainder_fc": remainder_fc,
        "summary_rows": summary_rows,
        "summary_field_names": summary_field_names,
        "summary_total_rows": inserted,
        "unprot_by_id": unprot_by_id,
        "overlapped_ids": overlapped_ids,
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
            "cha_exported.gdb", "critical_habitat_area_post_fed_erase"
        ),
        planarized_fc=os.path.join(gdb, "designations_planarized"),
        overlapping_fc=os.path.join(gdb, "designations_overlapping"),
        output_gdb=output_gdb,
    )
