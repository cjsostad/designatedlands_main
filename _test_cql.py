import requests

WFS_URL = 'https://openmaps.gov.bc.ca/geo/pub/wfs'

# Fetch DescribeFeatureType to see all fields for key layers
for layer in (
    'WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS',
    'WHSE_FOREST_TENURE.FTEN_RECREATION_POLY_SVW',
):
    print(f"=== Schema for {layer} ===")
    desc_params = {
        'SERVICE': 'WFS',
        'VERSION': '2.0.0',
        'REQUEST': 'DescribeFeatureType',
        'typeName': layer,
    }
    r = requests.get(WFS_URL, params=desc_params, verify=False, timeout=30)
    if r.status_code == 200:
        for line in r.text.split('\n'):
            if 'xsd:element' in line:
                print(f"  {line.strip()}")
    else:
        print(f"  ERROR {r.status_code}: {r.text[:200]}")
    print()

tests = [
    # Confirm FADM fix: RETIREMENT_DATE replaced by EXPIRED_OR_CANCELLED
    ("WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS", "EXPIRED_OR_CANCELLED = 'N' OR DESIGNATED_AREA_NAME = 'Yale Area 3'"),
    # Verify FTEN_RECREATION_POLY_SVW — RETIREMENT_DATE IS NULL is still valid
    ("WHSE_FOREST_TENURE.FTEN_RECREATION_POLY_SVW", "PROJECT_TYPE in ('Recreation Reserve', 'Interpretative Forest') and FILE_STATUS_CODE = 'HI' and RETIREMENT_DATE IS NULL"),
    ("WHSE_FOREST_TENURE.FTEN_RECREATION_POLY_SVW", "PROJECT_TYPE in ('Recreation Site', 'Recreation Trail') and FILE_STATUS_CODE = 'HI' and RETIREMENT_DATE IS NULL"),
]

for i, (layer, cql) in enumerate(tests, 1):
    params = {
        'SERVICE': 'WFS',
        'VERSION': '2.0.0',
        'REQUEST': 'GetFeature',
        'typeName': layer,
        'outputFormat': 'application/json',
        'CQL_FILTER': cql,
        'count': '1',
    }
    r = requests.get(WFS_URL, params=params, verify=False, timeout=30)
    if r.status_code == 200 and 'json' in r.headers.get('Content-Type', ''):
        n = r.json().get('numberMatched', '?')
        print(f"Test {i}: OK ({n} features) -- {layer}: {cql}")
    else:
        print(f"Test {i}: FAIL {r.status_code} -- {layer}: {cql}")
        print(f"         {r.text[:200]}")
