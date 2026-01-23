#!/usr/bin/env python3 -u
"""
Batch Geocode Voters and Assign Districts
==========================================

Geocodes NH voter addresses using Mapbox API and assigns legislative districts
using the legislators API.

Usage:
    python3 batch_geocode.py                    # Process all un-geocoded voters
    python3 batch_geocode.py --batch-size 500   # Custom batch size
    python3 batch_geocode.py --test 10          # Test with 10 voters only
    python3 batch_geocode.py --resume           # Resume from last progress
    python3 batch_geocode.py --stats            # Show current progress stats

Run on: Secondary server (138.197.36.143)
Requires: MAPBOX_TOKEN in environment or .env file
"""

import os
import sys
import json
import time
import argparse
import hashlib
import requests
import psycopg2
from datetime import datetime
from pathlib import Path

# Configuration
MAPBOX_TOKEN = os.environ.get("MAPBOX_TOKEN", "pk.eyJ1IjoiY2hyaXNtYWlkbWVudG5oIiwiYSI6ImNtNnM3NjN2YjA1ZmgybHB3bTA0eDdib3gifQ.Rxi5tnv_9Wh9uv5kgiuNlg")
LEGISLATORS_API = os.environ.get("LEGISLATORS_API", "http://138.197.20.97:5001")
PROGRESS_FILE = Path(__file__).parent / "geocode_progress.json"
GEOCODE_CACHE_FILE = Path(__file__).parent / "geocode_cache.json"

# Rate limits
MAPBOX_RATE_LIMIT = 5  # requests per second (free tier)
BATCH_SIZE = 1000  # voters per commit
CURSOR_CHUNK = 500  # rows to fetch from DB at a time (memory efficient)

# Database config - use Unix socket for peer auth
DB_CONFIG = {
    "database": "election_data",
    "user": "postgres",
}


def get_db():
    """Get database connection."""
    return psycopg2.connect(**DB_CONFIG)


def load_progress():
    """Load progress from file."""
    if PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            return json.load(f)
    return {
        "total_processed": 0,
        "total_geocoded": 0,
        "total_failed": 0,
        "last_voter_id": None,
        "started_at": None,
        "last_updated": None,
    }


def save_progress(progress):
    """Save progress to file."""
    progress["last_updated"] = datetime.now().isoformat()
    with open(PROGRESS_FILE, "w") as f:
        json.dump(progress, f, indent=2)


def load_geocode_cache():
    """Load geocode cache from file."""
    if GEOCODE_CACHE_FILE.exists():
        try:
            with open(GEOCODE_CACHE_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}


def save_geocode_cache(cache):
    """Save geocode cache to file."""
    with open(GEOCODE_CACHE_FILE, "w") as f:
        json.dump(cache, f)


def address_hash(address, city, zip5):
    """Create hash key for address caching."""
    addr_str = f"{address}|{city}|{zip5}".upper()
    return hashlib.md5(addr_str.encode()).hexdigest()[:16]


def geocode_address(address, city, zip5, cache):
    """
    Geocode an address using Mapbox API.
    Returns (lat, lng) tuple or None on failure.
    Uses cache to avoid duplicate API calls.
    """
    cache_key = address_hash(address, city, zip5)
    if cache_key in cache:
        cached = cache[cache_key]
        if cached is None:
            return None
        return cached["lat"], cached["lng"]

    addr_str = f"{address}, {city}, NH {zip5}"

    try:
        url = "https://api.mapbox.com/geocoding/v5/mapbox.places/" + requests.utils.quote(addr_str) + ".json"
        params = {
            "access_token": MAPBOX_TOKEN,
            "country": "US",
            "limit": 1,
            "types": "address",
        }

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()

        data = resp.json()
        features = data.get("features", [])

        if features:
            coords = features[0]["center"]
            lat, lng = coords[1], coords[0]
            cache[cache_key] = {"lat": lat, "lng": lng}
            return lat, lng
        else:
            cache[cache_key] = None
            return None

    except Exception as e:
        print(f"  Geocode error for {addr_str}: {e}")
        cache[cache_key] = None
        return None


def lookup_districts(lat, lng):
    """Look up legislative districts for coordinates."""
    try:
        url = f"{LEGISLATORS_API}/api/districts/lookup"
        params = {"lat": lat, "lng": lng}

        resp = requests.get(url, params=params, timeout=10)
        resp.raise_for_status()

        data = resp.json()

        # Handle None values from API (location outside NH)
        house = data.get("house_district")
        senate = data.get("senate_district")
        floterial = data.get("floterial_district")

        return {
            "house_district": house.get("full_name") if house else None,
            "senate_district": f"District {senate.get('district_number')}" if senate else None,
            "floterial_district": floterial.get("full_name") if floterial else None,
        }

    except Exception as e:
        print(f"  District lookup error for ({lat}, {lng}): {e}")
        return {}


def get_stats():
    """Get current geocoding statistics."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM statewidechecklist WHERE active = true")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM statewidechecklist WHERE active = true AND lat IS NOT NULL")
    geocoded = cur.fetchone()[0]

    cur.execute("""
        SELECT county, COUNT(*) as total,
               COUNT(lat) as geocoded
        FROM statewidechecklist
        WHERE active = true
        GROUP BY county
        ORDER BY county
    """)
    by_county = cur.fetchall()

    conn.close()

    return {
        "total_active": total,
        "geocoded": geocoded,
        "remaining": total - geocoded,
        "percent": round(geocoded / total * 100, 2) if total > 0 else 0,
        "by_county": by_county,
    }


def process_batch(batch_size=BATCH_SIZE, test_limit=None, resume=False):
    """Process voters in chunks to avoid cursor timeout."""
    progress = load_progress() if resume else {
        "total_processed": 0,
        "total_geocoded": 0,
        "total_failed": 0,
        "last_voter_id": None,
        "started_at": datetime.now().isoformat(),
        "last_updated": None,
    }

    geocode_cache = load_geocode_cache()

    conn = get_db()
    cur = conn.cursor()
    update_cur = conn.cursor()

    print(f"\nStarting geocoding...")

    processed = 0
    geocoded = 0
    failed = 0
    batch_updates = []
    last_api_call = 0
    done = False

    while not done:
        # Fetch next chunk of voters
        query = """
            SELECT id_voter, ad_num, ad_str1, ad_city, ad_zip5
            FROM statewidechecklist
            WHERE active = true
            AND lat IS NULL
            AND ad_num IS NOT NULL
            AND ad_str1 IS NOT NULL
            AND ad_city IS NOT NULL
            AND ad_zip5 IS NOT NULL
        """

        if progress.get("last_voter_id"):
            last_id = progress["last_voter_id"]
            query += f" AND id_voter > '{last_id}'::text"

        query += f" ORDER BY id_voter LIMIT {CURSOR_CHUNK}"

        cur.execute(query)
        rows = cur.fetchall()

        if not rows:
            break  # No more voters to process

        for row in rows:
            voter_id = row[0]
            ad_num = row[1]
            ad_str1 = row[2]
            city = row[3]
            zip5 = row[4]

            address = f"{ad_num} {ad_str1}"

            # Rate limiting
            elapsed = time.time() - last_api_call
            if elapsed < (1 / MAPBOX_RATE_LIMIT):
                time.sleep((1 / MAPBOX_RATE_LIMIT) - elapsed)

            coords = geocode_address(address, city, zip5, geocode_cache)
            last_api_call = time.time()

            if coords:
                lat, lng = coords
                batch_updates.append({
                    "voter_id": voter_id,
                    "lat": lat,
                    "lng": lng,
                })
                geocoded += 1
            else:
                failed += 1

            processed += 1
            progress["last_voter_id"] = voter_id

            if processed % 100 == 0:
                print(f"  Processed {processed:,} - Geocoded: {geocoded:,}, Failed: {failed:,}")

            if len(batch_updates) >= batch_size:
                commit_batch(update_cur, batch_updates)
                conn.commit()
                batch_updates = []

                progress["total_processed"] += batch_size
                progress["total_geocoded"] += geocoded
                save_progress(progress)
                save_geocode_cache(geocode_cache)

                geocoded = 0
                failed = 0

            if test_limit and processed >= test_limit:
                done = True
                break

    if batch_updates:
        commit_batch(update_cur, batch_updates)
        conn.commit()

    progress["total_processed"] += processed % batch_size if batch_size else processed
    progress["total_geocoded"] += geocoded
    progress["total_failed"] += failed
    save_progress(progress)
    save_geocode_cache(geocode_cache)

    cur.close()
    update_cur.close()
    conn.close()

    total_geocoded = progress["total_geocoded"]
    print(f"\nCompleted! Processed: {processed:,}, Total geocoded: {total_geocoded:,}")


def commit_batch(cur, updates):
    """Commit a batch of updates to the database."""
    if not updates:
        return

    for u in updates:
        cur.execute("""
            UPDATE statewidechecklist
            SET lat = %s, lng = %s, geocoded_at = NOW()
            WHERE id_voter = %s
        """, (u["lat"], u["lng"], u["voter_id"]))

    print(f"  Committed {len(updates)} updates")


def main():
    parser = argparse.ArgumentParser(description="Batch geocode voters and assign districts")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help="Voters per commit")
    parser.add_argument("--test", type=int, help="Test with N voters only")
    parser.add_argument("--resume", action="store_true", help="Resume from last progress")
    parser.add_argument("--stats", action="store_true", help="Show current statistics")
    args = parser.parse_args()

    if args.stats:
        stats = get_stats()
        print(f"\nGeocoding Progress")
        print(f"==================")
        total_active = stats["total_active"]
        geocoded_count = stats["geocoded"]
        remaining = stats["remaining"]
        percent = stats["percent"]
        print(f"Total active voters: {total_active:,}")
        print(f"Geocoded:            {geocoded_count:,} ({percent}%)")
        print(f"Remaining:           {remaining:,}")
        print(f"\nBy County:")
        for c in stats["by_county"]:
            pct = c[2] / c[1] * 100 if c[1] > 0 else 0
            print(f"  {c[0]}: {c[2]:,}/{c[1]:,} ({pct:.1f}%)")
        return

    if not MAPBOX_TOKEN:
        print("ERROR: MAPBOX_TOKEN environment variable not set")
        sys.exit(1)

    print(f"Starting batch geocoding...")
    print(f"  Mapbox token: {MAPBOX_TOKEN[:20]}...")
    print(f"  Legislators API: {LEGISLATORS_API}")
    print(f"  Batch size: {args.batch_size}")
    print(f"  Cursor chunk: {CURSOR_CHUNK} rows (memory efficient)")

    process_batch(
        batch_size=args.batch_size,
        test_limit=args.test,
        resume=args.resume,
    )


if __name__ == "__main__":
    main()
