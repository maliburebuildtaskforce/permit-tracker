import requests
import json
import csv
import time
import os
import re
import argparse
import datetime

# ── Configuration ──────────────────────────────────────────────────────────────

PORTAL_BASE     = "https://mlb-pptsrv.ci.malibu.ca.us"
GIS_BASE        = "https://services3.arcgis.com/w2LtkSgyOOlg6OKZ/arcgis/rest/services"
FIRE_SERVICE    = GIS_BASE + "/MalibuFirePerimeters_01142024/FeatureServer/0"
PARCEL_SERVICE  = GIS_BASE + "/Parcels_Public/FeatureServer/0"
DOC_VIEWER_BASE = "https://publicaccess.ci.malibu.ca.us/onbase-portal/BuildingPermits/index.html"

PALISADES_OBJECTID = 1204
FIRE_DATE          = datetime.datetime(2025, 1, 7)
REQUEST_DELAY      = 0.3

OUTPUT_ALL      = "palisades_permits_all.csv"
OUTPUT_SFR      = "palisades_permits_sfr.csv"
OUTPUT_GEOTECH  = "palisades_permits_geotech.csv"
OUTPUT_GEOJSON  = "palisades_permits_sfr.geojson"
PROGRESS_FILE   = "palisades_progress.json"
CACHE_BBOX      = "cache_fire_bbox.json"
CACHE_APNS      = "cache_fire_apns.json"       # APNs inside fire area (cached forever)

# ── Permit classification ──────────────────────────────────────────────────────

SFR_KEYWORDS = [
    "new sfr", "single family", "new residence", "new dwelling",
    "new house", "rebuild", "reconstruct", "new construction",
    "new 1-story", "new 2-story", "new one-story", "new two-story",
    "new one story", "new two story", "residential dwelling",
    "new detached", "replace sfr", "replacement sfr",
    "fire rebuild", "rebuild sfr", "new home", "new single family"
]

EXCLUDE_KEYWORDS = [
    "solar", "pv", "photovoltaic", "electric panel", "panel upgrade",
    "ev charger", "bulkhead", "retaining wall", "fence", "gate",
    "debris removal", "demolition", "demo only", "hazard tree",
    "temporary power", "re-roof", "roof repair", "window replacement",
    "pool", "spa", "driveway", "grading only", "septic repair",
    "water heater", "hvac", "mechanical", "plumbing repair"
]

GEOTECH_KEYWORDS = [
    "geotechnical", "geotech", "soils report", "soil report",
    "geological", "geology report", "slope stability",
    "soils investigation", "geologic hazard"
]

def classify_permit(permit):
    desc        = (permit.get("DescriptionOfWork") or "").strip().lower()
    ptype       = (permit.get("PermitType") or "").strip().upper()
    date_issued = (permit.get("DateIssued") or "").strip()
    itemnum     = permit.get("Itemnum")

    is_post_fire = False
    if date_issued:
        try:
            dt = datetime.datetime.fromisoformat(
                date_issued.replace("T", " ").split(".")[0])
            is_post_fire = dt >= FIRE_DATE
        except Exception:
            pass

    doc_url = ""
    if itemnum:
        doc_url = "{}?OBKey__19_1={}".format(DOC_VIEWER_BASE, itemnum)

    is_sfr = False
    if ptype in ("BUILDING", "GRADING") and desc:
        strong = any(kw in desc for kw in [
            "new sfr", "single family", "rebuild sfr", "new single family",
            "fire rebuild", "new residence", "new dwelling"])
        has_sfr  = any(kw in desc for kw in SFR_KEYWORDS)
        has_excl = any(kw in desc for kw in EXCLUDE_KEYWORDS)
        is_sfr   = strong or (has_sfr and not has_excl)

    is_geotech = any(kw in desc for kw in GEOTECH_KEYWORDS)

    return {
        "is_post_fire": is_post_fire,
        "is_sfr":       is_sfr,
        "is_geotech":   is_geotech,
        "doc_url":      doc_url,
    }

# ── Cache helpers ──────────────────────────────────────────────────────────────

def load_cache(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return None

def save_cache(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print("  Cached → {}".format(path))

# ── Step 1: Fire bbox (cached) ─────────────────────────────────────────────────

def fetch_fire_bbox(force=False):
    if not force:
        c = load_cache(CACHE_BBOX)
        if c:
            print("  [CACHED] Fire bbox.")
            return c
    print("Fetching Palisades Fire bbox...")
    params = {
        "where": "OBJECTID=" + str(PALISADES_OBJECTID),
        "outFields": "OBJECTID",
        "returnGeometry": "true",
        "returnExtentOnly": "true",
        "outSR": "4326",
        "f": "json",
    }
    r = requests.get(FIRE_SERVICE + "/query", params=params, timeout=30)
    r.raise_for_status()
    ext = r.json().get("extent")
    if not ext:
        raise RuntimeError("Fire extent not found.")
    print("  {:.4f},{:.4f} → {:.4f},{:.4f}".format(
        ext["xmin"], ext["ymin"], ext["xmax"], ext["ymax"]))
    save_cache(CACHE_BBOX, ext)
    return ext

# ── Step 2: APNs in fire area (cached) ────────────────────────────────────────

def fetch_fire_apns(bbox, force=False):
    """
    Returns a set of APN strings (LA County AIN format) that fall
    within the Palisades Fire bounding box, sourced from GIS Parcels_Public.
    Cached after first run — the fire perimeter doesn't change.
    """
    if not force:
        c = load_cache(CACHE_APNS)
        if c:
            print("  [CACHED] {} APNs in fire area.".format(len(c)))
            return set(c)

    print("Fetching parcels in fire area from GIS...")
    bbox_str    = "{},{},{},{}".format(
        bbox["xmin"], bbox["ymin"], bbox["xmax"], bbox["ymax"])
    all_apns    = []
    offset      = 0
    page_size   = 2000

    while True:
        params = {
            "geometry":          bbox_str,
            "geometryType":      "esriGeometryEnvelope",
            "inSR":              "4326",
            "spatialRel":        "esriSpatialRelIntersects",
            "outFields":         "AIN_1,SiteAddress_Full",
            "returnGeometry":    "false",
            "resultOffset":      str(offset),
            "resultRecordCount": str(page_size),
            "f":                 "json",
        }
        r = requests.get(PARCEL_SERVICE + "/query", params=params, timeout=60)
        r.raise_for_status()
        features = r.json().get("features", [])
        if not features:
            break
        for feat in features:
            ain = feat["attributes"].get("AIN_1")
            if ain:
                all_apns.append(str(int(ain)))   # convert to string APN format
        print("  {} APNs so far...".format(len(all_apns)))
        if len(features) < page_size:
            break
        offset += page_size

    print("  Total: {} APNs in fire area.".format(len(all_apns)))
    save_cache(CACHE_APNS, all_apns)
    return set(all_apns)

# ── Step 3: Get portal parcels filtered by fire APNs ──────────────────────────

def fetch_portal_parcels_in_fire(fire_apns):
    """
    Downloads all portal parcels, then filters to only those whose
    APN (LA County AIN) is in the fire area set.
    No address matching needed — pure APN join.
    """
    print("Fetching portal parcel list...")
    r = requests.get(
        PORTAL_BASE + "/Home/getAddressList",
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=60
    )
    r.raise_for_status()
    all_parcels = r.json().get("Data", [])
    print("  {} total portal parcels.".format(len(all_parcels)))

    matched = []
    for p in all_parcels:
        apn = (p.get("APN") or "").strip()
        if apn in fire_apns:
            matched.append(p)

    print("  {} parcels matched to fire area by APN.".format(len(matched)))
    return matched

# ── Step 4: Fetch permits ─────────────────────────────────────────────────────

PORTAL_HEADERS = {
    "Content-Type":     "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer":          PORTAL_BASE + "/Home/PublicBuildingPermits",
}

def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE) as f:
            return set(json.load(f))
    return set()

def save_progress(done):
    with open(PROGRESS_FILE, "w") as f:
        json.dump(list(done), f)

def fetch_permits_for_parcel(apn):
    r = requests.post(
        PORTAL_BASE + "/Home/GetOnBaseBuildingPermits",
        data={"selectedParcel": apn},
        headers=PORTAL_HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("Data", [])

def clean_permit(permit, apn, street_address):
    cleaned = {"APN": apn, "APN_formatted": normalize_apn(apn), "StreetAddress": street_address}
    for k, v in permit.items():
        if isinstance(v, str):
            v = v.strip()
            m = re.match(r'/Date\((\d+)\)/', v)
            if m:
                ts = int(m.group(1)) / 1000
                v  = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        cleaned[k] = v
    return cleaned

# ── Step 5: Output ─────────────────────────────────────────────────────────────

FIELDS = [
    "APN", "StreetAddress", "PermitNo", "PermitType", "PermitStatus",
    "DateIssued", "DateFinal", "DescriptionOfWork",
    "is_sfr", "is_geotech", "doc_url",
    "Itemnum", "StreetNumber", "StreetName",
    "DocDate", "OnBaseDocType", "DocTypeESD", "UnitNo", "CommercialTenant"
]

def write_csv(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print("  {} rows → {}".format(len(rows), path))

def write_geojson(path, rows):
    features = [{"type": "Feature", "geometry": None, "properties": r} for r in rows]
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2)
    print("  {} features → {}".format(len(rows), path))

def normalize_apn(apn):
    """Convert portal APN (4449001007) to sheet APN (4449-001-007)"""
    apn = apn.strip().replace('-', '')  # strip any existing dashes
    if len(apn) == 10:
        return f"{apn[:4]}-{apn[4:7]}-{apn[7:]}"  # → 4449-001-007
    return apn

# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Malibu Palisades permit scraper")
    parser.add_argument("--test",          action="store_true", help="30 parcels only")
    parser.add_argument("--resume",        action="store_true", help="Skip done APNs")
    parser.add_argument("--apn",           type=str,            help="Single APN lookup")
    parser.add_argument("--refresh-cache", action="store_true", help="Re-fetch GIS data")
    args = parser.parse_args()

    if args.apn:
        permits = fetch_permits_for_parcel(args.apn)
        for p in permits:
            cl = classify_permit(p)
            print(json.dumps(dict(**p, **cl), indent=2, default=str))
        return

    force = args.refresh_cache

    # Cached steps (only run once)
    bbox       = fetch_fire_bbox(force=force)
    fire_apns  = fetch_fire_apns(bbox, force=force)

    # Always refresh portal list (lightweight, catches new parcels)
    parcels    = fetch_portal_parcels_in_fire(fire_apns)

    if args.test:
        parcels = parcels[:30]
        print("  TEST MODE: {} parcels".format(len(parcels)))

    done_apns   = load_progress() if args.resume else set()
    all_permits = []
    total       = len(parcels)

    for i, parcel in enumerate(parcels, 1):
        apn     = (parcel.get("APN") or "").strip()
        address = (parcel.get("StreetAddress") or "").strip()

        if apn in done_apns:
            continue

        print("  [{}/{}] {} (APN {})".format(i, total, address, apn))

        try:
            raw = fetch_permits_for_parcel(apn)
            for p in raw:
                cleaned = clean_permit(p, apn, address)
                cl      = classify_permit(cleaned)
                cleaned.update(cl)
                all_permits.append(cleaned)
            done_apns.add(apn)
        except Exception as e:
            print("    ERROR: {}".format(e))

        if i % 50 == 0:
            save_progress(done_apns)

        time.sleep(REQUEST_DELAY)

    save_progress(done_apns)

    if not all_permits:
        print("No permits found.")
        return

    post_fire = [p for p in all_permits if p.get("is_post_fire")]
    sfr       = [p for p in post_fire   if p.get("is_sfr")]
    geotech   = [p for p in post_fire   if p.get("is_geotech")]

    print("\n── Summary ──────────────────────────────")
    print("  Total permits fetched:     {}".format(len(all_permits)))
    print("  Post-fire (≥ Jan 7 2025):  {}".format(len(post_fire)))
    print("  SFR / new house permits:   {}".format(len(sfr)))
    print("  Geotechnical reports:      {}".format(len(geotech)))
    print("─────────────────────────────────────────")

    write_csv(OUTPUT_ALL,     post_fire)
    write_csv(OUTPUT_SFR,     sfr)
    write_csv(OUTPUT_GEOTECH, geotech)
    write_geojson(OUTPUT_GEOJSON, sfr)

    print("\nDone. {} parcels processed.".format(len(done_apns)))
    print("Permit documents: open any doc_url value in a browser.")

if __name__ == "__main__":
    main()
    