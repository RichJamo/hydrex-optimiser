#!/usr/bin/env python3
"""Check how fresh the latest live_gauge_snapshot in the DB is."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH
import sqlite3

conn = sqlite3.connect(DATABASE_PATH)
cur = conn.cursor()

row = cur.execute(
    "SELECT MAX(snapshot_ts), vote_epoch, query_block FROM live_gauge_snapshots LIMIT 1"
).fetchone()

if not row or not row[0]:
    print("No snapshots found in DB.")
    sys.exit(0)

snapshot_ts, vote_epoch, query_block = int(row[0]), row[1], row[2]
age_hours = (time.time() - snapshot_ts) / 3600

print(f"Latest snapshot_ts : {snapshot_ts}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(snapshot_ts))})")
print(f"vote_epoch         : {vote_epoch}")
print(f"query_block        : {query_block}")
print(f"Age                : {age_hours:.1f} hours old")

row2 = cur.execute(
    "SELECT COUNT(*) FROM live_gauge_snapshots WHERE snapshot_ts = ?", (snapshot_ts,)
).fetchone()
print(f"Gauge rows         : {row2[0]}")
conn.close()
