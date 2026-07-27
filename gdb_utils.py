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