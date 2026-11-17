"""
html_report.py — Builds a self-contained HTML dashboard alongside the xlsx report.

This module performs NO geoprocessing, ArcPy, or compute. Every input is one of
the objects write_report_xlsx already receives; the dashboard is just a second
rendering of the same assembled data. Standard library only.

The dashboard HTML lives in dashboard_template.html (repo root). That file is a
complete, self-contained page containing the literal placeholder
``__CHA_DASHBOARD_DATA__`` where a single JSON blob is injected at write time.
"""

import csv
import json
import os
from collections import defaultdict


def _load_template():
    tpl_path = os.path.join(os.path.dirname(__file__), "dashboard_template.html")
    with open(tpl_path, encoding="utf-8") as fh:
        return fh.read()


def _to_float(val):
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _categorize_counts():
    """Count non-excluded designations per category.

    Reuses the shared _categorize logic and _find_csv locator from date_filter
    so the dashboard's category mix matches the xlsx "Designation Categories"
    sheet exactly (no duplicated classification logic).
    """
    from date_filter import _categorize, _find_csv

    counts = {
        "PPA": 0,
        "Protected Other": 0,
        "Exclude 1_2 Activities": 0,
        "Managed": 0,
    }
    csv_path = _find_csv()
    with open(csv_path, encoding="utf-8-sig") as csvf:
        for row in csv.DictReader(csvf):
            if row.get("exclude", "").strip().upper() == "T":
                continue
            fr = row.get("forest_restriction", "").strip()
            og = row.get("og_restriction", "").strip()
            mr = row.get("mine_restriction", "").strip()
            cat = _categorize(fr, og, mr)
            if cat in counts:
                counts[cat] += 1
    return counts


def write_report_html(change_rows, excluded_entries, summary, out_path,
                      federal_excluded=None, pipeline_options=None):
    """Write the HTML dashboard next to the xlsx report.

    Degrades gracefully on partial runs and never raises: the xlsx is the system
    of record, so any failure here logs a warning and returns None.

    Returns
    -------
    str or None
        The path written, or None if generation failed.
    """
    try:
        opts = pipeline_options or {}
        template = _load_template()

        data = {}

        # --- run metadata ---
        data["meta"] = {
            "run_ts": opts.get("run_timestamp", ""),
            "win_start": summary.get("start_date", ""),
            "win_end": summary.get("end_date", ""),
            "exclude_federal": bool(opts.get("exclude_federal")),
            "date_filter_active": bool(opts.get("date_filter_active")),
        }

        # --- per-species gap: aggregate the CHA unprotected summary rows ---
        cha_rows = opts.get("cha_summary_rows") or []
        field_names = opts.get("cha_summary_field_names") or []
        required = [
            "ECCC_Cha_Area_Ha",
            "Cha_Area_Fed_Removed",
            "Prov_Unprotect_Cha_Area",
        ]
        have_fields = all(name in field_names for name in required)

        total_fed_removed = 0.0   # A
        total_unprotected = 0.0   # U
        species_list = []
        species_count = 0

        if cha_rows and have_fields:
            groups = defaultdict(
                lambda: {"eccc": 0.0, "fed": 0.0, "unprot": 0.0, "n": 0}
            )
            for row in cha_rows:
                name = row.get("CommName_E") or row.get("Common Name")
                key = name if name else "Unknown"
                grp = groups[key]
                grp["eccc"] += _to_float(row.get("ECCC_Cha_Area_Ha"))
                grp["fed"] += _to_float(row.get("Cha_Area_Fed_Removed"))
                grp["unprot"] += _to_float(row.get("Prov_Unprotect_Cha_Area"))
                grp["n"] += 1

            for name, grp in groups.items():
                fed = grp["fed"]
                unprot = grp["unprot"]
                total_fed_removed += fed
                total_unprotected += unprot
                pct = round(100 * (fed - unprot) / fed, 1) if fed else 0
                species_list.append({
                    "name": name,
                    "n": grp["n"],
                    "assessed": round(fed),
                    "unprotected": round(unprot),
                    "protected": round(max(fed - unprot, 0)),
                    "pct": pct,
                    "eccc": round(grp["eccc"]),
                })

            species_list.sort(key=lambda s: s["assessed"], reverse=True)
            species_count = len(groups)

        data["species"] = species_list

        # --- KPIs ---
        A = total_fed_removed
        U = total_unprotected
        data["kpi"] = {
            "pct": round(100 * (A - U) / A, 1) if A else 0,
            "assessed": round(A),
            "protected": round(max(A - U, 0)),
            "datasets": summary.get("total_datasets", 0),
            "date_filtered": summary.get("date_filtered", 0),
            "changed": summary.get("layers_with_changes", 0),
            "no_change": summary.get("layers_no_changes", 0),
            "excluded": summary.get("excluded", 0),
            "features_changed": summary.get("total_features_changed", 0),
            "cha_polys": len(opts.get("cha_summary_rows") or []),
            "species": species_count,
        }

        # --- changes by layer ---
        change_counts = defaultdict(int)
        for row in (change_rows or []):
            change_counts[row.get("layer_name", "")] += 1
        changes = [
            {"layer": layer, "features": count}
            for layer, count in change_counts.items()
        ]
        changes.sort(key=lambda c: c["features"], reverse=True)
        data["changes"] = changes

        # --- designation categories (fixed order) ---
        cat_counts = _categorize_counts()
        data["categories"] = [
            {"cat": c, "n": cat_counts.get(c, 0)}
            for c in [
                "PPA",
                "Protected Other",
                "Exclude 1_2 Activities",
                "Managed",
            ]
        ]

        # --- excluded layers & truncation flag ---
        data["excluded"] = [
            [e["name"], e.get("exclude_reason") or "unknown"]
            for e in (excluded_entries or [])
        ]
        data["truncated"] = bool(
            opts.get("cha_planarized_truncated")
            or opts.get("cha_overlapping_truncated")
        )

        blob = json.dumps(data, ensure_ascii=False)
        html = template.replace("__CHA_DASHBOARD_DATA__", blob, 1)

        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        return out_path
    except Exception as exc:
        print(f"  WARNING: HTML dashboard generation failed: {exc}")
        return None
