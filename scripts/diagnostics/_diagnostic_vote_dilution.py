#!/usr/bin/env python3
"""Diagnostic: analyse per-pool vote dilution between T-1 and boundary for a given run."""

import sqlite3

EPOCH = 1775692800
RUN_ID = 54

conn = sqlite3.connect("data/db/data.db")
pre = sqlite3.connect("data/db/preboundary_dev.db")
cur = conn.cursor()
pre_cur = pre.cursor()

# Executed allocation
cur.execute(
    """SELECT lower(gauge_address), executed_votes
       FROM executed_allocations
       WHERE epoch=? AND tx_hash=(SELECT tx_hash FROM auto_vote_runs WHERE id=?)""",
    (EPOCH, RUN_ID),
)
exec_alloc = {r[0]: r[1] for r in cur.fetchall()}

# Boundary votes per gauge
cur.execute(
    "SELECT lower(gauge_address), CAST(votes_raw AS REAL) FROM boundary_gauge_values WHERE epoch=? AND active_only=1",
    (EPOCH,),
)
boundary_votes = {r[0]: r[1] for r in cur.fetchall()}

# Token prices: use latest historical price at or before the epoch boundary
cur.execute(
    """SELECT lower(token_address), usd_price
       FROM historical_token_prices h
       WHERE timestamp = (
           SELECT MAX(h2.timestamp) FROM historical_token_prices h2
           WHERE lower(h2.token_address) = lower(h.token_address)
             AND h2.timestamp <= ?
       )""",
    (EPOCH,),
)
price_map = {r[0]: r[1] for r in cur.fetchall()}

# Fallback: current token_prices table
cur.execute("SELECT lower(token_address), usd_price FROM token_prices")
for r in cur.fetchall():
    if r[0] not in price_map:
        price_map[r[0]] = r[1]

# Boundary rewards: compute from rewards_raw * token price
cur.execute(
    """SELECT lower(gauge_address), lower(reward_token), rewards_raw, token_decimals
       FROM boundary_reward_snapshots WHERE epoch=? AND active_only=1""",
    (EPOCH,),
)
boundary_rewards: dict = {}
for gauge, token, raw, decimals in cur.fetchall():
    if not raw:
        continue
    try:
        dec = int(decimals) if decimals else 18
        amount = int(str(raw)) / (10 ** max(0, dec))
    except Exception:
        continue
    price = float(price_map.get(token, 0.0))
    if price <= 0:
        continue
    boundary_rewards[gauge] = boundary_rewards.get(gauge, 0.0) + amount * price

# T-1 votes per gauge (excluding our votes — these are pre-our-vote totals)
pre_cur.execute(
    "SELECT lower(gauge_address), CAST(votes_now_raw AS REAL) FROM preboundary_snapshots WHERE epoch=? AND decision_window='T-1'",
    (EPOCH,),
)
t1_votes = {r[0]: r[1] for r in pre_cur.fetchall()}

rows = []
for gauge, our_votes in exec_alloc.items():
    t1_others = t1_votes.get(gauge, 0)        # other voters' votes at T-1
    bnd_others = boundary_votes.get(gauge, 0) # other voters' votes at boundary
    reward = boundary_rewards.get(gauge, 0.0)

    # Both t1 and boundary votes_raw exclude our escrow — add ours back for total
    total_t1 = t1_others + our_votes
    total_bnd = bnd_others + our_votes

    third_party_new_votes = bnd_others - t1_others   # positive = more voters piled in
    vote_growth_pct = (third_party_new_votes / total_t1 * 100) if total_t1 > 0 else 0

    share_t1 = our_votes / total_t1 if total_t1 > 0 else 0
    share_bnd = our_votes / total_bnd if total_bnd > 0 else 0

    est_reward_t1 = reward * share_t1
    est_reward_bnd = reward * share_bnd
    dilution_loss = est_reward_t1 - est_reward_bnd  # positive = we lost money to dilution

    rows.append(
        dict(
            gauge=gauge,
            our_votes=our_votes,
            t1_others=t1_others,
            bnd_others=bnd_others,
            third_party_new_votes=third_party_new_votes,
            vote_growth_pct=vote_growth_pct,
            share_t1_pct=share_t1 * 100,
            share_bnd_pct=share_bnd * 100,
            reward_usd=reward,
            est_reward_t1=est_reward_t1,
            est_reward_bnd=est_reward_bnd,
            dilution_loss=dilution_loss,
        )
    )

rows.sort(key=lambda x: -x["dilution_loss"])

hdr = f"{'Pool':<16} {'OurVotes':>9} {'T-1 Others':>11} {'Bnd Others':>11} {'3rdParty+':>10} {'Grwth%':>7} {'Sh@T1%':>7} {'Sh@Bnd%':>8} {'Rwd$':>8} {'EstT1$':>7} {'EstBnd$':>8} {'DilLoss$':>9}"
print(hdr)
print("-" * len(hdr))
for r in rows:
    print(
        f"{r['gauge'][:16]} {r['our_votes']:>9,.0f} {r['t1_others']:>11,.0f} {r['bnd_others']:>11,.0f} "
        f"{r['third_party_new_votes']:>+9,.0f} {r['vote_growth_pct']:>+6.1f}% "
        f"{r['share_t1_pct']:>6.1f}% {r['share_bnd_pct']:>7.1f}% "
        f"{r['reward_usd']:>8.2f} {r['est_reward_t1']:>7.2f} {r['est_reward_bnd']:>8.2f} {r['dilution_loss']:>9.2f}"
    )

total_t1 = sum(r["est_reward_t1"] for r in rows)
total_bnd = sum(r["est_reward_bnd"] for r in rows)
total_loss = sum(r["dilution_loss"] for r in rows)

print()
print(f"Pools voted:                         {len(rows)}")
print(f"Est. reward using T-1 shares:        ${total_t1:,.2f}")
print(f"Est. reward using boundary shares:   ${total_bnd:,.2f}  (actual reported: $310.85)")
print(f"Dilution loss (T-1 → boundary):      ${total_loss:,.2f}")
print()

# Pools where 3rd-party votes grew most aggressively
print("=== Top 10 pools by dilution loss ===" )
for r in rows[:10]:
    print(
        f"  {r['gauge'][:20]}  3rdParty+votes={r['third_party_new_votes']:>+10,.0f} ({r['vote_growth_pct']:>+6.1f}%)  "
        f"share: {r['share_t1_pct']:.1f}% → {r['share_bnd_pct']:.1f}%  "
        f"dilution_loss=${r['dilution_loss']:.2f}"
    )

conn.close()
pre.close()
