"""Diagnostic: show early-vote timing and impact for epoch 1778716800."""
import sys
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from src.db import db_conn

def fmt(ts):
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

EPOCH = 1778716800
VOTE_EPOCH = EPOCH - 604800  # 1778112000

with db_conn() as conn:
    runs = conn.execute(
        """
        SELECT id, vote_sent_at, snapshot_ts, vote_epoch, query_block,
               selected_k, expected_return_usd, tx_hash, status
        FROM auto_vote_runs
        WHERE vote_epoch = ? AND status != 'error'
        ORDER BY vote_sent_at DESC
        LIMIT 5
        """,
        (VOTE_EPOCH,),
    ).fetchall()

    print(f"auto_vote_runs for vote_epoch={VOTE_EPOCH} ({fmt(VOTE_EPOCH)}):")
    if not runs:
        print("  No rows found.")
    for r in runs:
        sent = r[1]
        hours_early = (EPOCH - int(sent)) / 3600
        print(f"  id={r[0]}")
        print(f"    vote_sent_at        = {sent} ({fmt(sent)})")
        print(f"    snapshot_ts         = {r[2]} ({fmt(r[2])})")
        print(f"    hours before boundary = {hours_early:.1f}h")
        print(f"    selected_k          = {r[5]}")
        print(f"    expected_return_usd = ${float(r[6]):.2f}")
        print(f"    tx_hash             = {r[7]}")
        print(f"    status              = {r[8]}")
        print()
