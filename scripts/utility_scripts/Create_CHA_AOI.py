"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, filters by AOI
(for testing), and writes the filtered feature class into source_data/.
"""

import argparse
import csv
import logging
import os
import sys

LOG = logging.getLogger(__name__)

CHA_DESIGNATION = "critical_habitat_area"


def _find_cha_config(csv_path=None):

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

    for root, dirs, _files in os.walk(extract_dir):
        if gdb_name in dirs:
            return os.path.join(root, gdb_name)

    raise FileNotFoundError(
        f"Could not find '{gdb_name}' anywhere inside {extract_dir}"
    )


def prepare_cha(source_data_dir=None, overwrite=True, csv_path=None):

    import arcpy
    from designatedlands import download_file

    # --------------------------------
    # AOI FOR TEST RUN
    # CHANGE THIS PATH
    # --------------------------------
    aoi = r"\\spatialfiles.bcgov\srm\gss\projects\gr_2025_1236_critical_habitat_protection\source_data\South Okanagan\South_Okanagan.shp"

    cfg = _find_cha_config(csv_path)

    if not cfg["url"]:
        raise ValueError("CHA row in sources_supporting.csv has no URL")

    if source_data_dir is None:
        source_data_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "source_data",
        )

    os.makedirs(source_data_dir, exist_ok=True)

    out_gdb = os.path.join(source_data_dir, "cha_exported.gdb")
    out_fc = os.path.join(out_gdb, CHA_DESIGNATION)

    if arcpy.Exists(out_fc) and not overwrite:
        LOG.info("CHA feature class already exists: %s — skipping", out_fc)
        print(f"[CHA] Already exists: {out_fc}")
        return out_fc

    print(f"[CHA] Downloading {cfg['url']}...")

    _local_file, extract_dir = download_file(
        url=cfg["url"],
        path=source_data_dir,
        filename=cfg["file_in_url"],
        overwrite=True,
    )

    gdb_name = cfg["file_in_url"]
    src_gdb = _find_gdb(extract_dir, gdb_name)

    print(f"[CHA] Found source GDB: {src_gdb}")

    layer = cfg["layer_in_file"] or None

    if layer:
        src = os.path.join(src_gdb, layer)

    else:
        prev_ws = arcpy.env.workspace
        arcpy.env.workspace = src_gdb
        fcs = arcpy.ListFeatureClasses() or []
        arcpy.env.workspace = prev_ws

        if not fcs:
            raise ValueError(f"No feature classes found in {src_gdb}")

        src = os.path.join(src_gdb, fcs[0])
        layer = fcs[0]

        print(f"[CHA] Auto-detected layer: {layer}")

    if arcpy.Exists(out_gdb):
        print("[CHA] Deleting existing output GDB...")
        arcpy.management.Delete(out_gdb)

    print(f"[CHA] Creating output GDB: {out_gdb}")

    arcpy.management.CreateFileGDB(
        os.path.dirname(out_gdb),
        os.path.basename(out_gdb),
    )

    sql_where = cfg["query"].strip('"') or ""

    print("[CHA] Applying definition query...")

    temp_lyr = "cha_temp_lyr"

    if arcpy.Exists(temp_lyr):
        arcpy.management.Delete(temp_lyr)

    arcpy.management.MakeFeatureLayer(src, temp_lyr, sql_where)

    # --------------------------------
    # AOI FILTER
    # --------------------------------
    if arcpy.Exists(aoi):

        print(f"[CHA] Applying AOI spatial filter: {aoi}")

        arcpy.management.SelectLayerByLocation(
            temp_lyr,
            "INTERSECT",
            aoi
        )

    else:
        print("[CHA] WARNING: AOI not found, continuing without filter")

    count = int(arcpy.management.GetCount(temp_lyr)[0])

    print(f"[CHA] {count} features remain after filtering")

    # Stamp the original ECCC OBJECTID into a regular attribute field so it
    # survives FeatureClassToFeatureClass OID re-numbering and flows through
    # PairwiseIntersect into all output tables. This lets users join
    # CHA_Source_ID back to the national CriticalHabitat.gdb on OBJECTID
    # without needing the locally-filtered intermediate copy. (2026-05-27)
    arcpy.management.AddField(temp_lyr, "CHA_Source_ID", "LONG")
    arcpy.management.CalculateField(temp_lyr, "CHA_Source_ID", "!OBJECTID!", "PYTHON3")
    print("[CHA] Stamped original ECCC OBJECTID into CHA_Source_ID field")

    arcpy.conversion.FeatureClassToFeatureClass(
        temp_lyr,
        out_gdb,
        CHA_DESIGNATION,
    )

    arcpy.management.Delete(temp_lyr)

    print(f"[CHA] Output created:")
    print(out_fc)

    LOG.info(
        "CHA feature class created: %s (%d features)",
        out_fc,
        count
    )

    return out_fc


def main():

    parser = argparse.ArgumentParser(
        description="Download and prepare the Critical Habitat Area dataset.",
    )

    parser.add_argument(
        "--no-overwrite",
        action="store_true",
        help="Skip download if output already exists",
    )

    parser.add_argument(
        "--source-data",
        metavar="DIR",
        default=None,
        help="Directory for downloaded data",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
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