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


def ensure_file_gdb(path: str, recreate_invalid: bool = False, logger: logging.Logger = None) -> str:
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    name = os.path.basename(path)
    os.makedirs(parent, exist_ok=True)

    if arcpy.Exists(path):
        if is_file_gdb(path):
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

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = f"{path}_invalid_backup_{stamp}"
        shutil.move(path, backup)
        if logger:
            logger.warning("%s. Moved invalid path to: %s", msg, backup)

    arcpy.management.CreateFileGDB(parent, name)
    if not is_file_gdb(path):
        factory = _workspace_factory(path) or "unknown"
        raise RuntimeError(
            f"Failed to create a valid File Geodatabase at {path} "
            f"(workspace factory: {factory})"
        )

    if logger:
        logger.info("Created output File Geodatabase: %s", path)
    return path