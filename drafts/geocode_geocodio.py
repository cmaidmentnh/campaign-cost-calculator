#!/usr/bin/env python3
"""
Batch Geocode Voters using Geocodio
====================================

Much faster than Mapbox - sends 10,000 addresses per API call.

Usage:
    python3 geocode_geocodio.py              # Process all un-geocoded voters
    python3 geocode_geocodio.py --test 100   # Test with 100 voters
    python3 geocode_geocodio.py --stats      # Show progress stats
"""

import os
import sys
import json
import time
import argparse
import requests
import psycopg2
from datetime import datetime

# Configuration
GEOCODIO_API_KEY = "2092c932b223d303b27cc096670b0355b5067c3"
GEOCODIO_URL = "https://api.geocod.io/v1.7/geocode"
BATCH_SIZE = 10000  # Geocodio allows up to 10,000 per batch request
COMMIT_SIZE = 10000  # Commit to DB after this many

# Database config
DB_CONFIG = {
    "database": "election_data",
    "user": "postgres",
}


def get_db():
    """Get database connection."""
    return psycopg2.connect(**DB_CONFIG)


def get_stats():
    """Get current geocoding statistics."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("""
        SELECT
            COUNT(*) FILTER (WHERE lat IS NULL AND ad_num IS NOT NULL AND ad_str1 IS NOT NULL) as need_geocoding,
            COUNT(*) FILTER (WHERE lat IS NOT NULL) as geocoded,
            COUNT(*) as total
        FROM statewidechecklist
        WHERE active = true
    """)
    row = cur.fetchone()
    conn.close()

    return {
        "need_geocoding": row[0],
        "geocoded": row[1],
        "total": row[2],
        "percent": round(row[1] / row[2] * 100, 2) if row[2] > 0 else 0
    }


def fetch_batch(cursor, limit):
    """Fetch a batch of voters needing geocoding."""
    cursor.execute("""
        SELECT id_voter, ad_num, ad_str1, ad_unit, ad_city, ad_zip5
        FROM statewidechecklist
        WHERE active = true
        AND lat IS NULL
        AND ad_num IS NOT NULL
        AND ad_str1 IS NOT NULL
        AND ad_city IS NOT NULL
        AND ad_zip5 IS NOT NULL
        ORDER BY id_voter
        LIMIT %s
    """, (limit,))
    return cursor.fetchall()


def format_address(row):
    """Format address for geocoding."""
    addr = f"{row[1]} {row[2]}"  # ad_num + ad_str1
    if row[3]:  # ad_unit
        addr += f" {row[3]}"
    addr += f", {row[4]}, NH {row[5]}"  # city, state, zip
    return addr


def geocode_batch(addresses):
    """
    Send batch to Geocodio API.
    addresses: list of (voter_id, address_string)
    Returns: dict of voter_id -> (lat, lng) or None
    """
    if not addresses:
        return {}

    # Geocodio batch format: just a list of addresses
    address_list = [addr for _, addr in addresses]
    voter_ids = [vid for vid, _ in addresses]

    try:
        resp = requests.post(
            GEOCODIO_URL,
            params={"api_key": GEOCODIO_API_KEY},
            json=address_list,
            timeout=300  # 5 minutes for large batches
        )
        resp.raise_for_status()
        data = resp.json()

        results = {}
        for i, result in enumerate(data.get("results", [])):
            voter_id = voter_ids[i]
            response = result.get("response", {})
            locations = response.get("results", [])

            if locations and len(locations) > 0:
                loc = locations[0].get("location", {})
                lat = loc.get("lat")
                lng = loc.get("lng")
                if lat and lng:
                    results[voter_id] = (lat, lng)

        return results

    except Exception as e:
        print(f"  Geocodio API error: {e}")
        return {}


def update_voters(conn, results):
    """Update voters with geocoded coordinates."""
    if not results:
        return 0

    cur = conn.cursor()
    for voter_id, (lat, lng) in results.items():
        cur.execute("""
            UPDATE statewidechecklist
            SET lat = %s, lng = %s, geocoded_at = NOW()
            WHERE id_voter = %s
        """, (lat, lng, voter_id))

    conn.commit()
    cur.close()
    return len(results)


def process_all(test_limit=None):
    """Process all voters needing geocoding."""
    conn = get_db()
    cur = conn.cursor()

    stats = get_stats()
    total_need = stats["need_geocoding"]
    if test_limit:
        total_need = min(total_need, test_limit)

    print(f"\nGeocoding {total_need:,} voters using Geocodio...")
    print(f"Batch size: {BATCH_SIZE:,} addresses per API call\n")

    processed = 0
    geocoded = 0
    failed = 0
    start_time = time.time()

    while processed < total_need:
        batch_size = min(BATCH_SIZE, total_need - processed)
        rows = fetch_batch(cur, batch_size)

        if not rows:
            break

        # Format addresses
        addresses = [(row[0], format_address(row)) for row in rows]

        print(f"  Batch {processed // BATCH_SIZE + 1}: Sending {len(addresses):,} addresses...")

        # Geocode batch
        results = geocode_batch(addresses)

        # Update database
        updated = update_voters(conn, results)
        geocoded += updated
        failed += len(addresses) - updated
        processed += len(addresses)

        # Progress
        elapsed = time.time() - start_time
        rate = processed / elapsed if elapsed > 0 else 0
        remaining = (total_need - processed) / rate if rate > 0 else 0

        print(f"    Geocoded: {updated:,}/{len(addresses):,} | Total: {geocoded:,}/{processed:,} | "
              f"Rate: {rate:.0f}/sec | ETA: {remaining/60:.1f} min")

    cur.close()
    conn.close()

    print(f"\nComplete!")
    print(f"  Total processed: {processed:,}")
    print(f"  Successfully geocoded: {geocoded:,}")
    print(f"  Failed: {failed:,}")
    print(f"  Time: {(time.time() - start_time)/60:.1f} minutes")


def main():
    parser = argparse.ArgumentParser(description="Geocode voters using Geocodio")
    parser.add_argument("--test", type=int, help="Test with N voters only")
    parser.add_argument("--stats", action="store_true", help="Show current statistics")
    args = parser.parse_args()

    if args.stats:
        stats = get_stats()
        print(f"\nGeocoding Progress")
        print(f"==================")
        print(f"Total active voters: {stats['total']:,}")
        print(f"Already geocoded:    {stats['geocoded']:,} ({stats['percent']}%)")
        print(f"Need geocoding:      {stats['need_geocoding']:,}")
        return

    print(f"Geocodio API Key: {GEOCODIO_API_KEY[:20]}...")
    process_all(test_limit=args.test)


if __name__ == "__main__":
    main()
