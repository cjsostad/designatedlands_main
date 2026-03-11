"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, and writes the
filtered feature class into source_data/.

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

LOG = logging.getLogger(__name__)

CHA_DESIGNATION = "critical_habitat_area"


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


def _find_gdb(extract_dir, gdb_name):
    """
    Recursively search *extract_dir* for a folder named *gdb_name*.

    download_file() extracts archives into a hash-named folder, but the
    GDB may be nested inside one or more subdirectories within the zip.
    """
    for root, dirs, _files in os.walk(extract_dir):
        if gdb_name in dirs:
            return os.path.join(root, gdb_name)
    raise FileNotFoundError(
        f"Could not find '{gdb_name}' anywhere inside {extract_dir}"
    )


def prepare_cha(source_data_dir=None, overwrite=True, csv_path=None):
    """
    Download the CHA archive, extract it, apply the definition query,
    and write the filtered feature class into source_data/.

    Parameters
    ----------
    source_data_dir : str or None
        Directory for downloaded/extracted data. Defaults to source_data/
        next to this script.
    overwrite : bool
        If True (default), re-download and rebuild to capture new CHA
        polygons. Set False to skip if the output already exists.
    csv_path : str or None
        Path to sources_supporting.csv. Defaults to the file next to this
        script.

    Returns
    -------
    str
        Path to the output feature class in source_data/.
    """
    import arcpy
    from designatedlands import download_file

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

    # Always re-download — the federal government may have posted
    # an updated CHA file with new polygons
    print(f"[CHA] Downloading {cfg['url']}...")
    _local_file, extract_dir = download_file(
        url=cfg["url"],
        path=source_data_dir,
        filename=cfg["file_in_url"],
        overwrite=True,
    )

    # Locate the GDB — the zip may nest it inside subdirectories
    gdb_name = cfg["file_in_url"]  # e.g. "CriticalHabitat.gdb"
    src_gdb = _find_gdb(extract_dir, gdb_name)
    print(f"[CHA] Found source GDB: {src_gdb}")

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
