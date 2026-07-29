"""
Utilities for validating and creating ArcGIS File Geodatabases.

This module provides small helpers used by pipeline entrypoints to ensure
output geodatabase paths are valid File GDB workspaces before writes occur.
"""

import datetime
import logging
import os
import shutil

import arcpy


def _workspace_factory(path: str) -> str:
    try:
        desc = arcpy.Describe(path)
        return getattr(desc, "workspaceFactoryProgID", "") or ""
    except Exception:
        return ""


def is_file_gdb(path: str) -> bool:
    if not arcpy.Exists(path):
        return False
    factory = _workspace_factory(path).lower()
    return "filegdbworkspacefactory" in factory


def _clear_readonly(path: str, logger: logging.Logger = None) -> None:
    """Recursively clear the read-only attribute on a File GDB folder.

    Network-share GDBs (UNC paths) can inherit a read-only attribute that
    causes arcpy writes (e.g. PairwiseIntersect) to fail with ERROR 160385
    even when the GDB itself is structurally valid.
    """
    try:
        for dirpath, dirnames, filenames in os.walk(path):
            current_mode = os.stat(dirpath).st_mode
            os.chmod(dirpath, current_mode | 0o222)
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                fmode = os.stat(fpath).st_mode
                os.chmod(fpath, fmode | 0o222)
    except Exception as chmod_err:
        if logger:
            logger.warning("Could not clear read-only flag on %s: %s", path, chmod_err)


def ensure_file_gdb(path: str, recreate_invalid: bool = False, logger: logging.Logger = None) -> str:
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    os.makedirs(parent, exist_ok=True)

    if arcpy.Exists(path):
        if is_file_gdb(path):
            # Clear any read-only flag that may have been inherited from the
            # parent folder on network shares — this causes ERROR 160385 on
            # subsequent arcpy writes even for a structurally valid GDB.
            _clear_readonly(path, logger)
            if logger:
                logger.info("Using output File Geodatabase: %s", path)
            return path

        factory = _workspace_factory(path) or "unknown"
        msg = (
            f"Path exists but is not a File Geodatabase: {path} "
            f"(workspace factory: {factory})"
        )

        if not recreate_invalid:
            raise RuntimeError(msg)

        # Try arcpy.Delete first — it releases GDB locks that shutil cannot handle
        # (e.g. ArcGIS .sr.lock files on UNC paths).
        try:
            arcpy.management.Delete(path)
            if logger:
                logger.warning("%s. Deleted invalid path via arcpy: %s", msg, path)
        except Exception as del_err:
            # Fall back to shutil.move as a backup copy
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            backup = f"{path}_invalid_backup_{stamp}"
            try:
                shutil.move(path, backup)
                if logger:
                    logger.warning("%s. arcpy.Delete failed (%s); moved invalid path to: %s", msg, del_err, backup)
            except Exception as move_err:
                raise RuntimeError(
                    f"{msg}. Could not remove invalid path — arcpy.Delete failed ({del_err}) "
                    f"and shutil.move failed ({move_err}). Close any ArcGIS sessions that have "
                    f"'{path}' open, then re-run."
                ) from move_err

    arcpy.management.CreateFileGDB(parent, name)
    if not is_file_gdb(path):
        factory = _workspace_factory(path) or "unknown"
        raise RuntimeError(
            f"Failed to create a valid File Geodatabase at {path} "
            f"(workspace factory: {factory})"
        )

    # Clear any read-only flag inherited from the parent folder on network shares.
    _clear_readonly(path, logger)

    if logger:
        logger.info("Created output File Geodatabase: %s", path)
    return path


def write_run_manifest(output_gdb: str, manifest: dict, logger: logging.Logger = None) -> None:
    """Create/overwrite a single-row 'run_manifest' table in output_gdb."""
    text_fields = [
        "run_timestamp", "date_filter", "start_date", "end_date",
        "exclude_federal", "skip_download", "skip_cleanup", "raster",
        "cha_filter_out_wrs", "output_gdb", "log_file",
    ]
    long_fields = [
        "cha_report_row_limit", "planarized_count", "overlapping_count",
        "planarized_cha_count", "overlapping_cha_count",
    ]

    def _coerce_text(value):
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if value is None:
            return ""
        return str(value)

    try:
        table_path = os.path.join(output_gdb, "run_manifest")
        if arcpy.Exists(table_path):
            arcpy.management.Delete(table_path)

        arcpy.management.CreateTable(output_gdb, "run_manifest")

        for fname in text_fields:
            arcpy.management.AddField(table_path, fname, "TEXT", field_length=255)
        for fname in long_fields:
            arcpy.management.AddField(table_path, fname, "LONG")

        field_order = text_fields + long_fields
        row = tuple(
            _coerce_text(manifest.get(f)) if f in text_fields else manifest.get(f)
            for f in field_order
        )
        with arcpy.da.InsertCursor(table_path, field_order) as cursor:
            cursor.insertRow(row)

        if logger:
            logger.info("Wrote run_manifest table to %s", output_gdb)
    except Exception as manifest_err:
        if logger:
            logger.warning("Could not write run_manifest table: %s", manifest_err)