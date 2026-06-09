"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, and writes the
filtered feature class into source_data/.

Download behaviour:
    - Attempts to download CriticalHabitat.zip from ECCC's data portal.
    - Extracts the zip into a TEMP sibling directory, validates the
        extracted geodatabase actually contains feature classes, then
        atomically moves it into source_data/CriticalHabitat.gdb.
    - The GDB is stored under ONE name only — CriticalHabitat.gdb. No
        rename to a second filename. This eliminates the WinError 183
        failure mode where a stale empty GDB would block a fresh download.
    - If the download fails, falls back to an existing
        source_data/CriticalHabitat.gdb only if it contains feature
        classes.
    - If neither succeeds, raises an error with manual download instructions.

Manual download fallback (e.g. if behind a firewall):
    1. Download CriticalHabitat.zip from the URL in sources_supporting.csv
       (or directly from https://data-donnees.az.ec.gc.ca)
    2. Extract CriticalHabitat.gdb from the zip
    3. Place CriticalHabitat.gdb directly inside the source_data/ directory
       (no rename needed — the pipeline uses this exact name)
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
CHA_GDB_NAME = "CriticalHabitat.gdb"          # name as it appears inside the ECCC zip
                                              # and the ONLY name we use locally
LEGACY_GDB_NAME = "CriticalHabitat_eccc_src.gdb"  # legacy name from older runs;
                                                  # detected for migration only
CHA_OUTPUT_GDB = "cha_exported.gdb"            # BC-filtered output GDB


def _rmtree_robust(path):
    """
    Delete a directory tree, retrying once with permission fix-ups for any
    read-only files. Returns True if the directory is gone afterwards.
    """
    if not os.path.exists(path):
        return True

    def _on_rm_error(func, p, exc_info):
        try:
            import stat
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    try:
        shutil.rmtree(path, onerror=_on_rm_error)
    except Exception as exc:
        print(f"[CHA] Could not remove {path}: {exc}")
        return False
    return not os.path.exists(path)


def _list_feature_classes(gdb_path):
    """
    Return a list of feature class names inside *gdb_path* using arcpy.
    Empty list if the GDB exists but has no feature classes, or if it
    cannot be opened.

    NOTE: opening a file GDB with arcpy creates schema lock files
    (*.sr.lock) inside the GDB folder. Do NOT call this on a GDB that
    you plan to move/rename immediately afterwards \u2014 use
    _looks_like_valid_gdb() for that instead.
    """
    import arcpy
    if not os.path.exists(gdb_path):
        return []
    prev = arcpy.env.workspace
    try:
        arcpy.env.workspace = gdb_path
        return list(arcpy.ListFeatureClasses() or [])
    except Exception:
        return []
    finally:
        arcpy.env.workspace = prev


def _looks_like_valid_gdb(gdb_path):
    """
    Pure-filesystem check that *gdb_path* is a non-empty file
    geodatabase. Returns True if the folder exists and contains at
    least one .gdbtable file (the on-disk container for a dataset).

    Used to validate a freshly extracted GDB BEFORE it is moved into
    place, without touching arcpy (which would create schema lock
    files that block the move).
    """
    if not os.path.isdir(gdb_path):
        return False
    try:
        for name in os.listdir(gdb_path):
            if name.lower().endswith(".gdbtable"):
                return True
    except OSError:
        return False
    return False


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
    Download the CHA zip from *url* and extract CriticalHabitat.gdb into
    *dest_dir*.

    Strategy (deliberately simple, no two-name renames):
        1. Download zip to a temp file inside *dest_dir*.
        2. Extract zip into a fresh sibling temp directory.
        3. Locate CriticalHabitat.gdb inside the extraction (may be nested).
        4. Validate the extracted GDB actually has feature classes.
        5. Robustly remove any existing dest_dir/CriticalHabitat.gdb.
        6. Move the validated GDB into dest_dir/CriticalHabitat.gdb.
        7. Clean up temp artifacts.

    The function NEVER renames the GDB to a second name. The GDB is
    stored under one canonical name (CHA_GDB_NAME) for the lifetime of
    the project. This eliminates the WinError 183 / "file already exists"
    failure mode where a stale empty GDB blocks a successful download.

    Parameters
    ----------
    url : str
    dest_dir : str
    max_retries : int
    timeout : int

    Returns
    -------
    str or None
        Path to the final extracted GDB, or None on any failure.
    """
    final_gdb_path = os.path.join(dest_dir, CHA_GDB_NAME)

    for attempt in range(1, max_retries + 1):
        temp_zip_path = None
        extract_dir = tempfile.mkdtemp(prefix="cha_extract_", dir=dest_dir)

        try:
            print(f"[CHA] Download attempt {attempt}/{max_retries} from {url}...")

            with tempfile.NamedTemporaryFile(
                "wb", suffix=".zip", delete=False, dir=dest_dir
            ) as temp_file:
                temp_zip_path = temp_file.name

            request = Request(url)
            request.add_header("User-Agent", "Mozilla/5.0")

            with urlopen(request, timeout=timeout) as response:
                total_size = response.headers.get("Content-Length")
                if total_size:
                    total_size = int(total_size)
                    print(f"[CHA] File size: {total_size / (1024 * 1024):.1f} MB")

                downloaded = 0
                chunk_size = 1024 * 1024  # 1 MB
                with open(temp_zip_path, "wb") as out_f:
                    while True:
                        chunk = response.read(chunk_size)
                        if not chunk:
                            break
                        out_f.write(chunk)
                        downloaded += len(chunk)
                        if total_size:
                            pct = (downloaded / total_size) * 100
                            print(
                                f"[CHA] Progress: {pct:.1f}% "
                                f"({downloaded / (1024 * 1024):.1f} MB)",
                                end="\r",
                            )
                if total_size:
                    print()

            print(f"[CHA] Download complete. Extracting into temp dir...")
            with zipfile.ZipFile(temp_zip_path, "r") as zf:
                zf.extractall(extract_dir)

            # Locate the GDB inside the extraction (may be nested one
            # level deep).
            extracted_gdb = None
            for root, dirs, _files in os.walk(extract_dir):
                if CHA_GDB_NAME in dirs:
                    extracted_gdb = os.path.join(root, CHA_GDB_NAME)
                    break

            if extracted_gdb is None:
                print(f"[CHA] WARNING: {CHA_GDB_NAME} not found in zip")
                return None

            # Validate: filesystem-only check (do NOT use arcpy here \u2014
            # opening the GDB would create *.sr.lock files inside the
            # extracted folder, and those locks would then block the
            # shutil.move() with "Permission denied").
            if not _looks_like_valid_gdb(extracted_gdb):
                print(
                    f"[CHA] WARNING: Extracted {CHA_GDB_NAME} looks empty "
                    f"(no .gdbtable files); treating as failed download"
                )
                return None
            print(f"[CHA] Extracted GDB validated (contains .gdbtable files)")

            # Robustly remove any existing copy at the final location
            # BEFORE the move. If we cannot delete it, fail the download
            # cleanly so the caller can fall back to whatever is there.
            if os.path.exists(final_gdb_path):
                print(f"[CHA] Removing previous {final_gdb_path}...")
                if not _rmtree_robust(final_gdb_path):
                    print(
                        f"[CHA] ERROR: Could not remove existing "
                        f"{final_gdb_path} (likely locked by ArcGIS Pro). "
                        f"Close any open project and re-run."
                    )
                    return None

            shutil.move(extracted_gdb, final_gdb_path)
            print(f"[CHA] Installed: {final_gdb_path}")
            return final_gdb_path

        except URLError as exc:
            if attempt < max_retries:
                wait = 2 ** attempt
                print(f"[CHA] Download failed: {exc}; retrying in {wait}s")
                time.sleep(wait)
                continue
            print(f"[CHA] Download failed after {max_retries} attempts: {exc}")
            return None
        except Exception as exc:
            print(f"[CHA] Download failed: {exc}")
            return None
        finally:
            if temp_zip_path and os.path.exists(temp_zip_path):
                try:
                    os.unlink(temp_zip_path)
                except Exception:
                    pass
            # Best-effort cleanup of the extraction temp dir
            _rmtree_robust(extract_dir)

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
    out_gdb = os.path.join(source_data_dir, CHA_OUTPUT_GDB)
    out_fc = os.path.join(out_gdb, CHA_DESIGNATION)

    if arcpy.Exists(out_fc) and not overwrite:
        LOG.info("CHA feature class already exists: %s — skipping", out_fc)
        print(f"[CHA] Already exists: {out_fc} (use overwrite=True to rebuild)")
        return out_fc

    # ------------------------------------------------------------------
    # Source GDB resolution — ONE canonical name (CriticalHabitat.gdb).
    # ------------------------------------------------------------------
    canonical_gdb = os.path.join(source_data_dir, CHA_GDB_NAME)
    legacy_gdb = os.path.join(source_data_dir, LEGACY_GDB_NAME)

    # One-time migration: if the legacy name is the only thing with data,
    # promote it to the canonical name so future runs are predictable.
    if (not os.path.exists(canonical_gdb)
            and os.path.exists(legacy_gdb)
            and _list_feature_classes(legacy_gdb)):
        print(f"[CHA] Migrating legacy GDB: {legacy_gdb} -> {canonical_gdb}")
        try:
            shutil.move(legacy_gdb, canonical_gdb)
        except Exception as exc:
            print(f"[CHA] Migration failed ({exc}); will try a fresh download")

    # Clean up any stale empty legacy GDB so it can never be picked up as
    # a fallback again.
    if os.path.exists(legacy_gdb) and not _list_feature_classes(legacy_gdb):
        print(f"[CHA] Removing stale empty legacy GDB: {legacy_gdb}")
        _rmtree_robust(legacy_gdb)

    # Attempt fresh download (validated inside _download_cha_zip).
    downloaded = _download_cha_zip(cfg["url"], source_data_dir)

    if downloaded:
        src_gdb = downloaded
    elif os.path.exists(canonical_gdb) and _list_feature_classes(canonical_gdb):
        print(f"[CHA] Falling back to existing {canonical_gdb}")
        src_gdb = canonical_gdb
    else:
        raise RuntimeError(
            f"Download failed and no usable {CHA_GDB_NAME} found in "
            f"{source_data_dir}.\n\n"
            f"To proceed manually:\n"
            f"  1. Download the zip from:\n"
            f"     {cfg['url']}\n"
            f"  2. Extract {CHA_GDB_NAME} from the zip\n"
            f"  3. Place {CHA_GDB_NAME} directly inside the source_data/ folder\n"
            f"     (no rename needed — the pipeline uses this exact name)\n"
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

    # Create or recreate output GDB. arcpy.management.Delete reports
    # "Succeeded" even when the GDB folder is still on disk (e.g. when
    # OneDrive sync holds a lock), which then makes CreateFileGDB fail
    # with ERROR 000258. Belt-and-suspenders: try arcpy first, then
    # force-remove the folder at the OS level if it survived.
    if arcpy.Exists(out_gdb):
        print(f"[CHA] Deleting existing output GDB...")
        try:
            arcpy.management.Delete(out_gdb)
        except Exception as exc:
            print(f"[CHA] arcpy.Delete on {out_gdb} failed: {exc}")
    if os.path.exists(out_gdb):
        if not _rmtree_robust(out_gdb):
            raise RuntimeError(
                f"Could not remove existing output GDB {out_gdb}. "
                f"Close any open ArcGIS Pro project that references it "
                f"(or pause OneDrive sync) and re-run."
            )
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
