"""
Tested and working 05_29_26

Run the full designatedlands pipeline in sequence.

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
  - Date filtering is ON by default — only features added/modified
    between START_DATE and END_DATE are downloaded and processed.
  - Federal layers (National Parks, National Wildlife Areas, Migratory
    Bird Sanctuaries) are excluded by default.
  - Raster processing is off by default (no Spatial Analyst license).
  - All other steps run automatically.

To change options, edit the PIPELINE OPTIONS block at the top of main().

Usage:
    python main.py
    python main.py --config path/to/config.cfg
    python main.py --verbose
"""
import argparse
import logging
import os
import sys
import datetime
import arcpy

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level
from create_cha import prepare_cha   # <-- import CHA script (retry + fallback, no AOI)
from intersect_area_calc import run_cha_intersection
from date_filter import run_report
from gdb_utils import ensure_file_gdb



LOG = logging.getLogger(__name__)


def build_parser():
    """Parse CLI args for --config and --verbose/--quiet only.

    All pipeline options are set in the PIPELINE OPTIONS block at the
    top of main() so they can be edited directly in VS Code.
    """
    parser = argparse.ArgumentParser(
        description="Run the full designatedlands pipeline."
    )
    parser.add_argument("--config", "-c", metavar="CONFIG_FILE", default=None)
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--quiet", "-q", action="store_true")
    return parser


def main():

    parser = build_parser()
    args = parser.parse_args()

    # =================================================================
    # PIPELINE OPTIONS  —  Edit these directly, then hit Run in VS Code
    # =================================================================

    # If changing the date filter settings, run pipeline_reset.py before running main.py to clear out stale layers from the GDB so the next run downloads fresh data matching your new settings.
    RECENT_ONLY    = False           # True = only process features new/modified in date window
    START_DATE     = "2025-04-01"   # Start of date window (YYYY-MM-DD)
    END_DATE       = "2026-04-01"           # End of date window (None = today)
    EXCLUDE_FEDERAL = True          # Exclude National Parks, NWAs, Migratory Bird Sanctuaries
    SKIP_DOWNLOAD  = False          # True = skip WFS download (use existing data in GDB)
    SKIP_CLEANUP   = True          # True = keep intermediate feature classes
    RASTER         = False          # True = create raster outputs (requires Spatial Analyst)
    CHA_FILTER_OUT_WRS = False       # True = full CHA filter (FINAL + BC + exclude species)
                                    # False = minimal CHA filter (FINAL + BC only)
    # =================================================================

    # Naming suffix: when a date filter is active, output FC names are
    # appended with "_date_filter" so filtered results are immediately
    # distinguishable from full-run outputs in the GDB.
    dl_suffix = "_date_filter" if RECENT_ONLY else ""

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
    print(f"  Date filter (recent only) : {RECENT_ONLY}")
    print(f"  Start date                : {START_DATE}")
    print(f"  End date                  : {END_DATE or 'today'}")
    print(f"  Exclude federal layers    : {EXCLUDE_FEDERAL}")
    print(f"  Skip download             : {SKIP_DOWNLOAD}")
    print(f"  Skip cleanup              : {SKIP_CLEANUP}")
    print(f"  Raster processing         : {RASTER}")
    print(f"  CHA filter out WRS        : {CHA_FILTER_OUT_WRS}")
    print("=" * 70 + "\n")

    # ---------------------------------
    # INIT
    # ---------------------------------

    LOG.info("=== Initialising DesignatedLands ===")

    print("[INIT] Loading configuration and source tables...")

    DL = DesignatedLands(
        config_file=args.config,
        recent_only=RECENT_ONLY,
        start_date=START_DATE,
        end_date=END_DATE,
        exclude_federal=EXCLUDE_FEDERAL,
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

    if not SKIP_DOWNLOAD:

        print("[Step 2/7] Downloading designation layers...")
        LOG.info("=== Step 2/7: download ===")

        run_step("download", lambda: DL.download(overwrite=RECENT_ONLY))

        # ---------------------------------
        # Run create_cha.py
        # ---------------------------------

        print("[Step 2/7] Preparing Critical Habitat Area dataset...")

        cha_query_override = None
        if not CHA_FILTER_OUT_WRS:
            cha_query_override = (
                "RD_Status IN (1) And ProvTerr_E LIKE '%British Columbia%'"
            )

        prepare_cha(
            source_data_dir=os.path.join(script_dir, "source_data"),
            query_override=cha_query_override,
        )

        print("[Step 2/7] CHA preparation complete.\n")

        print("[Step 2/7] Download complete.\n")

    else:

        print("[Step 2/7] Download SKIPPED (SKIP_DOWNLOAD=True).\n")

    # ---------------------------------
    # REPORT - Generate xlsx pipeline report
    # ---------------------------------
    # Produces an Excel workbook in outputs/ with a date-stamped filename
    # with sheets:
    #   - Changes: WFS features added/modified in the date window
    #   - Excluded Layers: layers removed by date or federal filter
    #   - Summary: counts per designation
    #   - Pipeline Options: the flags used for this run + query filters
    #   - Designation Categories: each designation mapped to a category

    print("[Report] Generating pipeline report (xlsx)...")
   
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
    # Include supporting sources (e.g. Critical Habitat Area filter)
    for src in DL.sources_supporting:
        if src.get("query", "").strip():
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

    written_report = run_report(
        DL.start_date, DL.end_date,
        xlsx_path=xlsx_path, avoid_overwrite=True,
        exclude_federal=DL.exclude_federal,
        federal_excluded=DL.federal_excluded_sources,
        pipeline_options=pipeline_options,
    )

    print(f"[Report] Saved to {written_report}\n")

    # ---------------------------------
    # PRE-CHECK: verify all sources exist
    # ---------------------------------

    print("[Pre-check] Verifying source feature classes in GDB...")
    LOG.info("=== Pre-check: verify sources ===")
    verify_result = DL.verify_sources()
    log_arcpy_messages("verify-sources")
    n_present = verify_result["present"]
    n_missing = verify_result["missing_designations"]
    n_total = verify_result["total_designations"]
    if n_missing:
        print(f"[Pre-check] {n_present} sources present, "
              f"{n_missing}/{n_total} designation sources skipped "
              f"(no features in date window).\n")
    else:
        print(f"[Pre-check] All {n_present} sources present.\n")

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

    run_step("designations-overlapping", lambda: DL.create_designations_overlapping(suffix=dl_suffix))

    run_step("designations-planarized", lambda: DL.create_designations_planarized(suffix=dl_suffix))

    print("[Step 4/7] Vector processing complete.\n")

    print("[Step 4/7] Running Critical Habitat Area intersections...")

    # CHA dataset created by create_cha.py
    cha_fc = os.path.join(
        script_dir,
        "source_data",
        "cha_exported.gdb",
        "critical_habitat_area"
    )

    # Pipeline outputs (intermediate)
    planarized_fc = os.path.join(DL.gdb, f"designations_planarized{dl_suffix}")
    overlapping_fc = os.path.join(DL.gdb, f"designations_overlapping{dl_suffix}")

    # Final output gdb
    output_gdb = os.path.join(script_dir, "outputs", "designatedlands_output.gdb")
    ensure_file_gdb(output_gdb, recreate_invalid=True, logger=LOG)

    # Date-stamped output names
    now = datetime.datetime.now()
    date_suffix = now.strftime("%m_%d")

    run_cha_intersection(
        cha_fc=cha_fc,
        planarized_fc=planarized_fc,
        overlapping_fc=overlapping_fc,
        output_gdb=output_gdb,
        planarized_out_name=f"designations_planarized{dl_suffix}_cha_{date_suffix}",
        overlapping_out_name=f"designations_overlapping{dl_suffix}_cha_{date_suffix}",
    )

    print("[Step 4/7] CHA intersection and area calculation complete.")
    # ---------------------------------
    # STEP 5
    # ---------------------------------

    if RASTER:

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

    run_step("dump", lambda: DL.dump(suffix=dl_suffix))

    print("[Step 6/7] Export complete.\n")

    # ---------------------------------
    # STEP 7
    # ---------------------------------

    if not SKIP_CLEANUP:

        print("[Step 7/7] Cleaning up intermediate data...")

        LOG.info("=== Step 7/7: cleanup ===")

        run_step("cleanup", DL.cleanup)

        print("[Step 7/7] Cleanup complete.\n")

    else:

        print("[Step 7/7] Cleanup SKIPPED (SKIP_CLEANUP=True).\n")

    print("=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)

    LOG.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()

