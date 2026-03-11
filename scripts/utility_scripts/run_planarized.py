"""
Re-run only the designations_planarized step.

Use this script when designations_overlapping already exists in the
working GDB and you only need to regenerate the planarized output
(e.g., after a code fix to create_designations_planarized()).

After building the planarized FC it also re-exports both outputs
to the clean output GDB (dump step).

Usage:
    python run_planarized.py
    python run_planarized.py --config path/to/config.cfg
    python run_planarized.py --no-exclude-federal
    python run_planarized.py --skip-dump
    python run_planarized.py --verbose
"""

import argparse
import logging
import os
import sys

from designatedlands import DesignatedLands, log_arcpy_messages, set_log_level

LOG = logging.getLogger(__name__)


def build_parser():
    parser = argparse.ArgumentParser(
        description="Rebuild designations_planarized from existing overlapping FC.",
    )
    parser.add_argument(
        "--config", "-c", metavar="CONFIG_FILE", default=None,
        help="Path to .cfg configuration file",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser.add_argument(
        "--exclude-federal",
        action=argparse.BooleanOptionalAction, default=True,
        help="Exclude federal designations (default: True)",
    )
    parser.add_argument(
        "--skip-dump", action="store_true",
        help="Skip the dump step (don't re-export to output GDB)",
    )
    return parser


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

    # ---- Initialise ----
    print("\n" + "=" * 60)
    print("  REBUILD DESIGNATIONS_PLANARIZED")
    print("=" * 60)

    print("[INIT] Loading configuration...")
    DL = DesignatedLands(
        config_file=args.config,
        exclude_federal=args.exclude_federal,
    )
    log_arcpy_messages("initialise")
    print(f"[INIT] {len(DL.sources)} designation sources loaded")
    print(f"[INIT] GDB: {DL.gdb}\n")

    # ---- Verify prerequisite ----
    overlapping_fc = os.path.join(DL.gdb, "designations_overlapping")
    if not arcpy.Exists(overlapping_fc):
        print("[ERROR] designations_overlapping not found in the working GDB.")
        print("        Run the full pipeline (main.py) first.")
        sys.exit(1)

    count = int(arcpy.management.GetCount(overlapping_fc)[0])
    print(f"[CHECK] designations_overlapping exists ({count:,} features)\n")

    # ---- Rebuild planarized ----
    print("[PLANARIZE] Creating designations_planarized...")
    run_step("designations-planarized", DL.create_designations_planarized)
    planarized_fc = os.path.join(DL.gdb, "designations_planarized")
    p_count = int(arcpy.management.GetCount(planarized_fc)[0])
    print(f"[PLANARIZE] Done — {p_count:,} planarized features\n")

    # ---- Dump to output GDB ----
    if not args.skip_dump:
        print("[DUMP] Exporting to output GDB...")
        run_step("dump", DL.dump)
        print("[DUMP] Export complete.\n")
    else:
        print("[DUMP] Skipped (--skip-dump).\n")

    print("=" * 60)
    print("  COMPLETE")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
