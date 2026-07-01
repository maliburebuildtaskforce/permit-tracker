#!/usr/bin/env python3
"""
fetch_geo_coastal_v3.py

Queries Malibu Public Records Portal (ESD - Geology Files) for
ALL geotechnical and coastal engineering reports for fire-scar parcels.

Keeps up to 3 most recent reports per type per parcel.

Reads:  cache_fire_apns_polygon.json   (1,860 APNs inside fire perimeter)
Writes: palisades_permits_reports.csv   (all parcels with geo/coastal reports)
        _reports_cache.json             (resume cache)
"""

import csv, json, time, sys, os, re, urllib.parse, argparse
from datetime import datetime

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests"); import requests

# ── Config ────────────────────────────────────────────────────
REPORTS_API = "https://publicaccess.ci.malibu.ca.us/onbase-portal/api/CustomQuery/KeywordSearch"
DOC_BASE    = "https://publicaccess.ci.malibu.ca.us/onbase-portal/api/Document/"
QUERY_ID    = 111        # ESD - Geology Files
KEYWORD_ID  = 104        # APN/Parcel No
DELAY       = 0.35       # seconds between API calls
MAX_REPORTS = 3          # keep up to 3 most recent per type

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Document type classification ──────────────────────────────
GEO_TYPES = [
    "GEOTECHNICAL REPORT",
    "GEOTECHNICAL REPORTS",
    "GEOLOGIC/GEOTECHNICAL REPORT",
    "SOILS OBSERVATION REPORT",
]

COASTAL_TYPES = [
    "COASTAL ENGINEERING REPORT",
]

# ── Helpers ───────────────────────────────────────────────────

def normalize_apn(apn_str):
    return re.sub(r'\D', '', str(apn_str))

def dash_apn(apn_digits):
    d = normalize_apn(apn_digits).zfill(10)
    return f"{d[:4]}-{d[4:7]}-{d[7:10]}"

def parse_report_date(date_str):
    """Parse date like '6/20/2025' -> datetime, or None on failure."""
    try:
        return datetime.strptime(date_str.strip(), "%m/%d/%Y")
    except:
        try:
            parts = date_str.strip().split("/")
            if len(parts) == 3:
                return datetime(int(parts[2]), int(parts[0]), int(parts[1]))
        except:
            pass
    return None

def parse_year(date_str):
    """Extract just the year from a date string."""
    dt = parse_report_date(date_str)
    return dt.year if dt else 0

# ── API query ─────────────────────────────────────────────────

def fetch_geology_files(session, apn_dashed):
    """Query Public Records Portal for geology files for an APN."""
    payload = {
        "QueryID": QUERY_ID,
        "Keywords": [{"ID": KEYWORD_ID, "Value": apn_dashed, "KeywordOperator": "="}],
        "QueryLimit": 0
    }
    try:
        r = session.post(REPORTS_API, json=payload, timeout=30)
        r.raise_for_status()
        result = r.json()
        return result.get("Data", []) if isinstance(result, dict) else result
    except Exception as e:
        print(f"  ⚠ Error querying {apn_dashed}: {e}")
        return []

# ── Extract and classify reports ──────────────────────────────

def extract_all_reports(docs):
    """
    From docs, find ALL geo and coastal reports (no year filter).
    Returns up to MAX_REPORTS most recent of each type.
    Each entry is (year, date_str, url).
    """
    geo_candidates = []
    coastal_candidates = []

    for doc in docs:
        doc_id = doc.get("ID", "")
        cols = doc.get("DisplayColumnValues", [])
        doc_type = cols[3]["Value"].strip().upper() if len(cols) > 3 else ""
        doc_date = cols[6]["Value"].strip() if len(cols) > 6 else ""
        year = parse_year(doc_date)
        dt = parse_report_date(doc_date)

        url = DOC_BASE + urllib.parse.quote(str(doc_id), safe="") + "/"

        if any(t in doc_type for t in GEO_TYPES):
            geo_candidates.append((dt or datetime.min, year, doc_date, url))
        elif any(t in doc_type for t in COASTAL_TYPES):
            coastal_candidates.append((dt or datetime.min, year, doc_date, url))

    # Sort by date descending (most recent first), take top MAX_REPORTS
    geo_candidates.sort(key=lambda x: x[0], reverse=True)
    coastal_candidates.sort(key=lambda x: x[0], reverse=True)

    geo_results = [(c[1], c[2], c[3]) for c in geo_candidates[:MAX_REPORTS]]
    coastal_results = [(c[1], c[2], c[3]) for c in coastal_candidates[:MAX_REPORTS]]

    return geo_results, coastal_results

# ── Load fire-scar APNs ──────────────────────────────────────

def load_fire_apns():
    """Load fire-scar parcel APNs — prefer polygon-filtered list."""

    # Primary: polygon-filtered (accurate fire perimeter)
    path1 = os.path.join(SCRIPT_DIR, "cache_fire_apns_polygon.json")
    if os.path.exists(path1):
        with open(path1) as f:
            data = json.load(f)
        apns = sorted(set(normalize_apn(a) for a in data if normalize_apn(a)))
        print(f"  Loaded {len(apns)} APNs from cache_fire_apns_polygon.json (fire perimeter)")
        return apns

    # Fallback: bounding box list
    path2 = os.path.join(SCRIPT_DIR, "cache_fire_apns.json")
    if os.path.exists(path2):
        with open(path2) as f:
            data = json.load(f)
        apns = sorted(set(normalize_apn(a) for a in data if normalize_apn(a)))
        print(f"  ⚠ Using bounding-box list: {len(apns)} APNs from cache_fire_apns.json")
        print(f"    (Run the polygon query to get accurate fire perimeter APNs)")
        return apns

    print("  ERROR: No APN cache found.")
    print("  Place cache_fire_apns_polygon.json in the script directory.")
    return None

# ── Main ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Fetch geo & coastal reports for fire-scar parcels")
    parser.add_argument("--resume", action="store_true", help="Resume from cached results")
    parser.add_argument("--test", type=int, default=0, help="Only query N parcels (for testing)")
    parser.add_argument("--output-dir", default=SCRIPT_DIR)
    args = parser.parse_args()

    # ── Load APNs ─────────────────────────────────────────────
    print("Loading fire-scar parcels...")
    all_apns = load_fire_apns()
    if all_apns is None:
        sys.exit(1)

    if args.test > 0:
        all_apns = all_apns[:args.test]
        print(f"  TEST MODE: {len(all_apns)} parcels")

    print(f"\nTotal parcels to query: {len(all_apns)}")

    # ── Resume support ────────────────────────────────────────
    reports_cache = os.path.join(args.output_dir, "_reports_cache.json")
    reports = {}
    if args.resume and os.path.exists(reports_cache):
        with open(reports_cache) as f:
            reports = json.load(f)
        print(f"  Resumed: {len(reports)} APNs already cached")

    # ── Query Public Records Portal ───────────────────────────
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})

    to_query = [a for a in all_apns if a not in reports]
    print(f"  {len(to_query)} APNs to query ({len(all_apns) - len(to_query)} cached)\n")

    for i, apn in enumerate(to_query):
        dashed = dash_apn(apn)
        print(f"  [{i+1}/{len(to_query)}] {dashed}...", end=" ", flush=True)

        docs = fetch_geology_files(session, dashed)
        entry = {"doc_count": len(docs) if docs else 0}

        if docs:
            geo_list, coastal_list = extract_all_reports(docs)
            for j in range(MAX_REPORTS):
                if j < len(geo_list):
                    entry[f"geo_url_{j+1}"] = geo_list[j][2]
                    entry[f"geo_year_{j+1}"] = geo_list[j][0]
                else:
                    entry[f"geo_url_{j+1}"] = ""
                    entry[f"geo_year_{j+1}"] = ""
            for j in range(MAX_REPORTS):
                if j < len(coastal_list):
                    entry[f"coastal_url_{j+1}"] = coastal_list[j][2]
                    entry[f"coastal_year_{j+1}"] = coastal_list[j][0]
                else:
                    entry[f"coastal_url_{j+1}"] = ""
                    entry[f"coastal_year_{j+1}"] = ""

            g = len(geo_list)
            c = len(coastal_list)
            print(f"{len(docs)} docs -> {g} geo, {c} coastal")
        else:
            for j in range(MAX_REPORTS):
                entry[f"geo_url_{j+1}"] = ""
                entry[f"geo_year_{j+1}"] = ""
                entry[f"coastal_url_{j+1}"] = ""
                entry[f"coastal_year_{j+1}"] = ""
            print("no docs")

        reports[apn] = entry

        # Save cache periodically
        if (i + 1) % 50 == 0:
            with open(reports_cache, "w") as f:
                json.dump(reports, f)
            print(f"  [cache saved at {i+1}]")

        time.sleep(DELAY)

    # Final cache save
    with open(reports_cache, "w") as f:
        json.dump(reports, f)

    # ── Summary ───────────────────────────────────────────────
    geo_count = sum(1 for r in reports.values() if r.get("geo_url_1"))
    coastal_count = sum(1 for r in reports.values() if r.get("coastal_url_1"))
    any_report = sum(1 for r in reports.values()
                     if r.get("geo_url_1") or r.get("coastal_url_1"))
    multi_geo = sum(1 for r in reports.values() if r.get("geo_url_2"))
    multi_coastal = sum(1 for r in reports.values() if r.get("coastal_url_2"))

    print(f"\n{'='*55}")
    print(f"Results across {len(reports)} parcels queried:")
    print(f"  {geo_count} parcels with geo reports ({multi_geo} with 2+)")
    print(f"  {coastal_count} parcels with coastal reports ({multi_coastal} with 2+)")
    print(f"  {any_report} parcels with any report")
    print(f"{'='*55}")

    # ── Write CSV ─────────────────────────────────────────────
    out_csv = os.path.join(args.output_dir, "palisades_permits_reports.csv")
    fieldnames = ["APN",
                  "geo_url_1", "geo_year_1",
                  "geo_url_2", "geo_year_2",
                  "geo_url_3", "geo_year_3",
                  "coastal_url_1", "coastal_year_1",
                  "coastal_url_2", "coastal_year_2",
                  "coastal_url_3", "coastal_year_3"]

    row_count = 0
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for apn in sorted(reports.keys()):
            r = reports[apn]
            if r.get("geo_url_1") or r.get("coastal_url_1"):
                row = {"APN": apn}
                for j in range(1, MAX_REPORTS + 1):
                    row[f"geo_url_{j}"] = r.get(f"geo_url_{j}", "")
                    row[f"geo_year_{j}"] = r.get(f"geo_year_{j}", "")
                    row[f"coastal_url_{j}"] = r.get(f"coastal_url_{j}", "")
                    row[f"coastal_year_{j}"] = r.get(f"coastal_year_{j}", "")
                writer.writerow(row)
                row_count += 1

    print(f"\nWrote {out_csv} ({row_count} rows with reports)")
    print("\n✅ Done!")


if __name__ == "__main__":
    main()