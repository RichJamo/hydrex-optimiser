#!/usr/bin/env python3
"""
Multi-epoch per-pool dilution analysis.

For every epoch with executed allocations, shows per-pool:
  - Our votes committed
  - Others' votes at boundary (boundary_total - ours)
  - Bribe reward total (from preboundary_dev.db T-1 snapshot = closest to boundary)
  - Realized return (corrected formula: base = others only)
  - ROI per 1k votes

Then aggregates across epochs to show which pools consistently suffer dilution
and which are stable/improving.
"""
import sqlite3
import datetime
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimizer import expected_return_usd

LIVE_DB = "data/db/data.db"
PRE_DB  = "data/db/preboundary_dev.db"

live = sqlite3.connect(LIVE_DB)
pre  = sqlite3.connect(PRE_DB)

# ── 1. Load all (epoch, run_id, vote_sent_at) pairs with executed allocations
runs = live.execute("""
    SELECT ea.epoch,
           avr.id AS run_id,
           avr.vote_sent_at,
           avr.expected_return_usd AS run_expected
    FROM executed_allocations ea
    JOIN auto_vote_runs avr
        ON ea.strategy_tag = 'auto_voter_run_' || avr.id
        AND avr.status = 'tx_success'
    GROUP BY ea.epoch
    ORDER BY ea.epoch ASC
""").fetchall()

print(f"Found {len(runs)} epochs with executed allocations\n")

# ── 2. Per-epoch analysis
all_rows = []  # (epoch, pool, our_votes, others_bndry, bribe_usd, realized, roi_1k)

for epoch, run_id, vote_sent_at, run_expected in runs:
    epoch_dt = datetime.datetime.utcfromtimestamp(int(epoch)).strftime("%Y-%m-%d")
    strategy_tag = f"auto_voter_run_{run_id}"

    # Our executed votes per gauge
    exec_alloc = {
        str(g).lower(): int(v)
        for g, v in live.execute(
            "SELECT lower(gauge_address), executed_votes FROM executed_allocations "
            "WHERE epoch=? AND strategy_tag=?",
            (epoch, strategy_tag),
        ).fetchall()
        if v and int(v) > 0
    }
    if not exec_alloc:
        continue

    # Boundary total votes per gauge (includes ours)
    bndry_votes = {
        str(g).lower(): float(v or 0)
        for g, v in live.execute(
            "SELECT lower(gauge_address), votes_raw FROM boundary_gauge_values "
            "WHERE epoch=? AND active_only=1",
            (epoch,),
        ).fetchall()
    }

    # Gauge → pool address
    gauge_to_pool = {
        str(g).lower(): str(p or g).lower()
        for g, p in live.execute(
            "SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)) "
            "FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
            (epoch,),
        ).fetchall()
    }

    # Bribe rewards at boundary (T-1 preboundary snapshot — closest-to-boundary onchain data)
    bribe_usd = {
        str(g).lower(): float(r or 0)
        for g, r in pre.execute(
            "SELECT lower(gauge_address), rewards_now_usd FROM preboundary_snapshots "
            "WHERE epoch=? AND decision_window='T-1'",
            (epoch,),
        ).fetchall()
    }

    # Per-pool calculation
    epoch_realized = 0.0
    epoch_n = 0
    for gauge, our_v in exec_alloc.items():
        total_v  = bndry_votes.get(gauge, 0.0)
        others_v = max(0.0, total_v - our_v)
        rew      = bribe_usd.get(gauge, 0.0)
        pool     = gauge_to_pool.get(gauge, gauge)

        realized = expected_return_usd(rew, others_v, float(our_v))
        roi_1k   = (realized / our_v * 1000) if our_v > 0 else 0.0

        all_rows.append((epoch, epoch_dt, pool, gauge, our_v, others_v, total_v, rew, realized, roi_1k))
        epoch_realized += realized
        epoch_n += 1

    print(f"  {epoch_dt} (epoch={epoch})  pools={epoch_n}  "
          f"run_expected=${run_expected:.2f}  realized=${epoch_realized:.2f}  "
          f"delta=${epoch_realized - run_expected:.2f}")

print()

# ── 3. Per-pool summary: pools voted on ≥2 epochs
from collections import defaultdict

pool_epochs = defaultdict(list)
for row in all_rows:
    epoch, epoch_dt, pool, gauge, our_v, others_v, total_v, rew, realized, roi_1k = row
    pool_epochs[pool].append(row)

# Filter: pools with ≥2 appearances, sort by total realized descending
multi_epoch_pools = {p: v for p, v in pool_epochs.items() if len(v) >= 2}
print(f"Pools voted on ≥2 epochs: {len(multi_epoch_pools)}\n")

# Sort pools by total realized USD across all appearances
pool_summary = []
for pool, rows in multi_epoch_pools.items():
    total_realized = sum(r[8] for r in rows)
    avg_roi_1k = sum(r[9] for r in rows) / len(rows)
    avg_others = sum(r[5] for r in rows) / len(rows)
    total_our   = sum(r[4] for r in rows)
    pool_summary.append((pool, len(rows), total_realized, avg_roi_1k, avg_others, total_our, rows))
pool_summary.sort(key=lambda x: x[2], reverse=True)

# ── 4. Print per-pool trend table
header = (
    f"{'Epoch':>10}  {'OurVotes':>9}  {'OtherVotes':>11}  "
    f"{'BribeUSD':>9}  {'Realized':>9}  {'ROI/1k':>7}  "
    f"{'OtherVotes_delta':>17}"
)

for pool, n_epochs, total_realized, avg_roi_1k, avg_others, total_our, rows in pool_summary:
    pool_short = pool[:42]
    print(f"{'='*90}")
    print(f"Pool: {pool_short}  |  epochs_voted={n_epochs}  total_realized=${total_realized:.2f}  avg_roi/1k=${avg_roi_1k:.3f}")
    print(header)
    print("-" * len(header))
    prev_others = None
    for row in sorted(rows, key=lambda r: r[0]):
        epoch, epoch_dt, _pool, gauge, our_v, others_v, total_v, rew, realized, roi_1k = row
        if prev_others is not None:
            delta = others_v - prev_others
            delta_str = f"{delta:+,.0f}"
        else:
            delta_str = "—"
        prev_others = others_v
        print(
            f"  {epoch_dt:>10}  {our_v:>9,.0f}  {others_v:>11,.0f}  "
            f"{rew:>9,.2f}  {realized:>9,.2f}  {roi_1k:>7.3f}  "
            f"{delta_str:>17}"
        )
    print()

# ── 5. Worst dilution events: single-epoch drops
print(f"\n{'='*90}")
print("WORST DILUTION EVENTS (single epoch, realized < $5, bribe > $20)")
print(f"{'Epoch':>10}  {'Pool':>44}  {'OurVotes':>9}  {'OtherVotes':>11}  "
      f"{'BribeUSD':>9}  {'Realized':>8}  {'ROI/1k':>7}")
print("-" * 105)
worst = [r for r in all_rows if r[8] < 5.0 and r[7] > 20.0]
worst.sort(key=lambda r: r[8])
for row in worst:
    epoch, epoch_dt, pool, gauge, our_v, others_v, total_v, rew, realized, roi_1k = row
    print(
        f"  {epoch_dt:>10}  {pool[:44]:>44}  {our_v:>9,.0f}  {others_v:>11,.0f}  "
        f"{rew:>9,.2f}  {realized:>8,.2f}  {roi_1k:>7.3f}"
    )

# ── 6. Summary: average dilution by pool across epochs
print(f"\n{'='*90}")
print("POOL CONSISTENCY RANKING (pools voted ≥2 epochs, sorted by avg ROI/1k votes)")
print(f"{'Pool':>44}  {'Epochs':>6}  {'TotalRealized':>14}  {'AvgROI/1k':>10}  "
      f"{'AvgOtherVotes':>14}  {'OtherVotes_trend':>16}")
print("-" * 115)
for pool, n_epochs, total_realized, avg_roi_1k, avg_others, total_our, rows in pool_summary:
    rows_s = sorted(rows, key=lambda r: r[0])
    first_others = rows_s[0][5]
    last_others  = rows_s[-1][5]
    trend = last_others - first_others
    trend_str = f"{trend:+,.0f}"
    print(
        f"  {pool[:44]:>44}  {n_epochs:>6}  ${total_realized:>13,.2f}  "
        f"${avg_roi_1k:>9.3f}  {avg_others:>14,.0f}  {trend_str:>16}"
    )
