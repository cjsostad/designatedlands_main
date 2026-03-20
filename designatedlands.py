# Copyright 2017 Province of British Columbia
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# ============================================================================
# Rewritten for ArcGIS Pro compatibility
# - Replaces PostgreSQL/PostGIS backend with ArcGIS Pro File Geodatabase
# - Replaces pgdata, geoalchemy2, sqlalchemy, bcdata, rasterio, fiona, cligj,
#   gdal/ogr2ogr subprocess calls with arcpy and standard library equivalents
# - Requires: ArcGIS Pro 2.x or 3.x (arcgispro-py3 conda environment)
# - Optional pip extras: requests (already bundled)
# ============================================================================

import argparse
import configparser
import csv
import hashlib
import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from functools import wraps
from datetime import date, datetime
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import requests

try:
    import arcpy
    from arcpy.sa import Raster
    arcpy.CheckOutExtension("Spatial")
    ARCPY_AVAILABLE = True
except ImportError:
    ARCPY_AVAILABLE = False
    print(
        "WARNING: arcpy not found. This script requires ArcGIS Pro. "
        "Run from the ArcGIS Pro Python Command Prompt."
    )


LOG = logging.getLogger(__name__)
_STDOUT_REDIRECTED = False
_ORIGINAL_STDOUT = sys.stdout
_ORIGINAL_STDERR = sys.stderr
_ARCPY_TOOL_LOGGING_ENABLED = False


class _LoggerStream:
    """File-like stream that forwards writes to logging."""

    def __init__(self, logger: logging.Logger, level: int):
        self.logger = logger
        self.level = level
        self._buffer = ""

    def write(self, msg):
        if not msg:
            return 0
        self._buffer += str(msg)
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip()
            if line:
                self.logger.log(self.level, line)
        return len(msg)

    def flush(self):
        if self._buffer:
            line = self._buffer.rstrip()
            if line:
                self.logger.log(self.level, line)
            self._buffer = ""

    def isatty(self):
        return False


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "dl_path": "source_data",
    "sources_designations": "sources_designations.csv",
    "sources_supporting": "sources_supporting.csv",
    "out_path": "outputs",
    "gdb_path": "designatedlands.gdb",
    "n_processes": -1,
    "resolution": 25,
}

# BC Albers extent (EPSG:3005) for raster processing
BC_BOUNDS = {
    "xmin": 273287.5,
    "ymin": 367687.5,
    "xmax": 1870687.5,
    "ymax": 1735887.5,
}

# BCGW WFS base URL (public, no auth required)
BCGW_WFS_URL = "https://openmaps.gov.bc.ca/geo/pub/wfs"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ConfigError(Exception):
    """Configuration key error"""


class ConfigValueError(Exception):
    """Configuration value error"""


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def set_log_level(verbose: bool, quiet: bool, log_dir: str = None) -> str:
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO

    if not log_dir:
        log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(
        log_dir, f"designatedlands_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    )

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s %(name)-12s %(levelname)-8s %(message)s"
    )

    # Keep console verbosity controlled by CLI flags.
    console_handler = logging.StreamHandler(_ORIGINAL_STDERR)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    # Always keep file logging detailed for diagnostics.
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)

    logging.captureWarnings(True)

    global _STDOUT_REDIRECTED
    if not _STDOUT_REDIRECTED:
        stdout_logger = logging.getLogger("stdout")
        stderr_logger = logging.getLogger("stderr")
        sys.stdout = _LoggerStream(stdout_logger, logging.INFO)
        sys.stderr = _LoggerStream(stderr_logger, logging.ERROR)
        _STDOUT_REDIRECTED = True

    def _log_unhandled_exception(exc_type, exc_value, exc_traceback):
        if issubclass(exc_type, KeyboardInterrupt):
            return sys.__excepthook__(exc_type, exc_value, exc_traceback)
        logging.getLogger(__name__).exception(
            "Unhandled exception", exc_info=(exc_type, exc_value, exc_traceback)
        )

    sys.excepthook = _log_unhandled_exception
    _enable_arcpy_tool_logging()

    LOG.info("Detailed logging enabled. Log file: %s", log_path)
    return log_path


def log_arcpy_messages(context: str = ""):
    """Write ArcPy geoprocessing messages to the logger."""
    if not ARCPY_AVAILABLE:
        return
    try:
        messages = arcpy.GetMessages()
    except Exception as exc:
        LOG.debug("Unable to retrieve ArcPy messages: %s", exc)
        return

    if not messages:
        return

    prefix = f"[arcpy:{context}] " if context else "[arcpy] "
    for line in messages.splitlines():
        line = line.strip()
        if line:
            LOG.info("%s%s", prefix, line)


def _enable_arcpy_tool_logging():
    """Wrap common ArcPy toolboxes so each geoprocessing call logs messages."""
    global _ARCPY_TOOL_LOGGING_ENABLED
    if _ARCPY_TOOL_LOGGING_ENABLED or not ARCPY_AVAILABLE:
        return

    def _wrap_toolbox(toolbox, toolbox_name: str):
        for attr in dir(toolbox):
            if attr.startswith("_"):
                continue

            try:
                tool = getattr(toolbox, attr)
            except Exception:
                continue

            if not callable(tool) or getattr(tool, "_dl_wrapped", False):
                continue

            @wraps(tool)
            def _wrapped(*args, __tool=tool, __name=attr, **kwargs):
                context = f"{toolbox_name}.{__name}"
                try:
                    result = __tool(*args, **kwargs)
                except Exception:
                    log_arcpy_messages(f"{context}-failed")
                    raise
                log_arcpy_messages(context)
                return result

            try:
                _wrapped._dl_wrapped = True
                setattr(toolbox, attr, _wrapped)
            except Exception:
                # Some ArcPy attributes are read-only and cannot be wrapped.
                continue

    for toolbox_name in ("management", "analysis", "conversion"):
        toolbox = getattr(arcpy, toolbox_name, None)
        if toolbox is not None:
            _wrap_toolbox(toolbox, toolbox_name)

    _ARCPY_TOOL_LOGGING_ENABLED = True


# ---------------------------------------------------------------------------
# Archive helpers
# ---------------------------------------------------------------------------

class ZipCompatibleTarFile(tarfile.TarFile):
    """Wrapper around TarFile to make it more compatible with ZipFile"""

    def infolist(self):
        members = self.getmembers()
        for m in members:
            m.filename = m.name
        return members

    def namelist(self):
        return self.getnames()


def get_compressed_file_wrapper(path):
    """Return an appropriate archive object for the given path."""
    if path.endswith(".zip"):
        return zipfile.ZipFile(path, "r")
    elif path.endswith(".tar.gz") or path.endswith(".tgz"):
        return ZipCompatibleTarFile.open(path, "r:gz")
    elif path.endswith(".tar.bz2"):
        return ZipCompatibleTarFile.open(path, "r:bz2")
    else:
        try:
            return zipfile.ZipFile(path, "r")
        except Exception:
            pass
        try:
            f = ZipCompatibleTarFile.open(path, "r")
            f.close()
            return ZipCompatibleTarFile.open(path, "r")
        except Exception:
            pass
    raise Exception("Unable to determine archive format for: " + path)


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------

def download_file(url: str, path: str, filename: str, overwrite: bool = False):
    """
    Download a zip/tar archive from *url* and extract it.
    Returns: (local_file_path, extracted_folder)
    The extract folder is named by a hash of the URL so the same URL is only
    downloaded once.
    """
    out_folder = os.path.join(path, hashlib.sha224(url.encode("utf-8")).hexdigest())
    out_file = os.path.join(out_folder, filename)

    if overwrite and os.path.exists(out_folder):
        shutil.rmtree(out_folder)

    if not os.path.exists(out_folder):
        LOG.info("Downloading %s", url)
        parsed = urlparse(url)
        suffix = os.path.splitext(parsed.path)[1] or ".zip"
        fp = tempfile.NamedTemporaryFile("wb", suffix=suffix, delete=False)
        try:
            if parsed.scheme in ("http", "https"):
                resp = requests.get(url, stream=True, verify=False, timeout=(30, 300))
                resp.raise_for_status()
                for chunk in resp.iter_content(chunk_size=8192):
                    fp.write(chunk)
            elif parsed.scheme == "ftp":
                with urllib.request.urlopen(url) as download:
                    while True:
                        buf = download.read(8192)
                        if not buf:
                            break
                        fp.write(buf)
            else:
                raise ValueError(f"Unsupported URL scheme: {parsed.scheme}")
        finally:
            fp.close()

        Path(out_folder).mkdir(parents=True, exist_ok=True)
        LOG.info("Extracting %s to %s", fp.name, out_folder)
        archive = get_compressed_file_wrapper(fp.name)
        archive.extractall(out_folder)
        archive.close()
        os.unlink(fp.name)

    return out_file, out_folder


def resolve_catalogue_to_wfs_layer(slug: str) -> str:
    """
    Resolve a BC Data Catalogue dataset slug to its BCGW WFS typeName.

    Queries the catalogue API and inspects each resource URL for an
    openmaps.gov.bc.ca WFS/OWS link, then extracts the layer name.

    Parameters
    ----------
    slug : str
        Last path segment of the catalogue URL
        (e.g. 'bcgs-1-20-000-grid').

    Returns
    -------
    str
        BCGW WFS typeName, e.g. 'WHSE_BASEMAPPING.BCGS_20K_GRID'.

    Raises
    ------
    ValueError
        If no WFS layer name can be resolved from the catalogue entry.
    """
    api_url = (
        f"https://catalogue.data.gov.bc.ca/api/3/action/package_show?id={slug}"
    )
    resp = requests.get(api_url, verify=False, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    resources = data.get("result", {}).get("resources", [])
    for res in resources:
        url = res.get("url", "")
        if "openmaps.gov.bc.ca" not in url:
            continue
        # Pattern 1: .../pub/SCHEMA.LAYER_NAME/wfs  or  .../pub/SCHEMA.LAYER_NAME/ows
        parts = url.split("/")
        for i, part in enumerate(parts):
            if part == "pub" and i + 1 < len(parts):
                candidate = parts[i + 1].split("?")[0]  # strip any query string
                if "." in candidate:
                    return candidate
        # Pattern 2: typeName=SCHEMA.LAYER_NAME in query string
        parsed_res = urlparse(url)
        qs = dict(
            p.split("=", 1) for p in parsed_res.query.split("&") if "=" in p
        )
        for key in ("typeName", "TYPENAME", "layers", "LAYERS"):
            if key in qs and "." in qs[key]:
                return qs[key]

    raise ValueError(
        f"Could not resolve a WFS layer name for catalogue slug '{slug}'. "
        f"No matching openmaps.gov.bc.ca resource was found in the catalogue "
        f"entry. Check the URL in your sources CSV."
    )


def download_bcgw_wfs(
    package: str,
    out_fc: str,
    query: str = None,
    workspace: str = None,
):
    """
    Download a BCGW layer via its public WFS endpoint and load it into
    an ArcGIS File Geodatabase feature class.

    Parameters
    ----------
    package : str
        The BCGW package/layer name (e.g. 'WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS').
    out_fc : str
        Full path to the output feature class (inside a .gdb workspace).
    query : str, optional
        CQL filter string to pass to the WFS endpoint.
    workspace : str, optional
        GDB workspace path (used to derive schema if out_fc is relative).
    """
    LOG.info("Downloading BCGW layer: %s", package)

    # Build WFS request — use GeoJSON output for easy loading
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "typeName": package,
        "outputFormat": "application/json",
        "SRSNAME": "EPSG:3005",
    }
    if query:
        params["CQL_FILTER"] = query

    resp = requests.get(BCGW_WFS_URL, params=params, verify=False, timeout=300)
    resp.raise_for_status()

    geojson_data = resp.json()
    feature_count = len(geojson_data.get("features", []))
    LOG.info("  Retrieved %d features", feature_count)

    if feature_count == 0:
        LOG.warning("  No features returned for %s", package)
        return

    # Strip Z/M coordinates and bbox metadata — JSONToFeatures can still infer
    # 3D geometry from bbox arrays even after coordinates are flattened.
    def _drop_z(coords):
        if not coords:
            return coords
        if isinstance(coords[0], (int, float)):
            return coords[:2]
        return [_drop_z(c) for c in coords]

    geojson_data.pop("bbox", None)
    for feature in geojson_data.get("features", []):
        feature.pop("bbox", None)
        geom = feature.get("geometry")
        if geom:
            geom.pop("bbox", None)
            if "coordinates" in geom:
                geom["coordinates"] = _drop_z(geom["coordinates"])

    # Write GeoJSON to a temp file and load with arcpy
    with tempfile.NamedTemporaryFile(
        "w", suffix=".geojson", delete=False, encoding="utf-8"
    ) as tf:
        json.dump(geojson_data, tf)
        tf_path = tf.name

    try:
        with arcpy.EnvManager(outputZFlag="Disabled", outputMFlag="Disabled"):
            arcpy.conversion.JSONToFeatures(tf_path, out_fc)
        LOG.info("  Loaded %s into %s", package, out_fc)
    finally:
        os.unlink(tf_path)


def load_file_to_gdb(
    src_file: str,
    layer: str,
    out_fc: str,
    sql_where: str = None,
):
    """
    Load a vector file (shapefile, GeoPackage, GDB layer, etc.) into
    a File Geodatabase feature class using arcpy.

    Parameters
    ----------
    src_file : str
        Path to the source file.
    layer : str
        Layer name within the source file (required for multi-layer formats).
    out_fc : str
        Full output feature class path.
    sql_where : str, optional
        SQL where clause to filter features on load.
    """
    LOG.info("Loading %s (layer=%s) -> %s", src_file, layer, out_fc)
    gdb = os.path.dirname(out_fc)
    fc_name = os.path.basename(out_fc)

    # Make feature layer to support optional SQL filter
    temp_lyr = "temp_load_lyr"
    if arcpy.Exists(temp_lyr):
        arcpy.management.Delete(temp_lyr)

    if layer:
        src = os.path.join(src_file, layer) if not os.path.isfile(src_file) else src_file
    else:
        src = src_file

    arcpy.management.MakeFeatureLayer(src, temp_lyr, sql_where or "")
    arcpy.conversion.FeatureClassToFeatureClass(temp_lyr, gdb, fc_name)
    arcpy.management.Delete(temp_lyr)
    LOG.info("  Done loading %s", fc_name)


def get_first_layer(path: str) -> str:
    """Return the name of the first layer in a multi-layer vector dataset."""
    desc = arcpy.Describe(path)
    if hasattr(desc, "datasetType") and desc.datasetType == "FeatureDataset":
        fcs = arcpy.ListFeatureClasses(feature_dataset=path)
        return fcs[0] if fcs else None
    # For GeoPackage / GDB workspace
    prev_ws = arcpy.env.workspace
    arcpy.env.workspace = path
    layers = arcpy.ListFeatureClasses()
    arcpy.env.workspace = prev_ws
    return layers[0] if layers else None


# ---------------------------------------------------------------------------
# Raster attribute table helper
# ---------------------------------------------------------------------------

def create_rat(raster_path: str, lookup: dict):
    """
    Write a raster attribute table (VALUE -> DESCRIPTION) to a .tif.
    Uses arcpy to build a VAT table, then joins a CSV-based lookup.

    Parameters
    ----------
    raster_path : str
        Path to an integer raster (.tif).
    lookup : dict
        {int_value: description_string} mapping.
    """
    LOG.info("Building raster attribute table for %s", raster_path)
    # arcpy BuildRasterAttributeTable works on integer rasters
    arcpy.management.BuildRasterAttributeTable(raster_path, "OVERWRITE")
    # Open the attribute table and add Description field
    fields = [f.name for f in arcpy.ListFields(raster_path)]
    if "DESCRIPTION" not in fields:
        arcpy.management.AddField(raster_path, "DESCRIPTION", "TEXT", field_length=255)
    # Update rows
    with arcpy.da.UpdateCursor(raster_path, ["VALUE", "DESCRIPTION"]) as cur:
        for row in cur:
            val = row[0]
            row[1] = lookup.get(val, str(val))
            cur.updateRow(row)


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class DesignatedLands:
    """Holds the job configuration, workspace, and all processing methods."""

    def __init__(self, config_file=None, recent_only=False,
                 start_date=None, end_date=None, exclude_federal=False):
        LOG.info("Initializing DesignatedLands")

        # Date filter settings
        self.recent_only = recent_only
        self.start_date = start_date or "2025-04-01"
        self.end_date = end_date or date.today().isoformat()
        self.exclude_federal = exclude_federal
        self.federal_excluded_sources = []  # populated by _read_sources
        if self.recent_only:
            LOG.info(
                "Date filter ENABLED: %s to %s",
                self.start_date, self.end_date,
            )
        if self.exclude_federal:
            LOG.info("Federal layers will be EXCLUDED")

        if not ARCPY_AVAILABLE:
            raise RuntimeError(
                "arcpy is not available. Run this script from the "
                "ArcGIS Pro Python Command Prompt."
            )

        # Load defaults then override from config file if provided
        self.config = DEFAULT_CONFIG.copy()
        if config_file:
            if not os.path.exists(config_file):
                raise ConfigValueError(f"Config file not found: {config_file}")
            self._read_config(config_file)

        # Resolve process count
        import multiprocessing
        cpu_count = multiprocessing.cpu_count()
        if self.config["n_processes"] == -1:
            self.config["n_processes"] = max(1, cpu_count - 1)
        elif self.config["n_processes"] > cpu_count:
            self.config["n_processes"] = cpu_count

        # Set arcpy parallel processing
        arcpy.env.parallelProcessingFactor = str(self.config["n_processes"])

        # Initialize File Geodatabase workspace
        self.gdb = os.path.abspath(self.config["gdb_path"])
        self._init_workspace()

        # Set arcpy environment
        arcpy.env.workspace = self.gdb
        arcpy.env.overwriteOutput = True
        arcpy.env.outputCoordinateSystem = arcpy.SpatialReference(3005)  # BC Albers

        # Restriction level lookup (name -> integer value)
        self.restriction_lookup = {
            "PROTECTED": 5,
            "FULL": 4,
            "HIGH": 3,
            "MEDIUM": 2,
            "LOW": 1,
            "NONE": 0,
        }

        # BC Albers raster extent and resolution
        self.bounds = BC_BOUNDS
        self.resolution = int(self.config["resolution"])
        self.raster_extent = arcpy.Extent(
            self.bounds["xmin"],
            self.bounds["ymin"],
            self.bounds["xmax"],
            self.bounds["ymax"],
        )

        # Load sources from CSV
        self._read_sources()

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def _init_workspace(self):
        """Create the File Geodatabase if it does not already exist."""
        gdb_dir = os.path.dirname(os.path.abspath(self.gdb))
        gdb_name = os.path.basename(self.gdb)
        if not arcpy.Exists(self.gdb):
            LOG.info("Creating File Geodatabase: %s", self.gdb)
            arcpy.management.CreateFileGDB(gdb_dir, gdb_name)
        else:
            LOG.info("Using existing File Geodatabase: %s", self.gdb)

    def _read_config(self, config_file: str):
        """Read a .cfg configuration file (INI format)."""
        parser = configparser.ConfigParser()
        parser.read(config_file)
        section = "designatedlands"
        if section not in parser:
            raise ConfigError(f"Config file must have a [{section}] section")
        cfg = dict(parser[section])
        # Type conversions
        for int_key in ("n_processes", "resolution"):
            if int_key in cfg:
                cfg[int_key] = int(cfg[int_key])
        if "out_path" in cfg:
            cfg["out_path"] = cfg["out_path"].lower()
        self.config.update(cfg)

    def _read_sources(self):
        """Load designation and supporting source CSV files."""
        # --- Designations ---
        designation_list = [
            s
            for s in csv.DictReader(open(self.config["sources_designations"]))
            if s.get("exclude", "").strip() != "T"
        ]
        self.sources = sorted(
            designation_list, key=lambda x: int(x["process_order"])
        )

        # Validate before any filtering removes sources
        self._validate_sources()

        # Exclude federal sources if requested
        if self.exclude_federal:
            kept = []
            for source in self.sources:
                if source.get("jurisdiction", "").strip().lower() == "federal":
                    LOG.info(
                        "Federal exclusion: REMOVING '%s'",
                        source.get("name", ""),
                    )
                    self.federal_excluded_sources.append({
                        "name": source.get("name", "").strip(),
                        "designation": source.get("designation", "").strip(),
                    })
                else:
                    kept.append(source)
            self.sources = kept

        # Tidy strings
        str_cols = [
            "designation", "source_id_col", "source_name_col",
            "forest_restriction", "og_restriction", "mine_restriction",
        ]
        for source in self.sources:
            for col in str_cols:
                source[col] = source.get(col, "").strip()

        # Build designations summary (unique process_order + designation)
        self.designations = (
            pd.DataFrame(self.sources)
            .astype({"process_order": int})[["process_order", "designation"]]
            .drop_duplicates()
            .sort_values("process_order")
            .to_dict("records")
        )

        # Apply date filter if enabled
        if self.recent_only:
            from date_filter import apply_date_filter_to_query, NO_DATE_FIELD, NON_BCGW
            filtered_sources = []
            for source in self.sources:
                template = source.get("date_filter_query", "").strip()
                if template in (NO_DATE_FIELD, NON_BCGW, ""):
                    LOG.info(
                        "Date filter: EXCLUDING '%s' (%s)",
                        source.get("name", ""), template or "no date_filter_query",
                    )
                    continue
                # Resolve placeholders and merge into existing query
                try:
                    date_cql = template.format(
                        start_date=self.start_date,
                        end_date=self.end_date,
                    )
                except KeyError:
                    LOG.warning(
                        "Date filter: bad placeholder in '%s' — skipping",
                        source.get("name", ""),
                    )
                    continue
                source["query"] = apply_date_filter_to_query(
                    source.get("query", ""), date_cql,
                )
                LOG.info(
                    "Date filter: '%s' -> %s",
                    source.get("name", ""), source["query"],
                )
                filtered_sources.append(source)
            self.sources = filtered_sources

        # Enrich each source with computed fields
        for i, source in enumerate(self.sources, start=1):
            source["id"] = i
            # Convert restriction names to integer codes
            source["forest_restriction"] = self.restriction_lookup[
                source["forest_restriction"].upper()
            ]
            source["og_restriction"] = self.restriction_lookup[
                source["og_restriction"].upper()
            ]
            source["mine_restriction"] = self.restriction_lookup[
                source["mine_restriction"].upper()
            ]
            source["process_order"] = str(source["process_order"]).zfill(2)
            # Feature class names (max 64 chars, no spaces)
            base = f"src_{source['process_order']}_{source['designation']}"[:60]
            source["src"] = base
            source["preprc"] = base + "_pp"  # preprocessed
            source["dl"] = f"dl_{source['process_order']}_{source['designation']}"[:60]

        # --- Supporting sources ---
        supporting_list = list(csv.DictReader(open(self.config["sources_supporting"])))
        for i, source in enumerate(supporting_list, start=len(self.sources) + 1):
            source["id"] = i
            source["process_order"] = "00"
            source["src"] = source.get("designation", f"supporting_{i}")
        self.sources_supporting = supporting_list

    def _validate_sources(self):
        """Basic validation of the sources CSV."""
        orders = [int(s["process_order"]) for s in self.sources]
        if min(orders) != 1:
            raise ValueError("Lowest process_order in source table must be 1")
        if len(set(orders)) != max(orders):
            raise ValueError(
                "process_order values must be a contiguous sequence starting at 1"
            )
        valid = set(self.restriction_lookup.keys())
        for s in self.sources:
            for field in ("forest_restriction", "og_restriction", "mine_restriction"):
                val = s.get(field, "").upper()
                if val not in valid:
                    raise ValueError(
                        f"Invalid {field} value '{val}' for designation '{s['designation']}'"
                    )

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def download(self, designation: str = None, overwrite: bool = False):
        """Download source data and load into the File Geodatabase."""
        sources = self.sources_supporting + self.sources

        if designation:
            sources = [s for s in sources if s["designation"] == designation]
            if not sources:
                raise ValueError(f"Designation '{designation}' not found in sources")

        Path(self.config["dl_path"]).mkdir(parents=True, exist_ok=True)

        # Auto-downloadable sources
        for source in [s for s in sources if s.get("manual_download", "") != "T"]:
            out_fc = os.path.join(self.gdb, source["src"])

            if overwrite and arcpy.Exists(out_fc):
                LOG.info("Dropping existing feature class: %s", source["src"])
                arcpy.management.Delete(out_fc)

            if arcpy.Exists(out_fc):
                LOG.info("%s already loaded — skipping", source["src"])
                continue

            url = source.get("url", "")
            if not url:
                LOG.warning("No URL for %s — skipping", source["designation"])
                continue

            parsed = urlparse(url)

            if parsed.hostname == "catalogue.data.gov.bc.ca":
                # BCGW download via public WFS
                slug = os.path.split(parsed.path)[1]
                try:
                    wfs_layer = resolve_catalogue_to_wfs_layer(slug)
                except (ValueError, requests.RequestException) as exc:
                    LOG.error(
                        "Could not resolve WFS layer for '%s': %s — skipping",
                        slug, exc,
                    )
                    continue
                # Expand {currdate} placeholder if present in query
                query = source.get("query", "") or ""
                if "{currdate}" in query:
                    query = query.format(currdate=date.today().isoformat())
                download_bcgw_wfs(
                    package=wfs_layer,
                    out_fc=out_fc,
                    query=query or None,
                )
            else:
                # Non-BCGW: download archive, extract, load
                file_in_url = source.get("file_in_url", "")
                layer_in_file = source.get("layer_in_file", "") or None
                sql_query = source.get("query", "") or None

                local_file, _ = download_file(
                    url=url,
                    path=self.config["dl_path"],
                    filename=file_in_url,
                    overwrite=overwrite,
                )

                # Determine layer name if not specified
                if not layer_in_file:
                    try:
                        layer_in_file = get_first_layer(local_file)
                    except Exception:
                        layer_in_file = None

                load_file_to_gdb(
                    src_file=local_file,
                    layer=layer_in_file,
                    out_fc=out_fc,
                    sql_where=sql_query,
                )

        # Manually-downloaded sources
        for source in [s for s in sources if s.get("manual_download", "") == "T"]:
            file_path = os.path.join(
                self.config["dl_path"], source.get("file_in_url", "")
            )
            if not os.path.exists(file_path):
                raise FileNotFoundError(
                    f"{file_path} does not exist — download it manually and place it "
                    f"in '{self.config['dl_path']}'"
                )
            out_fc = os.path.join(self.gdb, source["src"])
            if overwrite and arcpy.Exists(out_fc):
                arcpy.management.Delete(out_fc)
            if arcpy.Exists(out_fc):
                LOG.info("%s already loaded — skipping", source["src"])
                continue

            layer_in_file = source.get("layer_in_file", "") or None
            if not layer_in_file:
                try:
                    layer_in_file = get_first_layer(file_path)
                except Exception:
                    layer_in_file = None

            load_file_to_gdb(
                src_file=file_path,
                layer=layer_in_file,
                out_fc=out_fc,
                sql_where=source.get("query", "") or None,
            )

    # ------------------------------------------------------------------
    # Verify sources exist in GDB
    # ------------------------------------------------------------------

    def verify_sources(self):
        """Check that all expected source feature classes exist in the GDB.

        Call this before preprocess/process-vector when download was skipped
        to fail fast with a clear message instead of crashing mid-operation.

        Raises
        ------
        RuntimeError
            If one or more source feature classes are missing.
        """
        missing = []
        for source in self.sources:
            src_fc = os.path.join(self.gdb, source["src"])
            if not arcpy.Exists(src_fc):
                missing.append(source["src"])
        for source in self.sources_supporting:
            src_fc = os.path.join(self.gdb, source["src"])
            if not arcpy.Exists(src_fc):
                missing.append(source["src"])
        if missing:
            raise RuntimeError(
                f"{len(missing)} source feature class(es) missing from GDB "
                f"(download may have been skipped or cleanup removed them):\n"
                + "\n".join(f"  - {m}" for m in missing)
            )
        LOG.info("All %d source feature classes verified in GDB.",
                 len(self.sources) + len(self.sources_supporting))

    # ------------------------------------------------------------------
    # Preprocess
    # ------------------------------------------------------------------

    def preprocess(self, designation: str = None):
        """
        Preprocess sources as specified in the CSV.
        Supported operations:
          - clip  : clip source by another layer
          - union : dissolve/union overlapping features by column(s)
        """
        preprocess_sources = [s for s in self.sources if s.get("preprocess_operation")]
        if designation:
            preprocess_sources = [
                s for s in preprocess_sources if s["designation"] == designation
            ]

        for source in preprocess_sources:
            op = source["preprocess_operation"].strip().lower()
            src_fc = os.path.join(self.gdb, source["src"])
            out_fc = os.path.join(self.gdb, source["preprc"])

            if arcpy.Exists(out_fc):
                arcpy.management.Delete(out_fc)

            if op == "clip":
                clip_layer = os.path.join(self.gdb, source.get("preprocess_args", ""))
                if not arcpy.Exists(clip_layer):
                    raise RuntimeError(
                        f"Clip layer '{source['preprocess_args']}' not found in GDB. "
                        "Ensure it has been downloaded."
                    )
                LOG.info("Clipping %s by %s", source["src"], source["preprocess_args"])
                arcpy.analysis.Clip(src_fc, clip_layer, out_fc)

            elif op == "union":
                # Union (dissolve) by specified column(s)
                dissolve_fields = [
                    f.strip() for f in source.get("preprocess_args", "").split(",")
                ]
                LOG.info("Dissolving %s by %s", source["src"], dissolve_fields)
                arcpy.management.Dissolve(src_fc, out_fc, dissolve_fields)

            else:
                raise ValueError(
                    f"Unsupported preprocess_operation '{op}' for '{source['designation']}'"
                )

    # ------------------------------------------------------------------
    # BC boundary / tiles
    # ------------------------------------------------------------------

    def create_bc_boundary(self):
        """
        Create a combined land+marine BC boundary feature class from loaded sources.

        Expects these feature classes in the GDB:
          - bc_boundary_land
          - bc_abms
          - marine_ecosections
        """
        LOG.info("Creating bc_boundary")

        # Merge bc_abms and marine_ecosections into bc_boundary_marine
        marine_fc = os.path.join(self.gdb, "bc_boundary_marine")
        if arcpy.Exists(marine_fc):
            arcpy.management.Delete(marine_fc)

        bc_abms = os.path.join(self.gdb, "bc_abms")
        marine_eco = os.path.join(self.gdb, "marine_ecosections")
        arcpy.management.Merge([bc_abms, marine_eco], marine_fc)

        # Erase land from marine to get true marine-only extent, then merge both
        bc_boundary_fc = os.path.join(self.gdb, "bc_boundary")
        if arcpy.Exists(bc_boundary_fc):
            arcpy.management.Delete(bc_boundary_fc)

        land_fc = os.path.join(self.gdb, "bc_boundary_land")

        # Union land and marine, dissolve
        union_tmp = os.path.join(self.gdb, "bc_boundary_union_tmp")
        if arcpy.Exists(union_tmp):
            arcpy.management.Delete(union_tmp)

        arcpy.analysis.Union([land_fc, marine_fc], union_tmp, "ALL")
        arcpy.management.Dissolve(union_tmp, bc_boundary_fc)
        try:
            arcpy.management.Delete(union_tmp)
        except Exception:
            LOG.warning("Could not delete temp FC %s", union_tmp)

        # Add bc_boundary and restriction columns
        arcpy.management.AddField(bc_boundary_fc, "bc_boundary", "TEXT", field_length=50)
        arcpy.management.CalculateField(
            bc_boundary_fc, "bc_boundary", "'bc_boundary'", "PYTHON3"
        )
        for col in ["forest_restriction", "og_restriction", "mine_restriction"]:
            arcpy.management.AddField(bc_boundary_fc, col, "SHORT")

        LOG.info("bc_boundary created")

    # ------------------------------------------------------------------
    # Vector processing
    # ------------------------------------------------------------------

    def create_designations_overlapping(self):
        """
        Create a single feature class holding all designation polygons, clipped
        to the BC terrestrial boundary. Overlaps are preserved.
        """
        out_fc = os.path.join(self.gdb, "designations_overlapping")
        bc_land = os.path.join(self.gdb, "bc_boundary_land")

        if arcpy.Exists(out_fc):
            arcpy.management.Delete(out_fc)

        # Template feature class (defined by bc_boundary_land projection)
        arcpy.management.CreateFeatureclass(
            self.gdb,
            "designations_overlapping",
            "POLYGON",
            spatial_reference=arcpy.SpatialReference(3005),
        )
        # Add fields
        field_defs = [
            ("process_order", "SHORT", None),
            ("designation", "TEXT", 100),
            ("source_id", "TEXT", 255),
            ("source_name", "TEXT", 255),
            ("forest_restriction", "SHORT", None),
            ("og_restriction", "SHORT", None),
            ("mine_restriction", "SHORT", None),
        ]
        for fname, ftype, flen in field_defs:
            if flen:
                arcpy.management.AddField(out_fc, fname, ftype, field_length=flen)
            else:
                arcpy.management.AddField(out_fc, fname, ftype)

        insert_fields = [
            "process_order", "designation", "source_id", "source_name",
            "forest_restriction", "og_restriction", "mine_restriction", "SHAPE@",
        ]

        for source in self.sources:
            # Use preprocessed table if available
            src_fc = os.path.join(self.gdb, source["src"])
            if arcpy.Exists(os.path.join(self.gdb, source["preprc"])):
                src_fc = os.path.join(self.gdb, source["preprc"])

            if not arcpy.Exists(src_fc):
                LOG.warning("Source not found, skipping: %s", src_fc)
                continue

            LOG.info("Inserting %s into designations_overlapping", source["designation"])

            # Clip source to bc_boundary_land first
            clipped_tmp = os.path.join(self.gdb, "clip_tmp")
            if arcpy.Exists(clipped_tmp):
                arcpy.management.Delete(clipped_tmp)
            arcpy.analysis.Clip(src_fc, bc_land, clipped_tmp)

            # Determine source id/name fields
            src_id_col = source.get("source_id_col", "")
            src_name_col = source.get("source_name_col", "")
            existing_fields = {f.name.lower() for f in arcpy.ListFields(clipped_tmp)}
            has_id = src_id_col and src_id_col.lower() in existing_fields
            has_name = src_name_col and src_name_col.lower() in existing_fields

            po = int(source["process_order"])
            des = source["designation"]
            fr = source["forest_restriction"]
            ogr = source["og_restriction"]
            mr = source["mine_restriction"]

            read_fields = ["SHAPE@"]
            if has_id:
                read_fields = [src_id_col] + read_fields
            if has_name:
                read_fields = [src_name_col] + read_fields

            with arcpy.da.SearchCursor(clipped_tmp, read_fields) as s_cur:
                with arcpy.da.InsertCursor(out_fc, insert_fields) as i_cur:
                    for row in s_cur:
                        # Parse values based on what fields are available
                        idx = 0
                        src_id_val = None
                        src_name_val = None
                        if has_id:
                            src_id_val = str(row[idx]) if row[idx] is not None else None
                            idx += 1
                        if has_name:
                            src_name_val = str(row[idx]) if row[idx] is not None else None
                            idx += 1
                        geom = row[idx]
                        if geom is None or geom.area == 0:
                            continue
                        i_cur.insertRow([
                            po, des, src_id_val, src_name_val,
                            fr, ogr, mr, geom
                        ])

            try:
                arcpy.management.Delete(clipped_tmp)
            except Exception:
                LOG.warning(
                    "Could not delete temp FC %s — will be cleaned up on next iteration",
                    clipped_tmp,
                )

        # Build spatial index
        arcpy.management.AddSpatialIndex(out_fc)
        LOG.info("designations_overlapping created")

    def create_designations_planarized(self):
        """
        From designations_overlapping, remove overlaps using Union + ranking.

        Union splits all polygons at every intersection boundary, creating
        planar topology.  Where designations overlap, the Union output
        contains multiple rows with identical geometry but different
        attributes.  This method groups those spatially identical fragments
        and for each group:
          - assigns the designation with the LOWEST process_order
            (= highest priority)
          - retains the MAXIMUM restriction value for each industry
            across all overlapping designations
          - records ALL contributing designation names in a semicolon-
            delimited ``overlapping_designations`` field
        """
        LOG.info("Creating designations_planarized")
        overlapping_fc = os.path.join(self.gdb, "designations_overlapping")
        out_fc = os.path.join(self.gdb, "designations_planarized")

        if arcpy.Exists(out_fc):
            arcpy.management.Delete(out_fc)

        # Union all overlapping features to create planar topology
        union_tmp = os.path.join(self.gdb, "planar_union_tmp")
        if arcpy.Exists(union_tmp):
            try:
                arcpy.management.Delete(union_tmp)
            except Exception:
                LOG.warning(
                    "Cannot delete corrupted %s — compacting GDB and retrying",
                    union_tmp,
                )
                arcpy.management.Compact(self.gdb)
                arcpy.management.Delete(union_tmp)

        LOG.info("Running Union to create planar topology...")
        arcpy.analysis.Union([overlapping_fc], union_tmp, "ALL")

        # Create output feature class
        arcpy.management.CreateFeatureclass(
            self.gdb,
            "designations_planarized",
            "POLYGON",
            spatial_reference=arcpy.SpatialReference(3005),
        )

        planar_fields = [
            ("process_order", "SHORT", None),
            ("designation", "TEXT", 255),
            ("overlapping_designations", "TEXT", 1000),
            ("source_id", "TEXT", 255),
            ("source_name", "TEXT", 255),
            ("forest_restriction_max", "SHORT", None),
            ("mine_restriction_max", "SHORT", None),
            ("og_restriction_max", "SHORT", None),
        ]
        for fname, ftype, flen in planar_fields:
            if flen:
                arcpy.management.AddField(out_fc, fname, ftype, field_length=flen)
            else:
                arcpy.management.AddField(out_fc, fname, ftype)

        out_fields = [
            "process_order", "designation", "overlapping_designations",
            "source_id", "source_name",
            "forest_restriction_max", "mine_restriction_max", "og_restriction_max",
            "SHAPE@",
        ]

        # Build lookup: process_order -> designation info
        po_lookup = {}
        for s in self.sources:
            po = int(s["process_order"])
            if po not in po_lookup:
                po_lookup[po] = {
                    "designation": s["designation"],
                    "source_id": "",
                    "source_name": "",
                }

        # -----------------------------------------------------------------
        # Aggregate Union fragments
        # -----------------------------------------------------------------
        # After Union, overlapping areas produce multiple rows with
        # identical geometry but different attributes (one per
        # contributing input polygon).  We group these spatially
        # identical fragments using a composite key of centroid
        # coordinates + area + perimeter (high-precision rounding),
        # then for each group:
        #   - pick MIN(process_order)  →  designation (highest priority)
        #   - pick MAX of each restriction field
        #   - collect ALL designation names  →  overlapping_designations
        #
        # The grouping key does NOT modify geometry — the original Union
        # geometry is passed through unchanged, so polygon areas remain
        # perfectly accurate.
        # -----------------------------------------------------------------
        LOG.info("Reading Union fragments and grouping by spatial location...")
        groups = {}
        read_fields = [
            "process_order", "designation",
            "forest_restriction", "og_restriction", "mine_restriction",
            "SHAPE@",
        ]
        frag_count = 0

        with arcpy.da.SearchCursor(union_tmp, read_fields) as cur:
            for po, desig, fr, og, mr, geom in cur:
                if geom is None or geom.area == 0:
                    continue
                if po is None or int(po) <= 0:
                    continue

                frag_count += 1
                po = int(po)
                fr = fr if fr is not None else 0
                og = og if og is not None else 0
                mr = mr if mr is not None else 0
                desig = desig or ""

                # Composite key: centroid X/Y (7 dp ≈ 0.01 m in BC Albers)
                # + area (2 dp) + perimeter (2 dp) to minimise collision
                # risk between genuinely different polygons.
                c = geom.trueCentroid
                key = (
                    round(c.X, 7),
                    round(c.Y, 7),
                    round(geom.area, 2),
                    round(geom.length, 2),
                )

                if key not in groups:
                    groups[key] = {
                        "min_po": po,
                        "max_fr": fr,
                        "max_og": og,
                        "max_mr": mr,
                        "designations": [desig],
                        "geom": geom,
                    }
                else:
                    g = groups[key]
                    g["min_po"] = min(g["min_po"], po)
                    g["max_fr"] = max(g["max_fr"], fr)
                    g["max_og"] = max(g["max_og"], og)
                    g["max_mr"] = max(g["max_mr"], mr)
                    if desig and desig not in g["designations"]:
                        g["designations"].append(desig)

        LOG.info(
            "Grouped %d Union fragments into %d unique planar polygons",
            frag_count, len(groups),
        )

        # Insert aggregated results into the output feature class
        LOG.info("Writing planarized features...")
        with arcpy.da.InsertCursor(out_fc, out_fields) as i_cur:
            for data in groups.values():
                info = po_lookup.get(data["min_po"], {})
                # Sort overlapping designations by process_order for
                # consistent display (lowest order = highest priority first)
                all_desigs = sorted(
                    data["designations"],
                    key=lambda d: next(
                        (int(s["process_order"]) for s in self.sources
                         if s["designation"] == d),
                        999,
                    ),
                )
                overlapping_str = "; ".join(all_desigs)
                i_cur.insertRow([
                    data["min_po"],
                    info.get("designation", ""),
                    overlapping_str,
                    info.get("source_id", ""),
                    info.get("source_name", ""),
                    data["max_fr"],
                    data["max_mr"],
                    data["max_og"],
                    data["geom"],
                ])

        # Clean up temporary data
        try:
            arcpy.management.Delete(union_tmp)
        except Exception:
            LOG.warning("Could not delete temp FC %s", union_tmp)
        arcpy.management.AddSpatialIndex(out_fc)
        LOG.info("designations_planarized created with %d features", len(groups))

    # ------------------------------------------------------------------
    # Raster processing
    # ------------------------------------------------------------------

    def rasterize(self):
        """
        Rasterize the designations_overlapping feature class for each
        process_order value, producing one GeoTIFF per order.
        Uses arcpy.conversion.PolygonToRaster.
        """
        rasters_dir = Path("rasters")
        rasters_dir.mkdir(parents=True, exist_ok=True)

        overlapping_fc = os.path.join(self.gdb, "designations_overlapping")
        bc_land = os.path.join(self.gdb, "bc_boundary_land")

        # Set raster environment
        arcpy.env.snapRaster = None
        arcpy.env.extent = self.raster_extent
        arcpy.env.cellSize = self.resolution
        arcpy.env.mask = bc_land
        arcpy.env.outputCoordinateSystem = arcpy.SpatialReference(3005)

        # Rasterize BC boundary base (process_order = 0)
        LOG.info("Rasterizing bc_boundary_land (base raster)")
        base_tif = str(rasters_dir / "dl_0.tif")
        # Add a constant field to rasterize
        tmp_lyr = "bc_land_lyr"
        arcpy.management.MakeFeatureLayer(bc_land, tmp_lyr)
        arcpy.management.AddField(bc_land, "BURN_VAL", "SHORT")
        arcpy.management.CalculateField(bc_land, "BURN_VAL", "0", "PYTHON3")
        arcpy.conversion.PolygonToRaster(
            bc_land, "BURN_VAL", base_tif, "CELL_CENTER", "", self.resolution
        )
        arcpy.management.DeleteField(bc_land, "BURN_VAL")

        # Rasterize each process_order level
        process_orders = sorted(
            set(int(s["process_order"]) for s in self.sources), reverse=True
        )
        for po in process_orders:
            LOG.info("Rasterizing process_order %d", po)
            out_tif = str(rasters_dir / f"dl_{po}.tif")

            # Select features for this order
            tmp_lyr = f"po_{po}_lyr"
            arcpy.management.MakeFeatureLayer(
                overlapping_fc, tmp_lyr, f"process_order = {po}"
            )
            count = int(arcpy.management.GetCount(tmp_lyr).getOutput(0))
            if count == 0:
                LOG.info("  No features for process_order %d — skipping", po)
                arcpy.management.Delete(tmp_lyr)
                continue

            # Add a temporary burn value field
            arcpy.management.AddField(tmp_lyr, "BURN_VAL", "SHORT")
            arcpy.management.CalculateField(tmp_lyr, "BURN_VAL", str(po), "PYTHON3")
            arcpy.conversion.PolygonToRaster(
                tmp_lyr, "BURN_VAL", out_tif, "CELL_CENTER", "", self.resolution
            )
            arcpy.management.Delete(tmp_lyr)

    def overlay_rasters(self):
        """
        Overlay individual per-order rasters into four output rasters:
          - designatedlands.tif         (designation process_order, highest wins)
          - forest_restriction.tif      (max forest restriction)
          - og_restriction.tif          (max oil & gas restriction)
          - mine_restriction.tif        (max mine restriction)

        Uses arcpy.RasterToNumPyArray / arcpy.NumPyArrayToRaster to mirror the
        original rasterio / numpy approach.
        """
        LOG.info("Overlaying rasters")
        rasters_dir = Path("rasters")
        out_dir = Path(self.config["out_path"])
        out_dir.mkdir(parents=True, exist_ok=True)

        nodata_val = np.uint8(255)

        # Load BC boundary base raster
        base_path = str(rasters_dir / "dl_0.tif")
        if not os.path.exists(base_path):
            raise FileNotFoundError(
                "Base raster dl_0.tif not found — run rasterize first."
            )

        base_raster = arcpy.Raster(base_path)
        lower_left = arcpy.Point(base_raster.extent.XMin, base_raster.extent.YMin)
        cell_size = base_raster.meanCellWidth
        no_data = base_raster.noDataValue

        designation_arr = arcpy.RasterToNumPyArray(base_raster, nodata_to_value=255)
        designation_arr = designation_arr.astype(np.uint8)
        forest_restriction = designation_arr.copy()
        og_restriction = designation_arr.copy()
        mine_restriction = designation_arr.copy()

        # Sort sources by process_order descending (lowest-priority processed first
        # so highest-priority overwrites)
        source_data = sorted(
            set(
                (
                    int(s["process_order"]),
                    int(s["forest_restriction"]),
                    int(s["og_restriction"]),
                    int(s["mine_restriction"]),
                )
                for s in self.sources
            ),
            key=lambda x: -x[0],  # highest process_order first
        )

        for po, fr_val, og_val, mr_val in source_data:
            tif_path = str(rasters_dir / f"dl_{po}.tif")
            if not os.path.exists(tif_path):
                LOG.info("Raster dl_%d.tif not found — skipping", po)
                continue

            LOG.info("Processing process_order %d", po)
            po_raster = arcpy.Raster(tif_path)
            B = arcpy.RasterToNumPyArray(po_raster, nodata_to_value=255).astype(np.uint8)

            # Cells that are within BC and have this process_order value
            index = (designation_arr < 255) & (B == po)

            # Update designation
            designation_arr[index] = np.uint8(po)

            # Update restriction layers where new value is more restrictive
            forest_restriction[index & (forest_restriction < fr_val)] = np.uint8(fr_val)
            og_restriction[index & (og_restriction < og_val)] = np.uint8(og_val)
            mine_restriction[index & (mine_restriction < mr_val)] = np.uint8(mr_val)

        # Write output rasters
        out_rasters = [
            (designation_arr, "designatedlands"),
            (forest_restriction, "forest_restriction"),
            (og_restriction, "og_restriction"),
            (mine_restriction, "mine_restriction"),
        ]

        for arr, name in out_rasters:
            out_path = str(out_dir / f"{name}.tif")
            LOG.info("Writing %s", out_path)
            out_raster = arcpy.NumPyArrayToRaster(
                arr,
                lower_left,
                cell_size,
                value_to_nodata=255,
            )
            out_raster.save(out_path)

            # Define projection
            arcpy.management.DefineProjection(out_path, arcpy.SpatialReference(3005))

        # Build raster attribute tables
        restriction_lookup_inv = {v: k for k, v in self.restriction_lookup.items()}
        for r in ("forest", "og", "mine"):
            tif = str(out_dir / f"{r}_restriction.tif")
            create_rat(tif, restriction_lookup_inv)

        designation_lookup = {
            int(s["process_order"]): s["designation"] for s in self.sources
        }
        create_rat(str(out_dir / "designatedlands.tif"), designation_lookup)

    # ------------------------------------------------------------------
    # Dump outputs
    # ------------------------------------------------------------------

    def dump(self):
        """Export output feature classes to a File Geodatabase."""
        out_dir = Path(self.config["out_path"]).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        out_gdb_name = "designatedlands_output.gdb"
        out_gdb = str(out_dir / out_gdb_name)

        if not arcpy.Exists(out_gdb):
            arcpy.management.CreateFileGDB(str(out_dir), out_gdb_name)
            LOG.info("Created output File Geodatabase: %s", out_gdb)

        for fc_name in ("designations_planarized", "designations_overlapping"):
            fc_path = os.path.join(self.gdb, fc_name)
            if not arcpy.Exists(fc_path):
                LOG.warning("%s not found — skipping dump", fc_name)
                continue
            out_fc = os.path.join(out_gdb, fc_name)
            if arcpy.Exists(out_fc):
                arcpy.management.Delete(out_fc)
            LOG.info("Dumping %s to %s", fc_name, out_gdb)
            arcpy.conversion.FeatureClassToFeatureClass(
                fc_path, out_gdb, fc_name,
            )
            LOG.info("Exported %s to output GDB", fc_name)

    # ------------------------------------------------------------------
    # Overlay
    # ------------------------------------------------------------------

    def overlay(self, in_file: str, out_file: str, in_layer: str = None, out_layer: str = None):
        """
        Intersect an input layer with designations_overlapping and write output to file.
        """
        dl_fc = os.path.join(self.gdb, "designations_overlapping")
        if not arcpy.Exists(dl_fc):
            raise RuntimeError(
                "designations_overlapping not found. Run process-vector first."
            )

        # Determine input layer name
        if not in_layer:
            try:
                in_layer = get_first_layer(in_file)
            except Exception:
                in_layer = None

        src_path = os.path.join(in_file, in_layer) if in_layer else in_file

        if not out_layer:
            out_layer = (
                Path(in_file).stem if not in_layer else in_layer
            )

        LOG.info("Overlaying %s with designations_overlapping", src_path)

        # Load input layer to GDB temporarily
        tmp_fc = os.path.join(self.gdb, "overlay_input_tmp")
        if arcpy.Exists(tmp_fc):
            arcpy.management.Delete(tmp_fc)
        arcpy.conversion.FeatureClassToFeatureClass(
            src_path, self.gdb, "overlay_input_tmp"
        )

        # Intersect
        overlay_tmp = os.path.join(self.gdb, "overlay_output_tmp")
        if arcpy.Exists(overlay_tmp):
            arcpy.management.Delete(overlay_tmp)
        arcpy.analysis.Intersect([tmp_fc, dl_fc], overlay_tmp)

        # Export result to File Geodatabase
        out_file = os.path.abspath(out_file)
        out_dir = os.path.dirname(out_file)
        if not arcpy.Exists(out_file):
            arcpy.management.CreateFileGDB(out_dir, os.path.basename(out_file))
        arcpy.conversion.FeatureClassToFeatureClass(
            overlay_tmp, out_file, out_layer,
        )
        LOG.info("Overlay result written to %s\\%s", out_file, out_layer)

        # Clean up temp layers
        try:
            arcpy.management.Delete(tmp_fc)
        except Exception:
            LOG.warning("Could not delete temp FC %s", tmp_fc)
        try:
            arcpy.management.Delete(overlay_tmp)
        except Exception:
            LOG.warning("Could not delete temp FC %s", overlay_tmp)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Remove temporary source and preprocess feature classes from the GDB."""
        LOG.info("Cleaning up src_ and _pp feature classes")
        arcpy.env.workspace = self.gdb
        for source in self.sources:
            for fc_name in (source["src"], source["preprc"]):
                fc_path = os.path.join(self.gdb, fc_name)
                if arcpy.Exists(fc_path):
                    LOG.info("  Deleting %s", fc_name)
                    arcpy.management.Delete(fc_path)

    # ------------------------------------------------------------------
    # Test connection
    # ------------------------------------------------------------------

    def test_connection(self):
        """Verify the GDB workspace is accessible."""
        if arcpy.Exists(self.gdb):
            print(f"GDB workspace accessible: {self.gdb}")
            fcs = arcpy.ListFeatureClasses()
            print(f"Feature classes found: {len(fcs) if fcs else 0}")
        else:
            print(f"GDB workspace NOT found: {self.gdb}")


# ---------------------------------------------------------------------------
# CLI — argparse subcommands
# ---------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="designatedlands.py",
        description=(
            "Combine spatial data for 40+ designations to create a single "
            "Designated Lands layer for British Columbia. "
            "Runs in ArcGIS Pro's arcgispro-py3 Python environment."
        ),
    )
    parser.add_argument(
        "--config", "-c",
        metavar="CONFIG_FILE",
        help="Path to .cfg configuration file",
        default=None,
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--quiet", "-q", action="store_true", help="Suppress output")
    parser.add_argument(
        "--recent-only", action="store_true",
        help="Enable date filtering — only process features changed in the date window",
    )
    parser.add_argument(
        "--start-date", metavar="YYYY-MM-DD", default=None,
        help="Start date for the filter window (default: 2025-04-01)",
    )
    parser.add_argument(
        "--end-date", metavar="YYYY-MM-DD", default=None,
        help="End date for the filter window (default: today)",
    )
    parser.add_argument(
        "--exclude-federal", action="store_true",
        help="Exclude all federally protected areas from the output",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    # test-connection
    sub.add_parser("test-connection", help="Verify GDB workspace is accessible")

    # download
    dl = sub.add_parser("download", help="Download source data and load to GDB")
    dl.add_argument("--designation", "-d", help="Only download this designation")
    dl.add_argument("--overwrite", action="store_true", help="Re-download existing data")

    # preprocess
    pp = sub.add_parser("preprocess", help="Preprocess sources and create BC boundary")
    pp.add_argument("--designation", "-d", help="Only preprocess this designation")

    # process-vector
    sub.add_parser("process-vector", help="Create vector designation/restriction layers")

    # process-raster
    sub.add_parser("process-raster", help="Create raster designation/restriction layers")

    # dump
    sub.add_parser("dump", help="Dump output feature classes to File Geodatabase")

    # overlay
    ov = sub.add_parser("overlay", help="Intersect a layer with designatedlands")
    ov.add_argument("in_file", help="Input vector file")
    ov.add_argument("out_file", help="Output File Geodatabase path")
    ov.add_argument("--in_layer", "-l", help="Layer name in input file")
    ov.add_argument("--out_layer", "-nln", help="Output layer name")

    # cleanup
    sub.add_parser("cleanup", help="Remove temporary feature classes from GDB")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    set_log_level(args.verbose, args.quiet)

    DL = DesignatedLands(
        config_file=args.config,
        recent_only=args.recent_only,
        start_date=args.start_date,
        end_date=args.end_date,
        exclude_federal=args.exclude_federal,
    )

    cmd = args.command

    if cmd == "test-connection":
        DL.test_connection()

    elif cmd == "download":
        DL.download(designation=getattr(args, "designation", None),
                    overwrite=args.overwrite)
        if DL.recent_only:
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

    elif cmd == "preprocess":
        DL.preprocess(designation=getattr(args, "designation", None))
        DL.create_bc_boundary()

    elif cmd == "process-vector":
        DL.create_designations_overlapping()
        DL.create_designations_planarized()

    elif cmd == "process-raster":
        DL.rasterize()
        DL.overlay_rasters()

    elif cmd == "dump":
        DL.dump()

    elif cmd == "overlay":
        DL.overlay(
            in_file=args.in_file,
            out_file=args.out_file,
            in_layer=getattr(args, "in_layer", None),
            out_layer=getattr(args, "out_layer", None),
        )

    elif cmd == "cleanup":
        DL.cleanup()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
