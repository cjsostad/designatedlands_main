"""
Run the full designatedlands pipeline in sequence:
  1. test-connection
  2. download
  3. preprocess
  4. process-vector
  5. process-raster
  6. dump
  7. cleanup

Usage:
    python main.py
    python main.py --config path/to/config.cfg
    python main.py --skip-cleanup
    python main.py --verbose
"""

import argparse
import logging
import os
import sys

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level

LOG = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run the full designatedlands pipeline."
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
        "--skip-download",
        action="store_true",
        help="Skip the download step (use existing source data)",
    )
    parser.add_argument(
        "--skip-raster",
        action="store_true",
        help="Skip the process-raster step",
    )
    parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Skip the cleanup step (keep intermediate feature classes)",
    )
    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
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

    LOG.info("=== Initialising DesignatedLands ===")
    DL = DesignatedLands(config_file=args.config)
    log_arcpy_messages("initialise")

    # 1. Test connection
    LOG.info("=== Step 1/7: test-connection ===")
    run_step("test-connection", DL.test_connection)

    # 2. Download
    if not args.skip_download:
        LOG.info("=== Step 2/7: download ===")
        run_step("download", DL.download)
    else:
        LOG.info("=== Step 2/7: download (SKIPPED) ===")

    # 3. Preprocess
    LOG.info("=== Step 3/7: preprocess ===")
    run_step("preprocess", DL.preprocess)
    run_step("create-bc-boundary", DL.create_bc_boundary)

    # 4. Process vector
    LOG.info("=== Step 4/7: process-vector ===")
    run_step("designations-overlapping", DL.create_designations_overlapping)
    run_step("designations-planarized", DL.create_designations_planarized)

    # 5. Process raster
    if not args.skip_raster:
        LOG.info("=== Step 5/7: process-raster ===")
        run_step("rasterize", DL.rasterize)
        run_step("overlay-rasters", DL.overlay_rasters)
    else:
        LOG.info("=== Step 5/7: process-raster (SKIPPED) ===")

    # 6. Dump
    LOG.info("=== Step 6/7: dump ===")
    run_step("dump", DL.dump)

    # 7. Cleanup
    if not args.skip_cleanup:
        LOG.info("=== Step 7/7: cleanup ===")
        run_step("cleanup", DL.cleanup)
    else:
        LOG.info("=== Step 7/7: cleanup (SKIPPED) ===")

    LOG.info("=== Pipeline complete ===")


if __name__ == "__main__":
    main()
