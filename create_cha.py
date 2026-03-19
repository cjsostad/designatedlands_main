"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, and writes the
filtered feature class into source_data/.

Download behaviour:
  - Attempts to download CriticalHabitat.zip up to 3 times.
  - On success, extracts CriticalHabitat.gdb directly into source_data/
    (no hash folders).
  - If all download attempts fail, falls back to an existing
    source_data/CriticalHabitat.gdb if one is present.

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
import zipfile

LOG = logging.getLogger(__name__)

CHA_DESIGNATION = "critical_habitat_area"
CHA_GDB_NAME = "CriticalHabitat.gdb"
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 10


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


def _download_cha_zip(url, dest_dir):
    """
    Download the CHA zip from *url* with retries, extract CriticalHabitat.gdb
    directly into *dest_dir* (no hash folders).

    Returns the path to the extracted GDB, or None if all attempts failed.
    """
    import requests

    src_gdb_path = os.path.join(dest_dir, CHA_GDB_NAME)

    for attempt in range(1, MAX_RETRIES + 1):
        print(f"[CHA] Download attempt {attempt}/{MAX_RETRIES}...")
        try:
            resp = requests.get(
                url, stream=True, verify=False, timeout=(30, 300)
            )
            resp.raise_for_status()

            tmp = tempfile.NamedTemporaryFile(
                "wb", suffix=".zip", delete=False, dir=dest_dir
            )
            try:
                for chunk in resp.iter_content(chunk_size=8192):
                    tmp.write(chunk)
                tmp.close()
            except Exception:
                tmp.close()
                os.unlink(tmp.name)
                raise

            # Remove old GDB before extracting
            if os.path.exists(src_gdb_path):
                print(f"[CHA] Removing old {CHA_GDB_NAME}...")
                shutil.rmtree(src_gdb_path)

            print(f"[CHA] Extracting {CHA_GDB_NAME} into {dest_dir}...")
            with zipfile.ZipFile(tmp.name, "r") as zf:
                zf.extractall(dest_dir)
            os.unlink(tmp.name)

            # The zip may nest the GDB inside a subfolder — find it
            if os.path.exists(src_gdb_path):
                print(f"[CHA] Extracted: {src_gdb_path}")
                return src_gdb_path

            # Search for it if nested
            for root, dirs, _files in os.walk(dest_dir):
                if CHA_GDB_NAME in dirs:
                    nested = os.path.join(root, CHA_GDB_NAME)
                    if nested != src_gdb_path:
                        shutil.move(nested, src_gdb_path)
                        print(f"[CHA] Moved nested GDB to: {src_gdb_path}")
                    return src_gdb_path

            print(f"[CHA] WARNING: Zip extracted but {CHA_GDB_NAME} not found")
            return None

        except Exception as exc:
            print(f"[CHA] Attempt {attempt} failed: {exc}")
            if attempt < MAX_RETRIES:
                print(f"[CHA] Retrying in {RETRY_DELAY_SECONDS} seconds...")
                time.sleep(RETRY_DELAY_SECONDS)

    print(f"[CHA] All {MAX_RETRIES} download attempts failed.")
    return None


def prepare_cha(source_data_dir=None, overwrite=True, csv_path=None):
    """
    Download the CHA archive, extract it, apply the definition query,
    and write the filtered feature class into source_data/.

    If the download fails after retries, falls back to an existing
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

    print(f"[CHA] Downloading {cfg['url']}...")
    downloaded = _download_cha_zip(cfg["url"], source_data_dir)

    if downloaded:
        src_gdb = downloaded
    elif os.path.exists(src_gdb_path):
        print(f"[CHA] Falling back to existing {src_gdb_path}")
        src_gdb = src_gdb_path
    else:
        raise RuntimeError(
            f"Download failed and no existing {CHA_GDB_NAME} found in "
            f"{source_data_dir}. Download the zip manually, unzip "
            f"{CHA_GDB_NAME} into {source_data_dir}, and re-run."
        )

    print(f"[CHA] Using source GDB: {src_gdb}")

    # Determine layer name
    layer = cfg["layer_in_file"] or None
    if layer:
        src = os.path.join(src_gdb, layer)
    else:
        # Auto-detect first feature class
        prev_ws = arcpy.env.workspace
        arcpy.env.workspace = src_gdb
        fcs = arcpy.ListFeatureClasses() or []
        arcpy.env.workspace = prev_ws
        if not fcs:
            raise ValueError(f"No feature classes found in {src_gdb}")
        src = os.path.join(src_gdb, fcs[0])
        layer = fcs[0]
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
    sql_where = cfg["query"].strip('"') or ""
    print(f"[CHA] Applying definition query and exporting...")
    LOG.info("CHA query: %s", sql_where)

    temp_lyr = "cha_temp_lyr"
    if arcpy.Exists(temp_lyr):
        arcpy.management.Delete(temp_lyr)

    arcpy.management.MakeFeatureLayer(src, temp_lyr, sql_where)

    count = int(arcpy.management.GetCount(temp_lyr)[0])
    print(f"[CHA] {count} features matched the definition query")

    arcpy.conversion.FeatureClassToFeatureClass(
        temp_lyr, out_gdb, CHA_DESIGNATION,
    )
    arcpy.management.Delete(temp_lyr)

    print(f"[CHA] Output: {out_fc}")
    LOG.info("CHA feature class created: %s (%d features)", out_fc, count)
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
