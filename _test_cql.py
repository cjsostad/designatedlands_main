import requests
import sys

# Test different CQL date formats against BCGW WFS
base_params = {
    'SERVICE': 'WFS',
    'VERSION': '2.0.0',
    'REQUEST': 'GetFeature',
    'typeName': 'WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS',
    'outputFormat': 'application/json',
    'SRSNAME': 'EPSG:3005',
    'maxFeatures': '1',
}


# Fetch DescribeFeatureType to see all fields
print("=== Full schema for FADM_DESIGNATED_AREAS ===")
desc_params = {
    'SERVICE': 'WFS',
    'VERSION': '2.0.0',
    'REQUEST': 'DescribeFeatureType',
    'typeName': 'WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS',
}
r = requests.get('https://openmaps.gov.bc.ca/geo/pub/wfs', params=desc_params, verify=False, timeout=30)
if r.status_code == 200:
    for line in r.text.split('\n'):
        if 'xsd:element' in line:
            print(f"  {line.strip()}")
print()

tests = [
    # Final verification - FTEN_RECREATION_POLY_SVW with RETIREMENT_DATE IS NULL
    ("WHSE_FOREST_TENURE.FTEN_RECREATION_POLY_SVW", "PROJECT_TYPE in ('Recreation Reserve', 'Interpretative Forest') and FILE_STATUS_CODE = 'HI' and RETIREMENT_DATE IS NULL"),
    ("WHSE_FOREST_TENURE.FTEN_RECREATION_POLY_SVW", "PROJECT_TYPE in ('Recreation Site', 'Recreation Trail') and FILE_STATUS_CODE = 'HI' and RETIREMENT_DATE IS NULL"),
    # Confirm FADM fix
    ("WHSE_ADMIN_BOUNDARIES.FADM_DESIGNATED_AREAS", "EXPIRED_OR_CANCELLED = 'N' OR DESIGNATED_AREA_NAME = 'Yale Area 3'"),
]

for i, (layer, cql) in enumerate(tests, 1):
    params = {
        'SERVICE': 'WFS',
        'VERSION': '2.0.0',
        'REQUEST': 'GetFeature',
        'typeName': layer,
        'outputFormat': 'application/json',
        'CQL_FILTER': cql,
        'count': 1,
    }
    r = requests.get('https://openmaps.gov.bc.ca/geo/pub/wfs', params=params, verify=False, timeout=30)
    if r.status_code == 200:
        n = r.json().get('numberMatched', '?')
        print(f"Test {i}: OK ({n} features) -- {layer}: {cql}")
    else:
        print(f"Test {i}: FAIL {r.status_code} -- {layer}: {cql}")
        print(f"         {r.text[:200]}")
import sys; sys.exit()

for i, cql in enumerate(tests):
    params = dict(base_params)
    params['CQL_FILTER'] = cql
    r = requests.get('https://openmaps.gov.bc.ca/geo/pub/wfs', params=params, verify=False, timeout=30)
    status = r.status_code
    if status == 200:
        data = r.json()
        count = len(data.get('features', []))
        print(f"Test {i+1}: OK ({count} features) -- {cql}")
    else:
        # Extract error text
        err = r.text[:300].replace('\n', ' ')
        print(f"Test {i+1}: FAIL {status} -- {cql}")
        print(f"         {err}")
    print()
