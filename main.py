"""
Run the full designatedlands pipeline in sequence....

This script orchestrates the complete Designated Lands analysis for
British Columbia, combining 40+ spatial designation layers (parks,
conservancies, wildlife management areas, etc.) into two output
feature classes:

  - designations_overlapping: all designations stacked/unioned, allowing
    overlapping polygons (a single area can carry multiple designations).
  - designations_planarized: a non-overlapping layer where each polygon
    is assigned the single highest-priority designation (based on
    process_order from the CSV).

Pipeline steps:
  1. test-connection  — Verify the File Geodatabase is accessible.
  2. download         — Fetch each designation layer from BCGW via WFS
                        (or load from local file for manual sources).
                        Skips any layer already present in the GDB.
  3. preprocess       — Apply per-source operations (dissolve by attribute,
                        clip by another layer) and build the combined
                        BC land+marine boundary polygon.
  4. process-vector   — Build the two main output feature classes:
                        designations_overlapping and designations_planarized.
  5. process-raster   — (Optional, requires Spatial Analyst) Convert vector
                        outputs to raster TIFs for designation and
                        restriction levels.
  6. dump             — Export the two output feature classes from the
                        working GDB into a clean output File Geodatabase
                        (outputs/designatedlands_output.gdb).
  7. cleanup          — Remove intermediate feature classes (src_*, *_pp)
                        from the working GDB to free disk space.

Defaults (can be run with no arguments from VS Code):
  - Federal layers (National Parks, National Wildlife Areas, Migratory
    Bird Sanctuaries) are excluded by default (--exclude-federal).
  - Raster processing is off by default (no Spatial Analyst license).
  - All other steps run automatically.

Usage:
    python main.py
    python main.py --config path/to/config.cfg
    python main.py --skip-cleanup
    python main.py --no-exclude-federal
    python main.py --raster
    python main.py --verbose
"""
import argparse
import logging
import os
import sys
import arcpy

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level
from Create_CHA_AOI import prepare_cha   # <-- import CHA script

LOG = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run the full designatedlands pipeline."
    )

    parser.add_argument("--config", "-c", metavar="CONFIG_FILE", default=None)

    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")

    parser.add_argument("--skip-download", action="store_true")

    parser.add_argument(
        "--raster",
        action=argparse.BooleanOptionalAction,
        default=False,
    )

    parser.add_argument("--skip-cleanup", action="store_true")

    parser.add_argument("--recent-only", action="store_true")

    parser.add_argument("--start-date", metavar="YYYY-MM-DD", default=None)

    parser.add_argument("--end-date", metavar="YYYY-MM-DD", default=None)

    parser.add_argument(
        "--exclude-federal",
        action=argparse.BooleanOptionalAction,
        default=True,
    )

    return parser


def main():

    parser = build_parser()
    args = parser.parse_args()

    # ---------------------------------
    # Get script directory
    # ---------------------------------
    script_dir = os.path.dirname(os.path.abspath(__file__))

    # ---------------------------------
    # Logging setup
    # ---------------------------------
    logs_dir = os.path.join(script_dir, "logs")
    log_path = set_log_level(args.verbose, args.quiet, log_dir=logs_dir)

    LOG.info("Run log: %s", log_path)

    def run_step(step_name, func):
        try:
            func()
            log_arcpy_messages(step_name)
        except Exception:
            log_arcpy_messages(f"{step_name}-failed")
            LOG.exception("Step failed: %s", step_name)
            raise

    print("\n" + "=" * 70)
    print("  DESIGNATED LANDS PIPELINE")
    print("=" * 70)
    print(f"  Exclude federal layers : {args.exclude_federal}")
    print(f"  Recent-only filter     : {args.recent_only}")
    print(f"  Raster processing      : {args.raster}")
    print(f"  Skip download          : {args.skip_download}")
    print(f"  Skip cleanup           : {args.skip_cleanup}")
    print("=" * 70 + "\n")

    # ---------------------------------
    # INIT
    # ---------------------------------

    LOG.info("=== Initialising DesignatedLands ===")

    print("[INIT] Loading configuration and source tables...")

    DL = DesignatedLands(
        config_file=args.config,
        recent_only=args.recent_only,
        start_date=args.start_date,
        end_date=args.end_date,
        exclude_federal=args.exclude_federal,
    )

    log_arcpy_messages("initialise")

    print(f"[INIT] Loaded {len(DL.sources)} designation sources")

    print(f"[INIT] GDB: {DL.gdb}\n")

    # ---------------------------------
    # STEP 1
    # ---------------------------------

    print("[Step 1/7] Testing GDB connection...")
    LOG.info("=== Step 1/7: test-connection ===")

    run_step("test-connection", DL.test_connection)

    print("[Step 1/7] Connection OK.\n")

    # ---------------------------------
    # STEP 2 - DOWNLOAD
    # ---------------------------------

    if not args.skip_download:

        print("[Step 2/7] Downloading designation layers...")
        LOG.info("=== Step 2/7: download ===")

        run_step("download", DL.download)

        # ---------------------------------
        # Run create_cha.py
        # ---------------------------------

        print("[Step 2/7] Preparing Critical Habitat Area dataset...")

        prepare_cha(
            source_data_dir=os.path.join(script_dir, "source_data")
        )

        print("[Step 2/7] CHA preparation complete.\n")

        print("[Step 2/7] Download complete.\n")

    else:

        print("[Step 2/7] Download SKIPPED (--skip-download).\n")

    # ---------------------------------
    # REPORT - Generate xlsx pipeline report
    # ---------------------------------
    # Produces an Excel workbook (outputs/designated_lands_pipeline_report.xlsx)
    # with sheets:
    #   - Changes: WFS features added/modified in the date window
    #   - Excluded Layers: layers removed by date or federal filter
    #   - Summary: counts per designation
    #   - Pipeline Options: the flags used for this run + query filters
    #   - Designation Categories: each designation mapped to a category

    print("[Report] Generating pipeline report (xlsx)...")
    from date_filter import run_report

    xlsx_path = os.path.join(
        script_dir, "outputs", "designated_lands_pipeline_report.xlsx",
    )

    # Collect query filters from sources for the report
    source_queries = []
    for src in DL.sources:
        source_queries.append({
            "designation": src.get("designation", ""),
            "name": src.get("name", ""),
            "query": src.get("query", ""),
        })

    pipeline_options = {
        "recent_only": DL.recent_only,
        "exclude_federal": DL.exclude_federal,
        "start_date": DL.start_date,
        "end_date": DL.end_date,
        "source_queries": source_queries,
    }

    run_report(
        DL.start_date, DL.end_date,
        xlsx_path=xlsx_path, avoid_overwrite=True,
        exclude_federal=DL.exclude_federal,
        federal_excluded=DL.federal_excluded_sources,
        pipeline_options=pipeline_options,
    )

    print(f"[Report] Saved to {xlsx_path}\n")

    # ---------------------------------
    # STEP 3
    # ---------------------------------

    print("[Step 3/7] Preprocessing sources...")

    LOG.info("=== Step 3/7: preprocess ===")

    run_step("preprocess", DL.preprocess)

    run_step("create-bc-boundary", DL.create_bc_boundary)

    print("[Step 3/7] Preprocessing complete.\n")

    # ---------------------------------
    # STEP 4
    # ---------------------------------

    print("[Step 4/7] Building vector outputs...")

    LOG.info("=== Step 4/7: process-vector ===")

    run_step("designations-overlapping", DL.create_designations_overlapping)

    run_step("designations-planarized", DL.create_designations_planarized)

    print("[Step 4/7] Vector processing complete.\n")

    print("[Step 4/7] Running Critical Habitat Area intersections...")

    # CHA dataset created by create_cha.py
    cha_fc = os.path.join(
        script_dir,
        "source_data",
        "critical_habitat_area.gdb",
        "critical_habitat_area"
    )

    # Pipeline outputs
    planarized_fc = os.path.join(DL.gdb, "designations_planarized")
    overlapping_fc = os.path.join(DL.gdb, "designations_overlapping")

    # Output feature classes
    planarized_intersect = os.path.join(DL.gdb, "designations_planarized_cha")
    overlapping_intersect = os.path.join(DL.gdb, "designations_overlapping_cha")

    # Use all CPU cores for faster processing
    arcpy.env.parallelProcessingFactor = "100%"

    # -------------------------------------------------
    # Intersect 1 — Planarized
    # -------------------------------------------------
    print("[Step 4/7] Intersecting designations_planarized with CHA...")

    arcpy.analysis.PairwiseIntersect(
        [planarized_fc, cha_fc],
        planarized_intersect,
        "ALL"
    )

    print("[Step 4/7] designations_planarized_cha created.")

    # -------------------------------------------------
    # Intersect 2 — Overlapping
    # -------------------------------------------------
    print("[Step 4/7] Intersecting designations_overlapping with CHA...")

    arcpy.analysis.PairwiseIntersect(
        [overlapping_fc, cha_fc],
        overlapping_intersect,
        "ALL"
    )

    print("[Step 4/7] designations_overlapping_cha created.")
    print("[Step 4/7] CHA intersections complete.\n")

    print("[Step 4/7] Calculating CHA overlap percentages...")

    planarized_cha = os.path.join(DL.gdb, "designations_planarized_cha")
    planarized_fc = os.path.join(DL.gdb, "designations_planarized")

    # Field names in your layers
    orig_area_field_planarized = "Shape_Area"   # UPDATE if different in planarized_fc
    orig_area_field_overlapping = "Shape_Area"  # UPDATE if different in overlapping_fc
    intersect_area_field = "Shape_Area"         # UPDATE if different in intersect outputs

    # Function to calculate area in hectares
    def area_ha(value_in_m2):
        return value_in_m2 / 10000.0
    
    # -------------------------------------------------
    # Make sure CHA_Percent field exists
    # -------------------------------------------------
    if "CHA_Percent" not in [f.name for f in arcpy.ListFields(planarized_intersect)]:
        arcpy.management.AddField(planarized_intersect, "CHA_Percent", "DOUBLE")

    if "CHA_Percent" not in [f.name for f in arcpy.ListFields(overlapping_intersect)]:
        arcpy.management.AddField(overlapping_intersect, "CHA_Percent", "DOUBLE")

    # ------------------------
    # Planarized: Calculate CHA % overlap
    # ------------------------
    with arcpy.da.UpdateCursor(planarized_intersect, [intersect_area_field, orig_area_field_planarized, "CHA_Percent"]) as cursor:
        for row in cursor:
            original_ha = area_ha(row[1])  # convert original area to ha
            overlap_ha = area_ha(row[0])   # convert intersect area to ha
            row[2] = (overlap_ha / original_ha * 100) if original_ha > 0 else 0
            cursor.updateRow(row)

    # ------------------------
    # Overlapping: Calculate CHA % overlap
    # ------------------------
    with arcpy.da.UpdateCursor(overlapping_intersect, [intersect_area_field, orig_area_field_overlapping, "CHA_Percent"]) as cursor:
        for row in cursor:
            original_ha = area_ha(row[1])  # convert original area to ha
            overlap_ha = area_ha(row[0])   # convert intersect area to ha
            row[2] = (overlap_ha / original_ha * 100) if original_ha > 0 else 0
            cursor.updateRow(row)

    print("[Step 4/7] CHA percentage calculation complete.")

    # ---------------------------------
    # STEP 5
    # ---------------------------------

    if args.raster:

        print("[Step 5/7] Creating raster outputs...")

        LOG.info("=== Step 5/7: process-raster ===")

        run_step("rasterize", DL.rasterize)

        run_step("overlay-rasters", DL.overlay_rasters)

        print("[Step 5/7] Raster processing complete.\n")

    else:

        print("[Step 5/7] Raster processing SKIPPED.\n")

    # ---------------------------------
    # STEP 6
    # ---------------------------------

    print("[Step 6/7] Exporting results...")

    LOG.info("=== Step 6/7: dump ===")

    run_step("dump", DL.dump)

    print("[Step 6/7] Export complete.\n")

    # ---------------------------------
    # STEP 7
    # ---------------------------------

    if not args.skip_cleanup:

        print("[Step 7/7] Cleaning up intermediate data...")

        LOG.info("=== Step 7/7: cleanup ===")

        run_step("cleanup", DL.cleanup)

        print("[Step 7/7] Cleanup complete.\n")

    else:

        print("[Step 7/7] Cleanup SKIPPED (--skip-cleanup).\n")

    print("=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)

    LOG.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()

