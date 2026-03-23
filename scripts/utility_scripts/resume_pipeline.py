
"""
Smart-resume the designatedlands pipeline.

Automatically detects which steps have already completed by checking for
output feature classes and files, then picks up from where it left off.

Usage:
    python resume_pipeline.py
    python resume_pipeline.py --config path/to/config.cfg
    python resume_pipeline.py --verbose
    python resume_pipeline.py --force-from 4   (force restart from step 4)
"""

import argparse
import logging
import os
import sys
from pathlib import Path

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level

LOG = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Smart-resume the designatedlands pipeline."
    )
    parser.add_argument(
        "--config", "-c",
        metavar="CONFIG_FILE",
        default=None,
        help="Path to .cfg configuration file",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
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
    parser.add_argument(
        "--exclude-federal",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Exclude all federally protected areas from the output (default: True)",
    )
    parser.add_argument(
        "--recent-only",
        action="store_true",
        default=False,
        help="Name outputs with _date_filter suffix (date-filtered run)",
    )
    parser.add_argument(
        "--force-from",
        type=int,
        choices=range(1, 8),
        metavar="STEP",
        default=None,
        help="Force restart from this step number (1-7), ignoring auto-detection",
    )
    return parser


def detect_completed_steps(DL, arcpy, args):
    """Check GDB and output files to determine which steps are done.

    Returns the next step number to run (1-8, where 8 means all done).
    """
    gdb = DL.gdb
    dl_suffix = "_date_filter" if args.recent_only else ""

    # Step 2: Download — all source FCs loaded?
    all_src_loaded = all(
        arcpy.Exists(os.path.join(gdb, s["src"]))
        for s in DL.sources
    )
    supporting_loaded = all(
        arcpy.Exists(os.path.join(gdb, s["src"]))
        for s in DL.sources_supporting
    )
    if not (all_src_loaded and supporting_loaded):
        LOG.info("Auto-detect: some source FCs missing — resuming from step 2 (download)")
        return 2

    # Step 3: Preprocess — bc_boundary exists?
    bc_boundary = os.path.join(gdb, "bc_boundary")
    if not arcpy.Exists(bc_boundary):
        LOG.info("Auto-detect: bc_boundary missing — resuming from step 3 (preprocess)")
        return 3

    # Step 4a: Overlapping
    overlapping_fc = os.path.join(gdb, f"designations_overlapping{dl_suffix}")
    if not arcpy.Exists(overlapping_fc):
        LOG.info("Auto-detect: designations_overlapping%s missing — resuming from step 4a (overlapping)", dl_suffix)
        return 4

    # Step 4b: Planarized
    planarized_fc = os.path.join(gdb, f"designations_planarized{dl_suffix}")
    if not arcpy.Exists(planarized_fc):
        LOG.info("Auto-detect: designations_planarized%s missing — resuming from step 4 (planarized)", dl_suffix)
        return 5  # internal: 5 = planarized only (4a done)

    # Step 5: Raster (only if requested)
    # Skip detection — raster is off by default

    # Step 6: Dump — output GDB with both FCs?
    out_dir = Path(DL.config["out_path"]).resolve()
    out_gdb = str(out_dir / "designatedlands_output.gdb")
    dump_done = True
    for fc_name in (f"designations_planarized{dl_suffix}", f"designations_overlapping{dl_suffix}"):
        if not arcpy.Exists(os.path.join(out_gdb, fc_name)):
            dump_done = False
            break
    if not dump_done:
        LOG.info("Auto-detect: output files missing — resuming from step 6 (dump)")
        return 6

    # Step 7: Cleanup — src_ FCs removed?
    any_src_remaining = any(
        arcpy.Exists(os.path.join(gdb, s["src"]))
        for s in DL.sources
    )
    if any_src_remaining and not args.skip_cleanup:
        LOG.info("Auto-detect: source FCs still in GDB — resuming from step 7 (cleanup)")
        return 7

    return 8  # all done


def main():
    parser = build_parser()
    args = parser.parse_args()

    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    log_path = set_log_level(args.verbose, args.quiet, log_dir=logs_dir)
    LOG.info("Run log: %s", log_path)

    import arcpy

    def run_step(step_name, func):
        try:
            func()
            log_arcpy_messages(step_name)
        except Exception:
            log_arcpy_messages(f"{step_name}-failed")
            LOG.exception("Step failed: %s", step_name)
            raise

    LOG.info("=== Initialising DesignatedLands (smart resume) ===")
    DL = DesignatedLands(
        config_file=args.config,
        exclude_federal=args.exclude_federal,
        recent_only=args.recent_only,
    )
    log_arcpy_messages("initialise")

    dl_suffix = "_date_filter" if args.recent_only else ""

    # Compact GDB to repair any corruption from interrupted runs
    LOG.info("Compacting GDB...")
    arcpy.management.Compact(DL.gdb)
    log_arcpy_messages("compact-gdb")

    # Determine resume point
    if args.force_from:
        resume_from = args.force_from
        LOG.info("Forced restart from step %d", resume_from)
    else:
        resume_from = detect_completed_steps(DL, arcpy, args)

    if resume_from == 8:
        LOG.info("=== All steps already complete — nothing to do ===")
        return

    LOG.info("=== Resuming from step %d ===", resume_from)

    # Step 1: Test connection
    if resume_from <= 1:
        LOG.info("=== Step 1/7: test-connection ===")
        run_step("test-connection", DL.test_connection)
    else:
        LOG.info("=== Step 1/7: test-connection (DONE) ===")

    # Step 2: Download
    if resume_from <= 2:
        LOG.info("=== Step 2/7: download ===")
        run_step("download", DL.download)
    else:
        LOG.info("=== Step 2/7: download (DONE) ===")

    # Step 3: Preprocess
    if resume_from <= 3:
        LOG.info("=== Step 3/7: preprocess ===")
        run_step("preprocess", DL.preprocess)
        run_step("create-bc-boundary", DL.create_bc_boundary)
    else:
        LOG.info("=== Step 3/7: preprocess (DONE) ===")

    # Step 4: Process vector — overlapping
    if resume_from <= 4:
        LOG.info("=== Step 4/7: process-vector (overlapping) ===")
        run_step("designations-overlapping", lambda: DL.create_designations_overlapping(suffix=dl_suffix))
    else:
        LOG.info("=== Step 4/7: overlapping (DONE) ===")

    # Step 4b/5: Process vector — planarized
    if resume_from <= 5:
        LOG.info("=== Step 4/7: process-vector (planarized) ===")
        run_step("designations-planarized", lambda: DL.create_designations_planarized(suffix=dl_suffix))
    else:
        LOG.info("=== Step 4/7: planarized (DONE) ===")

    # Step 5: Process raster
    if args.raster:
        LOG.info("=== Step 5/7: process-raster ===")
        run_step("rasterize", DL.rasterize)
        run_step("overlay-rasters", DL.overlay_rasters)
    else:
        LOG.info("=== Step 5/7: process-raster (SKIPPED) ===")

    # Step 6: Dump
    if resume_from <= 6:
        LOG.info("=== Step 6/7: dump ===")
        run_step("dump", lambda: DL.dump(suffix=dl_suffix))
    else:
        LOG.info("=== Step 6/7: dump (DONE) ===")

    # Step 7: Cleanup
    if resume_from <= 7 and not args.skip_cleanup:
        LOG.info("=== Step 7/7: cleanup ===")
        run_step("cleanup", DL.cleanup)
    else:
        LOG.info("=== Step 7/7: cleanup (SKIPPED) ===")

    LOG.info("=== Pipeline complete (resumed from step %d) ===", resume_from)


if __name__ == "__main__":
    main()
