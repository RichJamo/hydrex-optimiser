#!/usr/bin/env python3
"""Check data availability for multi-epoch dilution analysis."""
import sqlite3
import datetime

conn = sqlite3.connect("data/db/data.db")

print("=== Epochs with executed allocations + matched run ===")
rows = conn.execute("""
    SELECT ea.epoch,
           COUNT(DISTINCT ea.gauge_address) as pools,
           MIN(CAST(REPLACE(ea.strategy_tag,'auto_voter_run_','') AS INTEGER)) as run_id,
           avr.vote_sent_at,
           avr.expected_return_usd
    FROM executed_allocations ea
    JOIN auto_vote_runs avr
        ON ea.strategy_tag = 'auto_voter_run_' || avr.id
        AND avr.status = 'tx_success'
    GROUP BY ea.epoch
    ORDER BY ea.epoch DESC
    LIMIT 20
""").fetchall()
for r in rows:
    dt = datetime.datetime.utcfromtimestamp(int(r[0])).strftime("%Y-%m-%d")
    print(f"  epoch={r[0]} ({dt})  pools={r[1]}  run_id={r[2]}  expected=${float(r[4] or 0):.2f}")

print()
print("=== Epochs with boundary_gauge_values (votes) ===")
rows2 = conn.execute("""
    SELECT epoch, COUNT(*) as gauges
    FROM boundary_gauge_values WHERE active_only=1 AND votes_raw > 0
    GROUP BY epoch ORDER BY epoch DESC LIMIT 15
""").fetchall()
for r in rows2:
    dt = datetime.datetime.utcfromtimestamp(int(r[0])).strftime("%Y-%m-%d")
    print(f"  epoch={r[0]} ({dt})  gauges_with_votes={r[1]}")

print()
print("=== onchain rewarddata coverage (boundary_reward_samples) ===")
rows3 = conn.execute("""
    SELECT epoch, COUNT(*) as rows
    FROM boundary_reward_samples
    GROUP BY epoch ORDER BY epoch DESC LIMIT 10
""").fetchall()
for r in rows3:
    dt = datetime.datetime.utcfromtimestamp(int(r[0])).strftime("%Y-%m-%d")
    print(f"  epoch={r[0]} ({dt})  rows={r[1]}")
