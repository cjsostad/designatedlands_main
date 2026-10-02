"""
create_cha.py — Download and prepare the Critical Habitat Area (CHA) dataset.

Reads the CHA entry from sources_supporting.csv, downloads the archive,
extracts the geodatabase, applies the definition query, erases federal
land from the CHA geometry (per the Point 1 federal-erase change,
2026-10-01), and writes the final feature class into
source_data/cha_exported.gdb/critical_habitat_area_post_fed_erase.

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

Federal-erase pipeline (apply_federal_erase=True, default):
    1. Export the query-filtered CHA to CHA_PRE_ERASE (geometry untouched;
       CHA_Source_ID stamped from the ECCC OBJECTID).
    2. Rename Area_ha -> ECCC_Cha_Area_Ha on the exported FC (ECCC's own
       pre-erase area, preserved for QA).
    3. Read four federal-land mask FCs from designatedlands.gdb:
         - fed_mask_pmbc_federal     (BCGW PMBC parcels, OWNER_TYPE='Federal')
         - fed_mask_indian_reserves  (BCGW CLAB Indian Reserves)
         - fed_mask_national_parks   (BCGW CLAB National Parks)
         - fed_mask_nwa              (ECCC CPCAD 2025 - NWA subset)
       Validate each (exists, >0 features, EPSG:3005). The NWA original
       download is additionally checked for NAD83 datum.
    4. Stamp Fed_Source on each copy, Merge -> FED_SOURCES_MERGED
       (kept for QA), RepairGeometry, PairwiseDissolve -> FED_MASK.
    5. PairwiseErase(CHA_PRE_ERASE, FED_MASK) -> critical_habitat_area_post_fed_erase.
    6. Compute geodesic Cha_Area_Fed_Removed on the final FC
       (hectares; matches the Overlap_Area_Ha method/unit used downstream).
    7. Audit: assert CHA_Source_ID uniqueness, export fully-federal CHA
       to CHA_FULLY_FEDERAL when present, log area totals.

Edition stamp: the NWA mask uses CPCAD edition 2025
(ProtectedConservedArea_2025.gdb / ProtectedConservedArea_2025). The URL,
filename and layer name in sources_supporting.csv all carry the year and
need an annual bump when CPCAD publishes a new release.

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
    python create_cha.py --mask-gdb path/to/designatedlands.gdb
    python create_cha.py --no-federal-erase
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

# CSV lookup key (matches the 'designation' column in sources_supporting.csv).
CHA_DESIGNATION = "critical_habitat_area"
# Final post-federal-erase CHA FC name in cha_exported.gdb. Distinct from the
# CSV designation key so downstream readers aren't confused about which stage
# of the pipeline the FC represents.
CHA_FINAL_FC = "critical_habitat_area_post_fed_erase"
CHA_GDB_NAME = "CriticalHabitat.gdb"          # name as it appears inside the ECCC zip
                                              # and the ONLY name we use locally
LEGACY_GDB_NAME = "CriticalHabitat_eccc_src.gdb"  # legacy name from older runs;
                                                  # detected for migration only
CHA_OUTPUT_GDB = "cha_exported.gdb"            # BC-filtered output GDB

# ---------------------------------------------------------------------------
# Federal exclusion mask (Point 1, 2026-10-01)
# ---------------------------------------------------------------------------
# Four federal-land FCs are read from designatedlands.gdb (downloaded as
# supporting sources) and erased from the CHA geometry before downstream
# overlap analysis. MBS is intentionally NOT in this list — it is a
# regulatory overlay, not federal land ownership.
FED_MASK_SOURCES = [
    ("fed_mask_pmbc_federal",    "PMBC_Federal"),
    ("fed_mask_indian_reserves", "Indian_Reserve"),
    ("fed_mask_national_parks",  "National_Park"),
    ("fed_mask_nwa",             "NWA"),
]

# Intermediate / audit FC names inside cha_exported.gdb (recreated each run).
CHA_PRE_ERASE      = "critical_habitat_area_pre_fed_erase"
FED_SOURCES_MERGED = "federal_exclusion_sources"
FED_MASK           = "federal_exclusion_mask"
CHA_FULLY_FEDERAL  = "cha_fully_federal"

# CPCAD edition-stamped filename — bump annually to match sources_supporting.csv.
NWA_SOURCE_GDB_BASENAME = "ProtectedConservedArea_2025.gdb"


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


def _assert_field_exists(fc, field_name):
    """Raise ValueError listing available fields if *field_name* is missing on *fc*."""
    import arcpy
    names = [f.name for f in arcpy.ListFields(fc)]
    if field_name not in names:
        raise ValueError(
            f"Expected field '{field_name}' not found on {fc}. "
            f"Available fields: {names}"
        )


def _find_nwa_source_gdb(source_data_dir):
    """
    Walk source_data/ looking for the originally-downloaded NWA GDB
    (NWA_SOURCE_GDB_BASENAME, e.g. ProtectedConservedArea_2025.gdb). The
    designatedlands.download_file() helper caches each download under a
    hashed folder name, so the GDB lives one or two levels deep in
    source_data/. We locate it by name rather than by replicating the
    hashing scheme — robust against future cache layout changes.

    Returns the absolute path to the GDB, or None if not found.
    """
    for root, dirs, _files in os.walk(source_data_dir):
        if NWA_SOURCE_GDB_BASENAME in dirs:
            return os.path.join(root, NWA_SOURCE_GDB_BASENAME)
    return None


def _check_pairwise_erase_licensed():
    """
    Raise RuntimeError if the current ArcGIS Pro license does not include
    arcpy.analysis.PairwiseErase. ArcInfo (Advanced) and ArcEditor (Standard)
    both ship PairwiseErase; ArcView (Basic) does not.
    """
    import arcpy
    product = arcpy.ProductInfo()
    if product not in ("ArcInfo", "ArcEditor"):
        raise RuntimeError(
            f"arcpy.ProductInfo() returned '{product}'. "
            f"PairwiseErase requires at least a Standard license (ArcEditor). "
            f"Cannot proceed with the federal-erase step — set "
            f"apply_federal_erase=False explicitly to skip, or install a "
            f"licensed ArcGIS Pro environment."
        )
    LOG.info("ArcGIS Pro license: %s (PairwiseErase available)", product)


def _no_erase_passthrough(out_gdb, pre_erase_fc, final_fc):
    """
    apply_federal_erase=False branch: export CHA_PRE_ERASE straight to the
    final name and populate Cha_Area_Fed_Removed with the geodesic area of
    the (un-erased) polygon, so the output schema is identical to an erased
    run. Downstream tools cannot distinguish schema shape.
    """
    import arcpy
    banner = "FEDERAL ERASE DISABLED"
    print("\n" + "=" * 70)
    print(f"[CHA]  {banner} (apply_federal_erase=False)")
    print("[CHA]  Final CHA retains federal land. Cha_Area_Fed_Removed will")
    print("[CHA]  equal the un-erased polygon area; schema matches erased runs.")
    print("=" * 70 + "\n")
    LOG.warning("%s - final CHA retains federal land", banner)

    arcpy.conversion.FeatureClassToFeatureClass(
        pre_erase_fc, out_gdb, CHA_FINAL_FC,
    )
    arcpy.management.AddField(final_fc, "Cha_Area_Fed_Removed", "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        final_fc,
        [["Cha_Area_Fed_Removed", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )
    LOG.info("No-erase passthrough complete: %s", final_fc)


def _federal_erase_pipeline(out_gdb, pre_erase_fc, federal_mask_gdb,
                            source_data_dir, final_fc):
    """
    B6-B9: build the federal exclusion mask, PairwiseErase it from the
    CHA, add geodesic Cha_Area_Fed_Removed, and emit audit output.

    All intermediate FCs land inside *out_gdb* (cha_exported.gdb, recreated
    each run).
    """
    import arcpy

    _check_pairwise_erase_licensed()

    print("\n[CHA] -- Federal exclusion mask ---------------------------------")
    LOG.info("Building federal exclusion mask from %s", federal_mask_gdb)

    # Validate every mask input, then copy into out_gdb with a Fed_Source label.
    staged_copies = []
    for designation, label in FED_MASK_SOURCES:
        src_path = os.path.join(federal_mask_gdb, designation)
        if not arcpy.Exists(src_path):
            raise RuntimeError(
                f"Federal mask input missing: {src_path}. Expected FC '{designation}' "
                f"in {federal_mask_gdb}. Re-run with SKIP_DOWNLOAD=False so "
                f"DL.download() fetches the supporting sources."
            )
        feat_count = int(arcpy.management.GetCount(src_path)[0])
        if feat_count == 0:
            raise RuntimeError(
                f"Federal mask input '{designation}' has 0 features — refusing to "
                f"proceed (silent under-erase would hide federal land). Inspect "
                f"{src_path} and re-run the download for this layer."
            )
        sr = arcpy.Describe(src_path).spatialReference
        if sr.factoryCode != 3005:
            raise RuntimeError(
                f"Federal mask input '{designation}' is in SR '{sr.name}' "
                f"(factoryCode={sr.factoryCode}), not EPSG:3005. Core Accuracy "
                f"Rule 6 requires BC Albers throughout."
            )
        print(f"[CHA]   {designation:28s}  {feat_count:>8d} features  SR={sr.name}")
        LOG.info("Mask input OK: %s (%d features, SR=%s)",
                 designation, feat_count, sr.name)

        # NWA: additionally inspect the original download for datum sanity.
        if designation == "fed_mask_nwa":
            nwa_src = _find_nwa_source_gdb(source_data_dir)
            if nwa_src is None:
                raise RuntimeError(
                    f"Could not locate original {NWA_SOURCE_GDB_BASENAME} inside "
                    f"{source_data_dir}. download_file() should have cached it; "
                    f"re-run DL.download() for fed_mask_nwa with overwrite=True."
                )
            nwa_layer = os.path.join(nwa_src, os.path.splitext(NWA_SOURCE_GDB_BASENAME)[0])
            if not arcpy.Exists(nwa_layer):
                raise RuntimeError(
                    f"NWA source layer missing inside {nwa_src}. "
                    f"Expected '{os.path.basename(nwa_layer)}'."
                )
            nwa_sr = arcpy.Describe(nwa_layer).spatialReference
            # The GCS attribute names the underlying geographic/datum CRS;
            # NAD83-family values start with "GCS_North_American_1983".
            gcs = nwa_sr.GCS if nwa_sr.GCS is not None else nwa_sr
            datum = getattr(gcs, "datumName", "") or ""
            print(f"[CHA]   NWA original  SR={nwa_sr.name}  datum={datum}")
            LOG.info("NWA original SR: %s, datum: %s", nwa_sr.name, datum)
            # Esri datum strings for NAD83 and its realisations all contain
            # "North_American_1983" (e.g. D_North_American_1983,
            # D_North_American_1983_CSRS, _HARN, _NSRS2007, _2011).
            if "North_American_1983" not in datum:
                raise RuntimeError(
                    f"NWA source datum '{datum}' is not NAD83. CPCAD may have "
                    f"switched reference frames; pick a transformation deliberately "
                    f"before re-enabling the federal erase."
                )

        # Stage copy inside out_gdb with Fed_Source attribute.
        staged_name = f"_fed_mask_src_{label.lower()}"
        staged_path = os.path.join(out_gdb, staged_name)
        arcpy.management.CopyFeatures(src_path, staged_path)
        arcpy.management.AddField(staged_path, "Fed_Source", "TEXT", field_length=32)
        arcpy.management.CalculateField(
            staged_path, "Fed_Source", f"'{label}'", "PYTHON3",
        )
        staged_copies.append(staged_path)

    merged_fc = os.path.join(out_gdb, FED_SOURCES_MERGED)
    arcpy.management.Merge(staged_copies, merged_fc)
    merged_count = int(arcpy.management.GetCount(merged_fc)[0])
    print(f"[CHA]   Merged -> {FED_SOURCES_MERGED} ({merged_count} features, kept for QA)")
    LOG.info("Merged federal mask sources: %d features in %s",
             merged_count, FED_SOURCES_MERGED)

    # Merge keeps a handle on each input; on UNC-path file GDBs this
    # surfaces as "File read/write error" during the following Delete.
    arcpy.management.ClearWorkspaceCache(out_gdb)

    # Staged copies are intermediate QA; cha_exported.gdb is recreated on
    # every prepare_cha() run, so a failed cleanup is non-fatal.
    for staged in staged_copies:
        try:
            arcpy.management.Delete(staged)
        except arcpy.ExecuteError as exc:
            LOG.warning(
                "Could not delete staged mask copy %s (will be removed on "
                "next prepare_cha run): %s",
                os.path.basename(staged), exc,
            )
            print(f"[CHA]   WARN: could not delete {os.path.basename(staged)} "
                  f"(non-fatal; will be cleaned up next run)")

    arcpy.management.RepairGeometry(merged_fc)

    mask_fc = os.path.join(out_gdb, FED_MASK)
    # PairwiseDissolve (Advanced-licensed; the CHA planarization step already
    # requires Advanced so this is not an extra constraint). Produces one row
    # per discrete federal landmass, no attributes carried through.
    arcpy.analysis.PairwiseDissolve(
        merged_fc, mask_fc, dissolve_field=None, multi_part="SINGLE_PART",
    )
    mask_count = int(arcpy.management.GetCount(mask_fc)[0])
    print(f"[CHA]   Dissolved -> {FED_MASK} ({mask_count} single-part polygons)")
    LOG.info("Federal mask dissolved: %d polygons in %s", mask_count, FED_MASK)

    # --- PairwiseErase ----------------------------------------------------
    pre_count = int(arcpy.management.GetCount(pre_erase_fc)[0])
    print(f"\n[CHA] PairwiseErase: {CHA_PRE_ERASE} ({pre_count}) \\ {FED_MASK} -> {CHA_FINAL_FC}")
    LOG.info("PairwiseErase %s (%d features) \\ %s -> %s",
             CHA_PRE_ERASE, pre_count, FED_MASK, CHA_FINAL_FC)

    arcpy.analysis.PairwiseErase(pre_erase_fc, mask_fc, final_fc)

    post_count = int(arcpy.management.GetCount(final_fc)[0])
    print(f"[CHA]   Post-erase feature count: {post_count}")
    LOG.info("Post-erase feature count: %d", post_count)

    # Geodesic area in hectares — same method/unit as Overlap_Area_Ha so the
    # downstream Pct_of_Cha_Prot_by_LandDes numerator and denominator stay consistent.
    arcpy.management.AddField(final_fc, "Cha_Area_Fed_Removed", "DOUBLE")
    arcpy.management.CalculateGeometryAttributes(
        final_fc,
        [["Cha_Area_Fed_Removed", "AREA_GEODESIC"]],
        area_unit="HECTARES",
    )

    # --- Audit (B9) ------------------------------------------------------
    _audit_federal_erase(
        pre_erase_fc=pre_erase_fc,
        final_fc=final_fc,
        out_gdb=out_gdb,
        pre_count=pre_count,
        post_count=post_count,
    )


def _audit_federal_erase(pre_erase_fc, final_fc, out_gdb, pre_count, post_count):
    """Lineage + area audit for the federal erase (B9)."""
    import arcpy

    pre_ids = [r[0] for r in arcpy.da.SearchCursor(pre_erase_fc, ["CHA_Source_ID"])]
    post_ids = [r[0] for r in arcpy.da.SearchCursor(final_fc, ["CHA_Source_ID"])]

    if len(set(pre_ids)) != len(pre_ids):
        raise RuntimeError(
            f"CHA_Source_ID is not unique in {CHA_PRE_ERASE} "
            f"({len(pre_ids)} rows, {len(set(pre_ids))} unique)."
        )
    if len(set(post_ids)) != len(post_ids):
        raise RuntimeError(
            f"CHA_Source_ID is not unique in {CHA_FINAL_FC} "
            f"({len(post_ids)} rows, {len(set(post_ids))} unique) — PairwiseErase "
            f"should preserve one row per surviving source ID."
        )

    fully_removed = sorted(set(pre_ids) - set(post_ids))
    expected_post = pre_count - len(fully_removed)
    if post_count != expected_post:
        raise RuntimeError(
            f"Post-erase count mismatch: expected {expected_post} "
            f"(pre {pre_count} - fully_removed {len(fully_removed)}), got {post_count}."
        )

    print(f"[CHA]   CHA_Source_ID unique in pre- and post-erase: OK")
    print(f"[CHA]   Fully removed by federal mask: {len(fully_removed)} CHA features")
    LOG.info("Fully-federal CHA_Source_IDs removed by erase: %d", len(fully_removed))

    if fully_removed:
        LOG.info("Fully-removed CHA_Source_IDs: %s", fully_removed)
        print(f"[CHA]   Exporting fully-federal CHA to {CHA_FULLY_FEDERAL}")
        temp_lyr = "cha_fully_federal_lyr"
        if arcpy.Exists(temp_lyr):
            arcpy.management.Delete(temp_lyr)
        id_list = ",".join(str(i) for i in fully_removed)
        where = f"CHA_Source_ID IN ({id_list})"
        arcpy.management.MakeFeatureLayer(pre_erase_fc, temp_lyr, where)
        arcpy.conversion.FeatureClassToFeatureClass(
            temp_lyr, out_gdb, CHA_FULLY_FEDERAL,
        )
        arcpy.management.Delete(temp_lyr)
    else:
        print(f"[CHA]   No CHA is wholly on federal land; "
              f"{CHA_FULLY_FEDERAL} not created.")

    # Area totals
    pre_total = sum(
        (r[0] or 0.0) for r in
        arcpy.da.SearchCursor(pre_erase_fc, ["ECCC_Cha_Area_Ha"])
    )
    post_total = sum(
        (r[0] or 0.0) for r in
        arcpy.da.SearchCursor(final_fc, ["Cha_Area_Fed_Removed"])
    )
    delta = pre_total - post_total
    print(f"[CHA]   Σ ECCC_Cha_Area_Ha (pre):      {pre_total:>18,.2f} ha")
    print(f"[CHA]   Σ Cha_Area_Fed_Removed (post): {post_total:>18,.2f} ha")
    print(f"[CHA]   Area removed by federal erase: {delta:>18,.2f} ha")
    LOG.info("Area pre=%.2f ha, post=%.2f ha, delta=%.2f ha",
             pre_total, post_total, delta)

    # Geodesic vs ECCC method differences can push post > pre on individual
    # rows; log, do not raise.
    rows_post_gt_pre = 0
    max_ratio = 0.0
    pre_area_by_id = {}
    with arcpy.da.SearchCursor(pre_erase_fc, ["CHA_Source_ID", "ECCC_Cha_Area_Ha"]) as cur:
        for cid, area in cur:
            pre_area_by_id[cid] = area or 0.0
    with arcpy.da.SearchCursor(final_fc, ["CHA_Source_ID", "Cha_Area_Fed_Removed"]) as cur:
        for cid, post_area in cur:
            pre_area = pre_area_by_id.get(cid, 0.0)
            if post_area is None or pre_area <= 0:
                continue
            if post_area > pre_area:
                rows_post_gt_pre += 1
                ratio = post_area / pre_area
                if ratio > max_ratio:
                    max_ratio = ratio
    if rows_post_gt_pre:
        msg = (f"{rows_post_gt_pre} features have Cha_Area_Fed_Removed > "
               f"ECCC_Cha_Area_Ha (max ratio {max_ratio:.4f}). Expected: ECCC "
               f"and AREA_GEODESIC use different methods; this is informational, "
               f"not an error.")
        print(f"[CHA]   {msg}")
        LOG.info(msg)


def prepare_cha(source_data_dir=None, overwrite=True, csv_path=None,
                query_override=None, federal_mask_gdb=None,
                apply_federal_erase=True):
    """
    Download the CHA archive, extract it, apply the definition query,
    erase federal land from the geometry, and write the final feature
    class into source_data/cha_exported.gdb/critical_habitat_area_post_fed_erase.

    Parameters
    ----------
    source_data_dir : str or None
        Target directory for downloads and the output GDB.
    overwrite : bool
        If False and the final FC already exists, return it without rebuilding.
    csv_path : str or None
        Path to sources_supporting.csv (defaults to the one next to this file).
    query_override : str or None
        If provided, overrides the ``query`` column from the CSV.
    federal_mask_gdb : str or None
        Path to the GDB holding the four fed_mask_* FCs (typically
        designatedlands.gdb). Required when apply_federal_erase=True.
    apply_federal_erase : bool
        True (default) runs the federal-land erase. False skips the erase
        but still writes an output with the identical schema so downstream
        tools remain compatible.

    Returns
    -------
    str
        Path to the output feature class in source_data/cha_exported.gdb.
    """
    import arcpy

    if apply_federal_erase and federal_mask_gdb is None:
        raise ValueError(
            "apply_federal_erase=True requires federal_mask_gdb (path to the "
            "GDB containing fed_mask_pmbc_federal, fed_mask_indian_reserves, "
            "fed_mask_national_parks, fed_mask_nwa). Pass designatedlands.gdb "
            "or set apply_federal_erase=False explicitly."
        )

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
    out_fc = os.path.join(out_gdb, CHA_FINAL_FC)

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

    # Enforce BC Albers for every geoprocessing call in this function so
    # standalone runs match pipeline runs (DesignatedLands.__init__ sets this
    # in the pipeline, but the standalone CLI does not). Core Accuracy Rule 6.
    with arcpy.EnvManager(outputCoordinateSystem=arcpy.SpatialReference(3005)):

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

        # Export to pre-erase name (Point 1 schema change, 2026-10-01).
        pre_erase_fc = os.path.join(out_gdb, CHA_PRE_ERASE)
        arcpy.conversion.FeatureClassToFeatureClass(
            temp_lyr, out_gdb, CHA_PRE_ERASE,
        )
        arcpy.management.Delete(temp_lyr)

        # Schema edits must land on the EXPORTED copy only — never on temp_lyr
        # (which writes through to CriticalHabitat.gdb and corrupts the native
        # Area_ha column for every subsequent run).
        _assert_field_exists(pre_erase_fc, "Area_ha")
        arcpy.management.AlterField(
            pre_erase_fc, "Area_ha",
            "ECCC_Cha_Area_Ha", "ECCC_Cha_Area_Ha",
        )
        LOG.info("Renamed Area_ha -> ECCC_Cha_Area_Ha on %s", CHA_PRE_ERASE)

        if apply_federal_erase:
            _federal_erase_pipeline(
                out_gdb=out_gdb,
                pre_erase_fc=pre_erase_fc,
                federal_mask_gdb=federal_mask_gdb,
                source_data_dir=source_data_dir,
                final_fc=out_fc,
            )
        else:
            _no_erase_passthrough(
                out_gdb=out_gdb,
                pre_erase_fc=pre_erase_fc,
                final_fc=out_fc,
            )

    print(f"[CHA] Output: {out_fc}")
    LOG.info("CHA feature class created: %s (%d features)", out_fc, count)
    print(f"[CHA] Done: {out_fc} ({count} input features)")
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
        "--mask-gdb", metavar="GDB", default=None,
        help="Path to the GDB containing fed_mask_* FCs (default: "
             "<script_dir>/designatedlands.gdb). Required unless "
             "--no-federal-erase is set.",
    )
    parser.add_argument(
        "--no-federal-erase", action="store_true",
        help="Skip the federal-land erase step. Output schema is still "
             "identical to an erased run (Cha_Area_Fed_Removed is populated "
             "with the un-erased geodesic area).",
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

    apply_federal_erase = not args.no_federal_erase
    if args.mask_gdb is not None:
        mask_gdb = args.mask_gdb
    else:
        mask_gdb = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "designatedlands.gdb",
        )

    try:
        prepare_cha(
            source_data_dir=args.source_data,
            overwrite=not args.no_overwrite,
            federal_mask_gdb=mask_gdb if apply_federal_erase else None,
            apply_federal_erase=apply_federal_erase,
        )
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
