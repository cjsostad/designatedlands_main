"""
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

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level

LOG = logging.getLogger(__name__)


def build_parser():
    """Build the argument parser with all pipeline options."""
    parser = argparse.ArgumentParser(
        description="Run the full designatedlands pipeline."
    )
    # --- Configuration ---
    parser.add_argument(
        "--config", "-c",
        metavar="CONFIG_FILE",
        default=None,
        help="Path to .cfg configuration file",
    )
    # --- Logging ---
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    # --- Step control ---
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip the download step (use existing source data)",
    )
    parser.add_argument(
        "--raster",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run the process-raster step (requires Spatial Analyst license, default: False)",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip the cleanup step (keep intermediate feature classes)",
    )
    # --- Date filtering ---
    parser.add_argument(
        "--recent-only",
        action="store_true",
        help="Filter datasets to only include designations added/changed within the date window",
    )
    parser.add_argument(
        "--start-date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Start date for --recent-only filter (default: 2025-04-01)",
    )
    parser.add_argument(
        "--end-date",
        metavar="YYYY-MM-DD",
        default=None,
        help="End date for --recent-only filter (default: today)",
    ) 
    # --- Federal exclusion ---
    parser.add_argument(
        "--exclude-federal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exclude all federally protected areas from the output (default: True)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --- Set up logging ---
    # All log output is written to a timestamped file in logs/ as well as
    # to the console (level controlled by --verbose / --quiet).
    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    log_path = set_log_level(args.verbose, args.quiet, log_dir=logs_dir)
    LOG.info("Run log: %s", log_path)

    def run_step(step_name, func):
        """Execute a pipeline step, capturing arcpy messages and handling errors."""
        try:
            func()
            log_arcpy_messages(step_name)
        except Exception:
            log_arcpy_messages(f"{step_name}-failed")
            LOG.exception("Step failed: %s", step_name)
            raise

    # ------------------------------------------------------------------ #
    #  Print a banner summarising the pipeline configuration              #
    # ------------------------------------------------------------------ #
    print("\n" + "=" * 70)
    print("  DESIGNATED LANDS PIPELINE")
    print("=" * 70)
    print(f"  Exclude federal layers : {args.exclude_federal}")
    print(f"  Recent-only filter     : {args.recent_only}")
    print(f"  Raster processing      : {args.raster}")
    print(f"  Skip download          : {args.skip_download}")
    print(f"  Skip cleanup           : {args.skip_cleanup}")
    print("=" * 70 + "\n")

    # ------------------------------------------------------------------ #
    #  INIT — Load configuration, CSV source tables, create/open GDB      #
    # ------------------------------------------------------------------ #
    # Reads the .cfg config file (or uses defaults), loads
    # sources_designations.csv and sources_supporting.csv, applies
    # federal exclusion and optional date filtering, creates the working
    # File Geodatabase if it doesn't exist, and sets arcpy environment
    # variables (workspace, coordinate system = BC Albers 3005).
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
    if DL.federal_excluded_sources:
        print(f"[INIT] Excluded {len(DL.federal_excluded_sources)} federal source(s): "
              + ", ".join(s["name"] for s in DL.federal_excluded_sources))
    print(f"[INIT] GDB: {DL.gdb}\n")

    # ------------------------------------------------------------------ #
    #  STEP 1 — Test connection                                           #
    # ------------------------------------------------------------------ #
    # Verifies the working File Geodatabase is accessible and lists the
    # feature classes currently in it.  Catches connection issues early
    # before spending time on downloads.
    print("[Step 1/7] Testing GDB connection...")
    LOG.info("=== Step 1/7: test-connection ===")
    run_step("test-connection", DL.test_connection)
    print("[Step 1/7] Connection OK.\n")

    # ------------------------------------------------------------------ #
    #  STEP 2 — Download designation and supporting layers                #
    # ------------------------------------------------------------------ #
    # For each source in the CSV that is not marked manual_download=T:
    #   - Resolves the BCGW catalogue URL to a WFS layer name
    #   - Downloads features via WFS (with optional CQL query filter)
    #   - Converts the GeoJSON response to a feature class in the GDB
    #   - Names the FC using the stable process_order from the CSV
    #     (e.g. src_02_park_er) so re-runs with different flags still
    #     match existing FCs and skip re-downloading.
    # Manual sources are loaded from local files in source_data/.
    # Supporting layers (tiles, boundaries) are also loaded here.
    if not args.skip_download:
        print(f"[Step 2/7] Downloading {len(DL.sources)} designation layers + "
              f"{len(DL.sources_supporting)} supporting layers from BCGW...")
        print("           (existing layers in GDB will be skipped)")
        LOG.info("=== Step 2/7: download ===")
        run_step("download", DL.download)

        # Download and prepare Critical Habitat Area (CHA)
        from create_cha import prepare_cha
        print("[Step 2/7] Preparing Critical Habitat Area dataset...")
        prepare_cha(
            source_data_dir=os.path.join(script_dir, "source_data"),
        )

        print("[Step 2/7] Download complete.\n")
    else:
        print("[Step 2/7] Download SKIPPED (--skip-download).\n")
        LOG.info("=== Step 2/7: download (SKIPPED) ===")

    # ------------------------------------------------------------------ #
    #  REPORT — Generate xlsx pipeline report                             #
    # ------------------------------------------------------------------ #
    # When federal exclusion or date filtering is active, produces an
    # Excel workbook (outputs/designated_lands_pipeline_report.xlsx) with sheets:
    #   - Changes: WFS features added/modified in the date window
    #   - Excluded Layers: layers removed by date or federal filter
    #   - Summary: counts per designation
    #   - Pipeline Options: the flags used for this run
    #   - Designation Categories: each designation mapped to a category
    if DL.recent_only or DL.exclude_federal:
        print("[Report] Generating pipeline report (xlsx)...")
        from date_filter import run_report
        script_dir = os.path.dirname(os.path.abspath(__file__))
        xlsx_path = os.path.join(
            script_dir, "outputs", "designated_lands_pipeline_report.xlsx",
        )
        pipeline_options = {
            "recent_only": DL.recent_only,
            "exclude_federal": DL.exclude_federal,
            "start_date": DL.start_date,
            "end_date": DL.end_date,
        }
        run_report(
            DL.start_date, DL.end_date,
            xlsx_path=xlsx_path, avoid_overwrite=True,
            exclude_federal=DL.exclude_federal,
            federal_excluded=DL.federal_excluded_sources,
            pipeline_options=pipeline_options,
        )
        print(f"[Report] Saved to {xlsx_path}\n")

    # ------------------------------------------------------------------ #
    #  STEP 3 — Preprocess sources and create BC boundary                 #
    # ------------------------------------------------------------------ #
    # Applies per-source preprocessing as defined in the CSV:
    #   - "clip": clips the source FC by another FC (e.g. clip NGO lands
    #     by BC boundary to remove out-of-province slivers)
    #   - "union" (dissolve): dissolves overlapping features within a
    #     single source by specified columns (e.g. dissolve conservation
    #     lands by CONSERVATION_LAND_TYPE)
    # Creates preprocessed FCs with "_pp" suffix.
    #
    # Also creates bc_boundary by merging bc_boundary_land with a marine
    # boundary (bc_abms + marine_ecosections), then dissolving.  This
    # combined boundary is used later to clip the final outputs.
    print("[Step 3/7] Preprocessing sources (dissolve, clip) and creating BC boundary...")
    LOG.info("=== Step 3/7: preprocess ===")
    run_step("preprocess", DL.preprocess)
    run_step("create-bc-boundary", DL.create_bc_boundary)
    print("[Step 3/7] Preprocessing complete.\n")

    # ------------------------------------------------------------------ #
    #  STEP 4 — Process vector (overlapping + planarized)                 #
    # ------------------------------------------------------------------ #
    # Creates the two main output feature classes:
    #
    # 4a. designations_overlapping:
    #   - Iterates sources in process_order (highest priority first)
    #   - For each source, selects features using source_id_col/query,
    #     clips to bc_boundary, and appends to the output FC
    #   - Polygons from different sources can overlap — a single area
    #     may carry multiple designation attributes
    #   - Each feature retains its process_order, designation name,
    #     source_id, source_name, and restriction levels
    #
    # 4b. designations_planarized:
    #   - Takes designations_overlapping and runs arcpy Union to split
    #     all polygons at intersection boundaries (planar topology)
    #   - Groups spatially identical fragments (overlapping areas
    #     produce duplicate-geometry rows in the Union output)
    #   - For each group, assigns the designation with the LOWEST
    #     process_order (= highest priority) and keeps the MAX
    #     restriction value for each industry
    #   - Result: every point in BC falls in at most one designation
    print("[Step 4/7] Building vector outputs...")
    print("           Creating designations_overlapping (union of all layers)...")
    LOG.info("=== Step 4/7: process-vector ===")
    run_step("designations-overlapping", DL.create_designations_overlapping)
    print("           Creating designations_planarized (non-overlapping, priority-based)...")
    run_step("designations-planarized", DL.create_designations_planarized)
    print("[Step 4/7] Vector processing complete.\n")

    # ------------------------------------------------------------------ #
    #  STEP 5 — Process raster (optional)                                 #
    # ------------------------------------------------------------------ #
    # Requires ArcGIS Spatial Analyst extension (not available with Basic
    # license).  When enabled (--raster):
    #   - Rasterizes each designation source to a GeoTIFF at the
    #     configured resolution (default 100m) in BC Albers
    #   - Overlays all raster layers to produce combined designation
    #     and restriction-level rasters in outputs/
    if args.raster:
        print("[Step 5/7] Creating raster designation/restriction layers...")
        LOG.info("=== Step 5/7: process-raster ===")
        run_step("rasterize", DL.rasterize)
        run_step("overlay-rasters", DL.overlay_rasters)
        print("[Step 5/7] Raster processing complete.\n")
    else:
        print("[Step 5/7] Raster processing SKIPPED (use --raster to enable).\n")
        LOG.info("=== Step 5/7: process-raster (SKIPPED) ===")

    # ------------------------------------------------------------------ #
    #  STEP 6 — Dump (export to output File Geodatabase)                  #
    # ------------------------------------------------------------------ #
    # Copies designations_planarized and designations_overlapping from
    # the working GDB into a clean output File Geodatabase at
    # outputs/designatedlands_output.gdb.  Creates the output GDB if
    # it doesn't exist; overwrites existing FCs if they do.
    print("[Step 6/7] Exporting results to output File Geodatabase...")
    LOG.info("=== Step 6/7: dump ===")
    run_step("dump", DL.dump)
    print("[Step 6/7] Export complete.\n")

    # ------------------------------------------------------------------ #
    #  STEP 7 — Cleanup (remove intermediate FCs)                         #
    # ------------------------------------------------------------------ #
    # Deletes all src_* and *_pp feature classes from the working GDB
    # to reclaim disk space.  The output GDB in outputs/ is not touched.
    # Use --skip-cleanup to keep intermediate data for debugging.
    if not args.skip_cleanup:
        print("[Step 7/7] Cleaning up intermediate feature classes from GDB...")
        LOG.info("=== Step 7/7: cleanup ===")
        run_step("cleanup", DL.cleanup)
        print("[Step 7/7] Cleanup complete.\n")
    else:
        print("[Step 7/7] Cleanup SKIPPED (--skip-cleanup).\n")
        LOG.info("=== Step 7/7: cleanup (SKIPPED) ===")

    print("=" * 70)
    print("  PIPELINE COMPLETE")
    print("=" * 70)
    LOG.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
