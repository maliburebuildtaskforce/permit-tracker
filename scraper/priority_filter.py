#!/usr/bin/env python3
"""
priority_filter.py

Collapses palisades_permits_all.csv (many rows per APN) down to ONE row per APN,
so it can feed the sheet's permit-link column cleanly.

Selection priority, per APN:
  1. PermitType == "BUILDING"  (the rebuild permit we care about)
  2. a row that actually has a doc_url (a clickable OnBase permit card)
  3. the most recently issued (latest DateIssued)

Reads:  palisades_permits_all.csv   (in the same folder)
Writes: palisades_permits_priority.csv

Keeps the SAME column order as the input, so anything downstream that references
columns by position or header keeps working.
"""

import csv
import os
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
IN_PATH  = os.path.join(SCRIPT_DIR, "palisades_permits_all.csv")
OUT_PATH = os.path.join(SCRIPT_DIR, "palisades_permits_priority.csv")


def rank(row):
    """Higher tuple sorts first. Plain string DateIssued (YYYY-MM-DD) sorts correctly."""
    is_building = 1 if (row.get("PermitType") or "").strip().upper() == "BUILDING" else 0
    has_doc     = 1 if (row.get("doc_url") or "").strip() else 0
    date_issued = (row.get("DateIssued") or "").strip()
    return (is_building, has_doc, date_issued)


def main():
    if not os.path.exists(IN_PATH):
        print("ERROR: {} not found — run malibu_palisades_permits.py first.".format(IN_PATH))
        sys.exit(1)

    with open(IN_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        if "APN" not in fieldnames:
            print("ERROR: input has no APN column. Headers: {}".format(fieldnames))
            sys.exit(1)
        rows = list(reader)

    best = {}
    for r in rows:
        apn = (r.get("APN") or "").strip()
        if not apn:
            continue
        if apn not in best or rank(r) > rank(best[apn]):
            best[apn] = r

    with open(OUT_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for apn in sorted(best.keys()):
            writer.writerow(best[apn])

    print("Collapsed {} permit rows -> {} unique APNs".format(len(rows), len(best)))
    print("Wrote {}".format(OUT_PATH))


if __name__ == "__main__":
    main()
