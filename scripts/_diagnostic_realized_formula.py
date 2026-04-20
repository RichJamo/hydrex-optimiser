#!/usr/bin/env python3
"""
Diagnostic: compare pipeline's executed_realized formula vs corrected formula.
Shows per-gauge breakdown for epoch 1776297600 executed run.
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
sendtime_expected = float(exec_row[1] or 0)
print(f"Executed run_id={run_id}, send-time expected=${sendtime_expected:.2f}")

exec_alloc_rows = conn.execute(
    """
    SELECT lower(gauge_address), executed_votes
    FROM executed_allocations
    WHERE epoch = ? AND strategy_tag = ?
    ORDER BY rank ASC
    """,
    (EPOCH, f"auto_voter_run_{run_id}"),
).fetchall()
print(f"Executed allocation rows: {len(exec_alloc_rows)}")
print(f"Total our votes: {sum(int(v) for _, v in exec_alloc_rows):,}")
print()

# Boundary votes (total, includes ours)
boundary_votes = {
    str(g): float(v or 0)
    for g, v in conn.execute(
        "SELECT lower(gauge_address), votes_raw FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
        (EPOCH,),
    ).fetchall()
}

# Rewards per gauge — same way the pipeline builds rewards_usd_by_gauge
token_prices = {
    str(t): float(p or 0)
    for t, p in conn.execute(
        "SELECT lower(token_address), usd_price FROM token_prices WHERE usd_price > 0"
    ).fetchall()
}

rewards_by_gauge: dict = {}
for gauge, token, rraw, dec, usd_price, total_usd, sym in conn.execute(
    """
    SELECT lower(s.gauge_address), lower(s.reward_token), s.rewards_raw, s.token_decimals,
           COALESCE(s.usd_price,0), COALESCE(s.total_usd,0), COALESCE(m.symbol,'')
    FROM boundary_reward_snapshots s
    LEFT JOIN token_metadata m ON lower(m.token_address)=lower(s.reward_token)
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

print(f"Rewards nonzero gauges: {sum(1 for v in rewards_by_gauge.values() if v > 0)}")
print(f"Sum of all reward snapshots: ${sum(rewards_by_gauge.values()):.2f}")
print()

# Compare formulas
header = (
    f"{'Gauge':44} {'OurVotes':>10} {'TotalVotes':>12} "
    f"{'OthersVotes':>12} {'Rewards':>9} {'Pipeline':>9} {'Corrected':>10}"
)
print(header)
print("-" * len(header))

cur_total = 0.0
corr_total = 0.0

for gauge, our_votes in exec_alloc_rows:
    our_v = float(our_votes)
    total_v = boundary_votes.get(gauge, 0.0)
    others_v = max(0.0, total_v - our_v)
    rew = rewards_by_gauge.get(gauge, 0.0)

    # Pipeline uses total_v as base (includes our votes — double-counts them)
    pipeline_val = expected_return_usd(rew, total_v, our_v)
    # Corrected: subtract our votes from total to get others
    corrected_val = expected_return_usd(rew, others_v, our_v)

    cur_total += pipeline_val
    corr_total += corrected_val

    flag = " !!!" if abs(corrected_val - pipeline_val) > 1 else ""
    print(
        f"{gauge:44} {our_v:>10,.0f} {total_v:>12,.0f} "
        f"{others_v:>12,.0f} {rew:>9.2f} {pipeline_val:>9.2f} {corrected_val:>10.2f}{flag}"
    )

print()
print(f"  Pipeline total (base = total boundary votes):  ${cur_total:.2f}")
print(f"  Corrected total (base = others-only votes):    ${corr_total:.2f}")
print(f"  Difference:                                    ${corr_total - cur_total:.2f}")
print(f"  Send-time expected (auto_voter):               ${sendtime_expected:.2f}")
print()
print("EXPLANATION:")
print("  The pipeline sets base_votes = boundary_gauge_values.votes_raw (total votes).")
print("  But expected_return_usd(total_usd, base, ours) treats 'base' as OTHERS' votes.")
print("  So denom = total + ours = others + ours + ours -> our votes counted twice.")
print("  Corrected: base_votes = total_boundary_votes - our_votes (others only).")
