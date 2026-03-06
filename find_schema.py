"""
Query the BCGW WFS DescribeFeatureType endpoint for each layer referenced
in sources_designations.csv and sources_supporting.csv, and report any
fields whose XML schema type contains 'date' or 'time'.

Usage:
    python find_schema.py
"""

import csv
import re
import xml.etree.ElementTree as ET

import requests

requests.packages.urllib3.disable_warnings()

WFS = "https://openmaps.gov.bc.ca/geo/pub/wfs"

# XSD namespace used in DescribeFeatureType responses
XSD_NS = "{http://www.w3.org/2001/XMLSchema}"


def get_date_fields(layer):
    """Return a list of (field_name, xsd_type) tuples for date/time fields."""
    r = requests.get(
        WFS,
        params={
            "SERVICE": "WFS",
            "VERSION": "2.0.0",
            "REQUEST": "DescribeFeatureType",
            "typeName": layer,
        },
        verify=False,
        timeout=30,
    )
    if r.status_code != 200:
        return None  # signal failure

    try:
        root = ET.fromstring(r.text)
    except ET.ParseError:
        return None

    date_fields = []
    for elem in root.iter(f"{XSD_NS}element"):
        name = elem.get("name", "")
        xsd_type = elem.get("type", "")
        if re.search(r"date|time", xsd_type, re.IGNORECASE):
            date_fields.append((name, xsd_type))
    return date_fields


def collect_layers():
    """Read both CSV files and return unique (dataset_name, bcgw_layer) pairs."""
    layers = []
    seen = set()
    for csv_file in ["sources_designations.csv", "sources_supporting.csv"]:
        try:
            with open(csv_file, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    layer = (row.get("bcgw_layer_name") or "").strip()
                    name = (row.get("name") or "").strip()
                    if layer and layer not in seen:
                        seen.add(layer)
                        layers.append((name, layer))
        except FileNotFoundError:
            print(f"WARNING: {csv_file} not found, skipping")
    return layers


def main():
    layers = collect_layers()
    print(f"Found {len(layers)} unique BCGW layers to inspect\n")
    print("=" * 90)

    summary = []

    for name, layer in layers:
        print(f"\n--- {name} ---")
        print(f"    Layer: {layer}")

        date_fields = get_date_fields(layer)

        if date_fields is None:
            print("    ERROR: Could not fetch schema")
            continue

        if date_fields:
            for field_name, xsd_type in date_fields:
                print(f"    DATE FIELD: {field_name}  ({xsd_type})")
            summary.append((name, layer, date_fields))
        else:
            print("    No date fields found")

    print("\n" + "=" * 90)
    print("\nSUMMARY — Layers with date fields:\n")
    if summary:
        for name, layer, fields in summary:
            field_list = ", ".join(f for f, _ in fields)
            print(f"  {name}")
            print(f"    Layer:  {layer}")
            print(f"    Fields: {field_list}\n")
    else:
        print("  None found.")


if __name__ == "__main__":
    main()
