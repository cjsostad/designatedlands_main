"""Audit WHA records excluded by NOT LIKE filters when FEATURE_NOTES is NULL."""

import csv
import datetime
import logging
import os
import sys

import requests

WFS_URL = "https://openmaps.gov.bc.ca/geo/pub/wfs"
WFS_LAYER = "WHSE_WILDLIFE_MANAGEMENT.WCP_WILDLIFE_HABITAT_AREA_POLY"
ZONES = ("NO HARVEST ZONE", "CONDITIONAL HARVEST ZONE")
FIELDS = [
    "zone",
    "numberMatched_current",
    "numberMatched_null_notes",
    "numberMatched_broadened",
    "pct_missed",
]

requests.packages.urllib3.disable_warnings()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
LOG = logging.getLogger(__name__)


def _get_count_helper():
    """Import the repository probe when available; otherwise use local HTTP."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    try:
        from date_filter import _count_features
        return _count_features
    except ImportError:
        return None


def _probe(cql, count_helper):
    params = {
        "SERVICE": "WFS",
        "VERSION": "2.0.0",
        "REQUEST": "GetFeature",
        "typeName": WFS_LAYER,
        "outputFormat": "application/json",
        "CQL_FILTER": cql,
        "count": 1,
    }
    url = requests.Request("GET", WFS_URL, params=params).prepare().url

    if count_helper is not None:
        status, raw_count = count_helper(WFS_LAYER, cql)
        LOG.info("WFS probe URL: %s", url)
        LOG.info("WFS probe raw numberMatched: %s", raw_count)
        if status != 200:
            raise RuntimeError(f"WFS probe failed (HTTP {status}): {raw_count}")
    else:
        response = requests.get(WFS_URL, params=params, verify=False, timeout=60)
        LOG.info("WFS probe URL: %s", response.url)
        response.raise_for_status()
        data = response.json()
        raw_count = data.get("numberMatched", len(data.get("features", [])))
        LOG.info("WFS probe raw numberMatched: %s", raw_count)

    try:
        return int(raw_count)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"WFS returned a non-numeric numberMatched: {raw_count!r}") from exc


def _format_table(rows):
    widths = {
        field: max(len(field), *(len(str(row[field])) for row in rows))
        for field in FIELDS
    }
    header = " | ".join(field.ljust(widths[field]) for field in FIELDS)
    rule = "-+-".join("-" * widths[field] for field in FIELDS)
    lines = [header, rule]
    for row in rows:
        lines.append(" | ".join(str(row[field]).ljust(widths[field]) for field in FIELDS))
    return "\n".join(lines)


def main():
    count_helper = _get_count_helper()
    rows = []
    assertions_clean = True

    for zone in ZONES:
        current = (
            f"TIMBER_HARVEST_CODE = '{zone}' AND "
            "FEATURE_NOTES NOT LIKE '%not a legal boundary%'"
        )
        null_notes = f"TIMBER_HARVEST_CODE = '{zone}' AND FEATURE_NOTES IS NULL"
        broadened = f"({current}) OR ({null_notes})"

        count_current = _probe(current, count_helper)
        count_null = _probe(null_notes, count_helper)
        count_broadened = _probe(broadened, count_helper)

        if count_current + count_null != count_broadened:
            assertions_clean = False
            LOG.warning(
                "%s count check failed: current (%d) + null-notes (%d) != "
                "broadened (%d). GeoServer may be behaving differently than "
                "SQL three-valued logic; this audit result is suspect.",
                zone, count_current, count_null, count_broadened,
            )

        pct_missed = (
            round(count_null / count_broadened * 100, 2)
            if count_broadened > 0 else 0.0
        )
        rows.append({
            "zone": zone,
            "numberMatched_current": count_current,
            "numberMatched_null_notes": count_null,
            "numberMatched_broadened": count_broadened,
            "pct_missed": f"{pct_missed:.2f}%",
        })

    print(_format_table(rows))

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = os.path.join(repo_root, "logs")
    os.makedirs(log_dir, exist_ok=True)
    csv_path = os.path.join(
        log_dir,
        f"wha_null_notes_audit_{datetime.date.today():%Y%m%d}.csv",
    )
    with open(csv_path, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    LOG.info("Wrote audit table to %s", csv_path)

    return 0 if assertions_clean else 1


if __name__ == "__main__":
    sys.exit(main())