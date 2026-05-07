#!/usr/bin/env python3
"""
Per-pool expected vs realized breakdown for a given epoch's executed run.
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimizer import expected_return_usd

EPOCH = 1776297600
DB_PATH = "data/db/data.db"

conn = sqlite3.connect(DB_PATH)

# Executed run
exec_row = conn.execute(
    """
    SELECT id, expected_return_usd
    FROM auto_vote_runs
    WHERE status = 'tx_success'
      AND vote_sent_at >= 1775692800
      AND vote_sent_at < 1776297600
    ORDER BY vote_sent_at DESC
    LIMIT 1
    """
).fetchone()
run_id = int(exec_row[0])
sendtime_expected_total = float(exec_row[1] or 0)

exec_alloc_rows = conn.execute(
    """
    SELECT lower(gauge_address), executed_votes
    FROM executed_allocations
    WHERE epoch = ? AND strategy_tag = ?
    ORDER BY rank ASC
    """,
    (EPOCH, f"auto_voter_run_{run_id}"),
).fetchall()

# Gauge -> pool name
gauge_to_pool = {}
for g, p in conn.execute(
    "SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)) FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
    (EPOCH,),
).fetchall():
    gauge_to_pool[str(g)] = str(p)

# Boundary votes (total, including ours)
boundary_votes = {
    str(g): float(v or 0)
    for g, v in conn.execute(
        "SELECT lower(gauge_address), votes_raw FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
        (EPOCH,),
    ).fetchall()
}

# Preflight total votes check
our_votes_map = {str(g): int(v) for g, v in exec_alloc_rows}
total_our = sum(our_votes_map.values())

# Token prices — loaded early, needed for live snapshot USD computation
token_prices = {
    str(t): float(p or 0)
    for t, p in conn.execute(
        "SELECT lower(token_address), usd_price FROM token_prices WHERE usd_price > 0"
    ).fetchall()
}

# Pre-boundary rewards per gauge from the live snapshot used by auto_voter
# (snapshot closest to and before vote send time)
SEND_TS = 1776297593  # auto_voter run vote_sent_at
snap_ts = conn.execute(
    "SELECT MAX(snapshot_ts) FROM live_reward_token_samples WHERE vote_epoch=1775692800 AND snapshot_ts <= ?",
    (SEND_TS,),
).fetchone()[0]

sendtime_rewards_by_gauge: dict = {}
for gauge, token, norm in conn.execute(
    """
    SELECT lower(gauge_address), lower(reward_token), rewards_normalized
    FROM live_reward_token_samples
    WHERE vote_epoch = 1775692800 AND snapshot_ts = ?
    """,
    (snap_ts,),
).fetchall():
    g = str(gauge or "").lower()
    if not g:
        continue
    price = token_prices.get(str(token or "").lower(), 0.0)
    if price <= 0:
        continue
    sendtime_rewards_by_gauge[g] = sendtime_rewards_by_gauge.get(g, 0.0) + float(norm or 0) * price

# Pre-boundary votes per gauge: use boundary_gauge_values (boundary total, our votes not yet cast at send time)
live_votes_by_gauge: dict = {}
for gauge, votes in conn.execute(
    "SELECT lower(gauge_address), votes_raw FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
    (EPOCH,),
).fetchall():
    live_votes_by_gauge[str(gauge or "").lower()] = float(votes or 0)

# Compute per-gauge send-time expected using the corrected formula
# (base = live others' votes, which at send time = total live votes since we haven't voted yet)
sendtime_expected_by_gauge: dict = {}
for gauge, our_votes in exec_alloc_rows:
    our_v = float(our_votes)
    rew = sendtime_rewards_by_gauge.get(gauge, 0.0)
    # At send time, others' votes = total live votes (we hadn't voted yet)
    others_v_at_send = live_votes_by_gauge.get(gauge, 0.0)
    sendtime_expected_by_gauge[gauge] = expected_return_usd(rew, others_v_at_send, our_v)

# Boundary realized rewards per gauge
rewards_by_gauge: dict = {}
for gauge, token, rraw, dec, usd_price, total_usd in conn.execute(
    """
    SELECT lower(s.gauge_address), lower(s.reward_token), s.rewards_raw, s.token_decimals,
           COALESCE(s.usd_price,0), COALESCE(s.total_usd,0)
    FROM boundary_reward_snapshots s
    WHERE s.epoch=? AND s.active_only=1
    """,
    (EPOCH,),
).fetchall():
    g = str(gauge or "").lower()
    if not g:
        continue
    tusd = float(total_usd or 0)
    if tusd > 0:
        rewards_by_gauge[g] = rewards_by_gauge.get(g, 0.0) + tusd
        continue
    try:
        amt = float(int(str(rraw or "0"))) / (10 ** max(0, int(dec or 18)))
    except Exception:
        amt = 0.0
    if amt <= 0:
        continue
    price = float(usd_price or 0)
    if price <= 0:
        price = token_prices.get(str(token), 0.0)
    if price <= 0:
        continue
    rewards_by_gauge[g] = rewards_by_gauge.get(g, 0.0) + amt * price



# Print table
print(f"\nPer-pool expected vs realized: epoch {EPOCH} (run_id={run_id})\n")

col_pool   = 44
col_votes  = 10
col_exp    = 11
col_real   = 11
col_gap    = 9
col_pct    = 8
col_bribe  = 11
col_base   = 12
col_bdry   = 12

header = (
    f"{'Pool':>{col_pool}}  {'OurVotes':>{col_votes}}  "
    f"{'PoolBribes':>{col_bribe}}  {'OtherVotes':>{col_base}}  "
    f"{'BndryVotes':>{col_bdry}}  "
    f"{'Expected':>{col_exp}}  {'Realized':>{col_real}}  {'Gap':>{col_gap}}  {'Gap%':>{col_pct}}"
)
print(header)
print("-" * len(header))

exp_total = 0.0
real_total = 0.0

rows_out = []
for gauge, our_votes in exec_alloc_rows:
    our_v = float(our_votes)
    total_v = boundary_votes.get(gauge, 0.0)
    others_v = max(0.0, total_v - our_v)
    rew = rewards_by_gauge.get(gauge, 0.0)

    send_exp = sendtime_expected_by_gauge.get(gauge, 0.0)
    realized = expected_return_usd(rew, others_v, our_v)

    gap = realized - send_exp
    gap_pct = (gap / send_exp * 100) if send_exp > 0 else 0.0

    pool = gauge_to_pool.get(gauge, gauge)
    display = pool[:col_pool] if len(pool) > col_pool else pool

    rows_out.append((display, our_v, rew, others_v, total_v, send_exp, realized, gap, gap_pct))
    exp_total += send_exp
    real_total += realized

rows_out.sort(key=lambda x: x[5], reverse=True)

for display, our_v, rew, others_v, total_v, send_exp, realized, gap, gap_pct in rows_out:
    flag = " <--" if gap_pct < -10 else ""
    print(
        f"{display:>{col_pool}}  {our_v:>{col_votes},.0f}  "
        f"{rew:>{col_bribe},.2f}  {others_v:>{col_base},.0f}  "
        f"{total_v:>{col_bdry},.0f}  "
        f"{send_exp:>{col_exp},.2f}  {realized:>{col_real},.2f}  "
        f"{gap:>{col_gap},.2f}  {gap_pct:>{col_pct}.1f}%{flag}"
    )

print("-" * len(header))
overall_gap = real_total - exp_total
overall_gap_pct = (overall_gap / exp_total * 100) if exp_total > 0 else 0.0
print(
    f"{'TOTAL':>{col_pool}}  {total_our:>{col_votes},.0f}  "
    f"{'':>{col_bribe}}  {'':>{col_base}}  {'':>{col_bdry}}  "
    f"{exp_total:>{col_exp},.2f}  {real_total:>{col_real},.2f}  "
    f"{overall_gap:>{col_gap},.2f}  {overall_gap_pct:>{col_pct}.1f}%"
)
print()
print(f"  Note: 'Expected' = auto_voter expected at vote send time (using pre-boundary state)")
print(f"        'Realized' = corrected formula using final boundary state (others' votes as base)")
print(f"        Negative gap = other voters moved into your pools after you voted (dilution)")
