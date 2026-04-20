#!/usr/bin/env python3
"""Enrich an epoch allocation CSV with boundary and actual return columns.

Columns added:
  boundary_usd      = total bribe pool USD sitting in the gauge at the boundary
                      (full pool, not share-adjusted)
  expected_share_pct = expected_usd / boundary_usd * 100 (your forecast capture %)
  actual_usd        = user-supplied claimed USD (populated externally; 0 until set)
  pct_drop          = (expected_usd - actual_usd) / expected_usd * 100

Usage:
    python scripts/_diagnostic_enrich_alloc_actual.py \
        --csv analysis/pre_boundary/epoch_1776297600_boundary_opt_alloc_k50.csv \
        --epoch 1776297600
"""

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

from config.settings import DATABASE_PATH


def load_token_prices_asof(conn, cutoff_ts):
    price_map = {}
    rows = conn.execute(
        """
        WITH latest AS (
            SELECT lower(token_address) AS token_address, MAX(timestamp) AS ts
            FROM historical_token_prices
            WHERE timestamp <= ? AND COALESCE(usd_price, 0) > 0
            GROUP BY lower(token_address)
        )
        SELECT lower(h.token_address), h.usd_price
        FROM historical_token_prices h
        JOIN latest l ON lower(h.token_address) = l.token_address AND h.timestamp = l.ts
        WHERE COALESCE(h.usd_price, 0) > 0
        """,
        (int(cutoff_ts),),
    ).fetchall()
    for token, usd in rows:
        price_map[str(token).lower()] = float(usd)
    fallback = conn.execute(
        "SELECT lower(token_address), usd_price FROM token_prices WHERE COALESCE(usd_price,0)>0"
    ).fetchall()
    for token, usd in fallback:
        price_map.setdefault(str(token).lower(), float(usd))
    return price_map


def expected_return_usd(total_usd, base_votes, your_votes):
    if your_votes <= 0:
        return 0.0
    denom = float(base_votes) + float(your_votes)
    if denom <= 0:
        return 0.0
    return float(total_usd) * (float(your_votes) / denom)


def load_boundary_data(db_path, epoch):
    conn = sqlite3.connect(str(db_path))
    price_map = load_token_prices_asof(conn, int(epoch))

    reward_rows = conn.execute(
        """
        SELECT lower(gauge_address), lower(reward_token), rewards_raw, token_decimals,
               COALESCE(usd_price, 0.0), COALESCE(total_usd, 0.0)
        FROM boundary_reward_snapshots
        WHERE epoch = ? AND active_only = 1
        """,
        (int(epoch),),
    ).fetchall()

    bribe_by_gauge = {}
    for gauge, token, rewards_raw, decimals, usd_price, total_usd in reward_rows:
        gauge_l = str(gauge or "").lower()
        if not gauge_l:
            continue
        total_usd_f = float(total_usd or 0.0)
        if total_usd_f > 0:
            bribe_by_gauge[gauge_l] = bribe_by_gauge.get(gauge_l, 0.0) + total_usd_f
            continue
        token_l = str(token or "").lower()
        dec_i = int(decimals or 18)
        try:
            reward_amt = float(int(str(rewards_raw or "0"))) / float(10 ** max(0, dec_i))
        except Exception:
            reward_amt = 0.0
        if reward_amt <= 0:
            continue
        price = float(usd_price or 0.0) or float(price_map.get(token_l, 0.0))
        if price <= 0:
            continue
        bribe_by_gauge[gauge_l] = bribe_by_gauge.get(gauge_l, 0.0) + reward_amt * price

    gauge_rows = conn.execute(
        """
        SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)), CAST(votes_raw AS REAL)
        FROM boundary_gauge_values
        WHERE epoch = ? AND active_only = 1
        """,
        (int(epoch),),
    ).fetchall()

    bribe_by_pool = {}
    votes_by_pool = {}
    for gauge, pool, votes in gauge_rows:
        gauge_l = str(gauge).lower()
        pool_l = str(pool).lower()
        bribe_by_pool[pool_l] = bribe_by_pool.get(pool_l, 0.0) + float(bribe_by_gauge.get(gauge_l, 0.0))
        votes_by_pool[pool_l] = votes_by_pool.get(pool_l, 0.0) + float(votes or 0.0)

    conn.close()
    return bribe_by_pool, votes_by_pool


def load_final_executed_votes(db_path, epoch):
    conn = sqlite3.connect(str(db_path))
    vote_epoch = int(epoch) - 604800
    run_row = conn.execute(
        """
        SELECT id FROM auto_vote_runs
        WHERE vote_epoch = ? AND status = 'tx_success'
        ORDER BY completed_at DESC LIMIT 1
        """,
        (vote_epoch,),
    ).fetchone()
    if not run_row:
        conn.close()
        return {}, None
    run_id = run_row[0]
    strategy_tag = f"auto_voter_run_{run_id}"
    rows = conn.execute(
        """
        SELECT lower(pool_address), SUM(executed_votes)
        FROM executed_allocations
        WHERE epoch = ? AND strategy_tag = ?
        GROUP BY lower(pool_address)
        """,
        (int(epoch), strategy_tag),
    ).fetchall()
    conn.close()
    return {pool: float(votes or 0) for pool, votes in rows}, strategy_tag


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--db-path", default=DATABASE_PATH)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    bribe_by_pool, _base_votes_by_pool = load_boundary_data(Path(args.db_path), args.epoch)

    with csv_path.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or rows[0].keys())

    new_cols = [c for c in ["boundary_usd", "expected_share_pct", "actual_usd", "pct_drop"] if c not in fieldnames]
    fieldnames = fieldnames + new_cols

    enriched = []
    for row in rows:
        pool = str(row.get("pool", "")).lower()
        expected = float(row.get("expected_usd", 0) or 0)
        total_bribe = float(bribe_by_pool.get(pool, 0.0))
        expected_share_pct = (expected / total_bribe * 100.0) if total_bribe > 0 else 0.0
        actual = float(row.get("actual_usd", 0) or 0)  # user-supplied; zero until set
        pct_drop = ((expected - actual) / expected * 100.0) if expected > 0 and actual > 0 else 0.0
        row["boundary_usd"] = f"{total_bribe:.6f}"
        row["expected_share_pct"] = f"{expected_share_pct:.2f}"
        row["actual_usd"] = f"{actual:.6f}"
        row["pct_drop"] = f"{pct_drop:.2f}"
        enriched.append(row)

    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(enriched)

    total_expected = sum(float(r["expected_usd"]) for r in enriched)
    total_boundary = sum(float(r["boundary_usd"]) for r in enriched)
    nonzero_pools = sum(1 for r in enriched if float(r["boundary_usd"]) > 0)

    print(f"Enriched: {csv_path.name}")
    print(f"  Pools with boundary bribe data : {nonzero_pools}/{len(enriched)}")
    print(f"  Total boundary bribe pool USD  : ${total_boundary:,.2f}")
    print(f"  Total expected (our share)     : ${total_expected:,.2f}")
    print(f"  Forecast capture rate          : {total_expected / total_boundary * 100:.1f}% of available bribes")


if __name__ == "__main__":
    main()
