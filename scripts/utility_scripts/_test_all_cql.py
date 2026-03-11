"""
Verify every BCGW WFS CQL query from both CSV files.
1. Check that all referenced fields exist in the WFS schema (DescribeFeatureType).
2. Check that each CQL filter returns HTTP 200 from the WFS endpoint.
"""
import csv, requests, re, xml.etree.ElementTree as ET
from datetime import date
requests.packages.urllib3.disable_warnings()

WFS = 'https://openmaps.gov.bc.ca/geo/pub/wfs'

# ---- helpers ----
_schema_cache = {}
def get_schema_fields(layer):
    if layer in _schema_cache:
        return _schema_cache[layer]
    r = requests.get(WFS, params={
        'SERVICE': 'WFS', 'VERSION': '2.0.0',
        'REQUEST': 'DescribeFeatureType', 'typeName': layer
    }, verify=False, timeout=30)
    fields = set()
    if r.status_code == 200:
        for m in re.finditer(r'name="(\w+)"', r.text):
            fields.add(m.group(1).upper())
    _schema_cache[layer] = fields
    return fields

def extract_field_refs(cql):
    """Extract likely field names from CQL (words before operators)."""
    # Remove string literals
    cleaned = re.sub(r"'[^']*'", '', cql)
    # Find uppercase identifiers that look like field names
    candidates = set()
    for tok in re.split(r'[\s(),]+', cleaned):
        tok = tok.strip()
        if tok and re.match(r'^[A-Z_][A-Z0-9_]*$', tok) and tok not in (
            'AND', 'OR', 'NOT', 'IN', 'IS', 'NULL', 'LIKE', 'BETWEEN', 'TRUE', 'FALSE'
        ):
            candidates.add(tok)
    return candidates

def test_cql(layer, cql):
    params = {
        'SERVICE': 'WFS', 'VERSION': '2.0.0',
        'REQUEST': 'GetFeature', 'typeName': layer,
        'outputFormat': 'application/json',
        'CQL_FILTER': cql, 'count': 1,
    }
    r = requests.get(WFS, params=params, verify=False, timeout=30)
    return r.status_code, r.json().get('numberMatched', '?') if r.status_code == 200 else r.text[:200]

# ---- collect queries from CSVs ----
queries = []
for csv_file in ['sources_designations.csv', 'sources_supporting.csv']:
    with open(csv_file, encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            layer = (row.get('bcgw_layer_name') or '').strip()
            query = (row.get('query') or '').strip()
            name = (row.get('name') or '').strip()
            if layer and query:
                # Expand {currdate}
                if '{currdate}' in query:
                    query = query.format(currdate=date.today().isoformat())
                queries.append((name, layer, query))

print(f"Found {len(queries)} BCGW queries to verify\n")
print("=" * 80)

issues = []
for i, (name, layer, cql) in enumerate(queries, 1):
    print(f"\n--- [{i}] {name} ---")
    print(f"    Layer: {layer}")
    print(f"    CQL:   {cql}")

    # 1. Schema check
    fields = get_schema_fields(layer)
    if fields:
        refs = extract_field_refs(cql)
        missing = refs - fields
        if missing:
            print(f"    SCHEMA WARNING: field(s) not found: {missing}")
            print(f"    Available fields: {sorted(fields)}")
            issues.append((name, 'MISSING_FIELD', missing))
        else:
            print(f"    Schema: OK (all fields exist)")
    else:
        print(f"    Schema: SKIPPED (could not fetch schema)")

    # 2. WFS filter test
    code, result = test_cql(layer, cql)
    if code == 200:
        print(f"    WFS:    OK ({result} features)")
    else:
        print(f"    WFS:    FAIL HTTP {code}")
        print(f"    Response: {result}")
        issues.append((name, 'WFS_FAIL', code))

print("\n" + "=" * 80)
if issues:
    print(f"\n*** {len(issues)} ISSUE(S) FOUND ***")
    for name, itype, detail in issues:
        print(f"  - {name}: {itype} -> {detail}")
else:
    print("\nAll queries verified OK!")
