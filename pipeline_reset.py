"""
pipeline_reset.py — Pre-run GDB cleanup utility

PURPOSE
-------
Use this script before running main.py when you want to change the date
filter settings (e.g. switching from RECENT_ONLY=True to RECENT_ONLY=False,
or changing START_DATE/END_DATE). It deletes all stale designation feature
classes from the working GDB so that the next pipeline run downloads
fresh data matching your new settings.

WHY THIS IS NECESSARY
---------------------
When RECENT_ONLY=False, the pipeline passes overwrite=False to the download
step. This means any feature class already in the GDB is silently skipped —
even if it was downloaded during a previous date-filtered run. Running this
script clears those stale layers, guaranteeing the next run fetches clean data.

WHAT GETS DELETED
-----------------
  - src_*          : designation source layers (date-filtered or full)
  - *_pp           : preprocessed intermediate layers
  - bc_boundary*   : derived BC boundary layers (rebuilt each run)
  - designations_* : output feature classes

WHAT IS PRESERVED
-----------------
These supporting layers are not date-filtered and take time to download,
so they are kept intact:
  - tiles_20k
  - tiles_250k
  - marine_ecosections
  - bc_abms
  - mk_boundary
  - bc_boundary_land  (manually downloaded from GeoBC FTP)

HOW TO USE
----------
1. Run this script (click the Run button in VS Code).
2. Confirm the deletion when prompted.
3. Edit your PIPELINE OPTIONS in main.py (e.g. set RECENT_ONLY=False).
4. Run main.py as normal.
"""

import os
import arcpy

# ---------------------------------------------------------------------------
# Locate GDB relative to this script — works regardless of where the
# project folder has been moved or shared.
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
GDB = os.path.join(SCRIPT_DIR, "designatedlands.gdb")

# Feature classes that should never be deleted — these are supporting layers
# that are valid regardless of date filter settings.
KEEP = {
    "tiles_20k",
    "tiles_250k",
    "marine_ecosections",
    "bc_abms",
    "mk_boundary",
    "bc_boundary_land",
}


def main():
    if not arcpy.Exists(GDB):
        print(f"ERROR: GDB not found at: {GDB}")
        return

    arcpy.env.workspace = GDB
    all_fcs = arcpy.ListFeatureClasses() or []

    to_delete = [fc for fc in all_fcs if fc not in KEEP]

    if not to_delete:
        print("Nothing to delete — GDB contains only supporting layers.")
        return

    print(f"\nGDB: {GDB}")
    print(f"\nThe following {len(to_delete)} feature class(es) will be deleted:\n")
    for fc in sorted(to_delete):
        print(f"  {fc}")

    print(f"\n{len(KEEP)} supporting layer(s) will be preserved: {sorted(KEEP)}\n")

    confirm = input("Type YES to confirm deletion, anything else to cancel: ").strip()
    if confirm != "YES":
        print("Cancelled — nothing was deleted.")
        return

    deleted = 0
    errors = 0
    for fc in to_delete:
        fc_path = os.path.join(GDB, fc)
        try:
            arcpy.management.Delete(fc_path)
            print(f"  Deleted: {fc}")
            deleted += 1
        except Exception as e:
            print(f"  ERROR deleting {fc}: {e}")
            errors += 1

    print(f"\nDone. {deleted} deleted, {errors} error(s).")
    if errors == 0:
        print("GDB is ready for a fresh pipeline run.")


if __name__ == "__main__":
    main()
