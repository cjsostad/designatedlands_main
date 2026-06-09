"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, and writes the
filtered feature class into source_data/.

Download behaviour:
    - Attempts to download CriticalHabitat.zip from ECCC's data portal.
    - On success, extracts CriticalHabitat.gdb directly into source_data/
        (no hash folders).
    - If the download fails, falls back to an existing
        source_data/CriticalHabitat.gdb if one is present.
    - If neither succeeds, raises an error with manual download instructions.

Manual download fallback (e.g. if behind a firewall):
    1. Download CriticalHabitat.zip from the URL in sources_supporting.csv
       (or directly from https://data-donnees.az.ec.gc.ca)
    2. Extract CriticalHabitat.gdb from the zip
    3. Place CriticalHabitat.gdb inside the source_data/ directory
    4. Re-run — the script will detect and use the local copy

Can be run standalone or called from the pipeline via prepare_cha().

Standalone usage:
    python create_cha.py
    python create_cha.py --no-overwrite
"""

import argparse
import csv
import logging
import os
import shutil
import sys
import tempfile
import time
from urllib.request import urlopen, Request
from urllib.error import URLError
import zipfile

LOG = logging.getLogger(__name__)

CHA_DESIGNATION = "critical_habitat_area"
CHA_GDB_NAME = "CriticalHabitat.gdb"


def _find_cha_config(csv_path=None):
    """
    Read the CHA row from sources_supporting.csv.

    Returns a dict with keys: url, file_in_url, layer_in_file, query.
    """
    if csv_path is None:
        csv_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "sources_supporting.csv",
        )
    with open(csv_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if row.get("designation", "").strip() == CHA_DESIGNATION:
                return {
                    "url": row.get("url", "").strip(),
                    "file_in_url": row.get("file_in_url", "").strip(),
                    "layer_in_file": row.get("layer_in_file", "").strip(),
                    "query": row.get("query", "").strip(),
                }
    raise ValueError(
        f"No row with designation='{CHA_DESIGNATION}' found in {csv_path}"
    )


def _download_cha_zip(url, dest_dir, max_retries=3, timeout=60):
    """
    Download the CHA zip from *url*, extract CriticalHabitat.gdb directly
    into *dest_dir* (no hash folders).

    Includes retry logic with exponential backoff to handle intermittent
    connection failures (e.g., WinError 10054).

    Parameters
    ----------
    url : str
        URL to download the zip archive from.
    dest_dir : str
        Directory to extract the GDB into.
    max_retries : int, optional
        Maximum number of download attempts (default: 3).
    timeout : int, optional
        Timeout in seconds for the connection (default: 60).

    Returns
    -------
    str or None
        Path to the extracted GDB, or None if all download attempts failed.
    """
    src_gdb_path = os.path.join(dest_dir, CHA_GDB_NAME)
    temp_zip_path = None

    for attempt in range(1, max_retries + 1):
        try:
            print(f"[CHA] Download attempt {attempt}/{max_retries} from {url}...")
            
            with tempfile.NamedTemporaryFile(
                "wb", suffix=".zip", delete=False, dir=dest_dir
            ) as temp_file:
                temp_zip_path = temp_file.name

            # Create request with timeout
            request = Request(url)
            request.add_header('User-Agent', 'Mozilla/5.0')  # Some servers require User-Agent
            
            with urlopen(request, timeout=timeout) as response:
                total_size = response.headers.get('Content-Length')
                if total_size:
                    total_size = int(total_size)
                    print(f"[CHA] File size: {total_size / (1024*1024):.1f} MB")
                
                downloaded = 0
                chunk_size = 1024 * 1024  # 1MB chunks
                
                with open(temp_zip_path, "wb") as output_file:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        output_file.write(chunk)
                        downloaded += len(chunk)
                        
                        # Progress reporting
                        if total_size:
                            percent = (downloaded / total_size) * 100
                            print(f"[CHA] Progress: {percent:.1f}% ({downloaded / (1024*1024):.1f} MB)", end='\r')
                
                if total_size:
                    print()  # New line after progress bar

            print(f"[CHA] Download complete. Extracting...")

            # Extract the zip directly into dest_dir. We intentionally do NOT
            # delete the old GDB first — it may be locked by ArcGIS Pro while
            # the pipeline is running, and shutil.rmtree would fail with
            # WinError 5 (Access Denied), discarding a successful download.
            # zipfile.ZipFile.extractall() overwrites individual files in place,
            # which works fine even when the GDB folder already exists.
            print(f"[CHA] Extracting {CHA_GDB_NAME} into {dest_dir}...")
            with zipfile.ZipFile(temp_zip_path, "r") as zf:
                zf.extractall(dest_dir)

            # Check if GDB was extracted at expected location
            if os.path.exists(src_gdb_path):
                print(f"[CHA] Extracted: {src_gdb_path}")
                return src_gdb_path

            # Check for nested GDB and move it
            for root, dirs, _files in os.walk(dest_dir):
                if CHA_GDB_NAME in dirs:
                    nested = os.path.join(root, CHA_GDB_NAME)
                    if nested != src_gdb_path:
                        shutil.move(nested, src_gdb_path)
                        print(f"[CHA] Moved nested GDB to: {src_gdb_path}")
                    return src_gdb_path

            print(f"[CHA] WARNING: Zip extracted but {CHA_GDB_NAME} not found")
            return None

        except URLError as exc:
            # Network-related errors (including WinError 10054)
            if attempt < max_retries:
                wait_time = 2 ** attempt  # Exponential backoff: 2, 4, 8 seconds
                print(f"[CHA] Download failed: {exc}")
                print(f"[CHA] Retrying in {wait_time} seconds...")
                time.sleep(wait_time)
            else:
                print(f"[CHA] Download failed after {max_retries} attempts: {exc}")
                return None
        except Exception as exc:
            # Other errors (zip extraction, file system, etc.)
            print(f"[CHA] Download failed: {exc}")
            return None
        finally:
            # Clean up temp zip file
            if temp_zip_path and os.path.exists(temp_zip_path):
                try:
                    os.unlink(temp_zip_path)
                except Exception:
                    pass  # Ignore cleanup errors
    
    return None


def prepare_cha(source_data_dir=None, overwrite=True, csv_path=None,
                query_override=None):
    """
    Download the CHA archive, extract it, apply the definition query,
    and write the filtered feature class into source_data/.

    If the download fails, falls back to an existing
    CriticalHabitat.gdb in source_data/ (e.g. manually downloaded).

    Returns
    -------
    str
        Path to the output feature class in source_data/.
    """
    import arcpy

    cfg = _find_cha_config(csv_path)

    if not cfg["url"]:
        raise ValueError("CHA row in sources_supporting.csv has no URL")

    if source_data_dir is None:
        source_data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "source_data",
        )
    os.makedirs(source_data_dir, exist_ok=True)

    # Output FC goes into a file GDB inside source_data/
    out_gdb = os.path.join(source_data_dir, "critical_habitat_area.gdb")
    out_fc = os.path.join(out_gdb, CHA_DESIGNATION)

    if arcpy.Exists(out_fc) and not overwrite:
        LOG.info("CHA feature class already exists: %s — skipping", out_fc)
        print(f"[CHA] Already exists: {out_fc} (use overwrite=True to rebuild)")
        return out_fc

    # Try to download; fall back to existing GDB if download fails
    src_gdb_path = os.path.join(source_data_dir, CHA_GDB_NAME)

    downloaded = _download_cha_zip(cfg["url"], source_data_dir)

    if downloaded:
        src_gdb = downloaded
    elif os.path.exists(src_gdb_path):
        print(f"[CHA] Falling back to existing {src_gdb_path}")
        src_gdb = src_gdb_path
    else:
        raise RuntimeError(
            f"Download failed and no existing {CHA_GDB_NAME} found in "
            f"{source_data_dir}.\n\n"
            f"To proceed manually:\n"
            f"  1. Download the zip from:\n"
            f"     {cfg['url']}\n"
            f"  2. Extract {CHA_GDB_NAME} from the zip\n"
            f"  3. Place {CHA_GDB_NAME} into the source_data/ folder\n"
            f"     in the same directory as this script\n"
            f"  4. Re-run the script"
        )

    print(f"[CHA] Using source GDB: {src_gdb}")

    # Determine layer name
    layer = cfg["layer_in_file"] or None
    prev_ws = arcpy.env.workspace
    arcpy.env.workspace = src_gdb
    available_fcs = [fc.lower() for fc in (arcpy.ListFeatureClasses() or [])]
    arcpy.env.workspace = prev_ws

    if layer and layer.lower() in available_fcs:
        # Configured layer name exists — use it
        src = os.path.join(src_gdb, layer)
    else:
        if layer:
            # Configured layer name not found — warn and auto-detect
            print(f"[CHA] WARNING: Configured layer '{layer}' not found in "
                  f"{src_gdb}. Available: {available_fcs}. Auto-detecting...")
            LOG.warning("Configured layer_in_file '%s' not found in %s; "
                        "available: %s", layer, src_gdb, available_fcs)
        if not available_fcs:
            raise ValueError(f"No feature classes found in {src_gdb}")
        # Use the first feature class found
        arcpy.env.workspace = src_gdb
        first_fc = (arcpy.ListFeatureClasses() or [])[0]
        arcpy.env.workspace = prev_ws
        src = os.path.join(src_gdb, first_fc)
        layer = first_fc
        print(f"[CHA] Auto-detected layer: {layer}")

    # Create or recreate output GDB
    if arcpy.Exists(out_gdb):
        print(f"[CHA] Deleting existing output GDB...")
        arcpy.management.Delete(out_gdb)
    print(f"[CHA] Creating output GDB: {out_gdb}")
    arcpy.management.CreateFileGDB(
        os.path.dirname(out_gdb), os.path.basename(out_gdb),
    )

    # Apply definition query and export
    if query_override is not None:
        sql_where = query_override
    else:
        sql_where = cfg["query"].strip('"') or ""
    print(f"[CHA] Applying definition query and exporting...")
    LOG.info("CHA query: %s", sql_where)

    temp_lyr = "cha_temp_lyr"
    if arcpy.Exists(temp_lyr):
        arcpy.management.Delete(temp_lyr)

    arcpy.management.MakeFeatureLayer(src, temp_lyr, sql_where)

    count = int(arcpy.management.GetCount(temp_lyr)[0])
    print(f"[CHA] {count} features matched the definition query")

    # Stamp the original ECCC OBJECTID into a regular attribute field so it
    # survives FeatureClassToFeatureClass OID re-numbering and flows through
    # PairwiseIntersect into all output tables. This lets users join
    # CHA_Source_ID back to the national CriticalHabitat.gdb on OBJECTID
    # without needing the locally-filtered intermediate copy. (2026-05-27)
    arcpy.management.AddField(temp_lyr, "CHA_Source_ID", "LONG")
    arcpy.management.CalculateField(temp_lyr, "CHA_Source_ID", "!OBJECTID!", "PYTHON3")
    print("[CHA] Stamped original ECCC OBJECTID into CHA_Source_ID field")
    LOG.info("CHA_Source_ID field added and calculated from original OBJECTID")

    arcpy.conversion.FeatureClassToFeatureClass(
        temp_lyr, out_gdb, CHA_DESIGNATION,
    )
    arcpy.management.Delete(temp_lyr)

    print(f"[CHA] Output: {out_fc}")
    LOG.info("CHA feature class created: %s (%d features)", out_fc, count)
    print(f"[CHA] Done: {out_fc} ({count} features)")
    LOG.info("CHA preparation complete: %s", out_fc)
    return out_fc


def main():
    parser = argparse.ArgumentParser(
        description="Download and prepare the Critical Habitat Area dataset.",
    )
    parser.add_argument(
        "--no-overwrite", action="store_true",
        help="Skip download if output already exists (default: always overwrite)",
    )
    parser.add_argument(
        "--source-data", metavar="DIR", default=None,
        help="Directory for downloaded data (default: source_data/)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    try:
        prepare_cha(
            source_data_dir=args.source_data,
            overwrite=not args.no_overwrite,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
