"""
Backtest summary: compare boundary_opt, T-1 realized, and executed_realized across all epochs.
Usage: venv/bin/python scripts/_diagnostic_backtest_summary.py
"""
import sqlite3

DB = "data/db/data.db"

EPOCHS = [1772668800, 1773273600, 1773878400, 1774483200, 1775088000, 1775692800, 1776297600]

# Results from preboundary_epoch_review.py run 2026-04-23
REVIEW = {
    1772668800: (2495.39, 1856.44),
    1773273600: (2080.13, 1452.71),
    1773878400: (1465.15, 1033.91),
    1774483200: (2066.73, 1522.13),
    1775088000: (908.85,  617.54),
    1775692800: (1051.95, 898.80),
    1776297600: (577.72,  541.34),
}

db = sqlite3.connect(DB)

HDR = "{:<12}  {:>9}  {:>12}  {:>7}  {:>7}  {:>13}  {:>10}  {:>10}"
ROW = "{:<12}  ${:>8.2f}  ${:>11.2f}  {:>6.1f}%  {:>7}  ${:>12.2f}  {:>+10.2f}  {:>+10.2f}"
SEP = "-" * 104

print(HDR.format("Epoch", "BdryOpt", "T1Real@Bdry", "Gap%", "RunID", "ExecRealized", "VsOpt", "VsT1Real"))
print(SEP)

total_opt = 0.0
total_t1 = 0.0
total_exec = 0.0

for epoch in EPOCHS:
    bdry_opt, t1_real = REVIEW[epoch]

    row = db.execute(
        """
        SELECT avr.id
        FROM epoch_boundaries eb
        JOIN auto_vote_runs avr
            ON avr.status = 'tx_success'
            AND avr.vote_sent_at >= eb.vote_epoch
            AND avr.vote_sent_at < :epoch
        WHERE eb.epoch = :epoch
        ORDER BY avr.vote_sent_at DESC LIMIT 1
        """,
        {"epoch": epoch},
    ).fetchone()

    if not row:
        print("{:<12}  ${:>8.2f}  ${:>11.2f}  {:>6.1f}%  (no run)".format(
            epoch, bdry_opt, t1_real, (bdry_opt - t1_real) / bdry_opt * 100
        ))
        continue

    run_id = row[0]
    strategy_tag = "auto_voter_run_{}".format(run_id)

    exec_rows = db.execute(
        """
        SELECT ea.executed_votes,
               bgv.votes_raw,
               SUM(
                   CAST(brs.rewards_raw AS REAL)
                   / POWER(10, COALESCE(brs.token_decimals, tm.decimals, 18))
                   * COALESCE(tp.usd_price, 0)
               ) AS pool_usd
        FROM executed_allocations ea
        JOIN boundary_gauge_values bgv
            ON lower(bgv.gauge_address) = lower(ea.gauge_address)
            AND bgv.epoch = ea.epoch
            AND bgv.active_only = 1
        LEFT JOIN boundary_reward_snapshots brs
            ON lower(brs.gauge_address) = lower(ea.gauge_address)
            AND brs.epoch = ea.epoch
            AND brs.active_only = 1
        LEFT JOIN token_prices tp ON lower(tp.token_address) = lower(brs.reward_token)
        LEFT JOIN token_metadata tm ON lower(tm.token_address) = lower(brs.reward_token)
        WHERE ea.epoch = ? AND ea.strategy_tag = ?
        GROUP BY ea.gauge_address, ea.executed_votes, bgv.votes_raw
        """,
        (epoch, strategy_tag),
    ).fetchall()

    exec_realized = 0.0
    for our_votes, bdry_votes_raw, pool_usd in exec_rows:
        if not our_votes or not bdry_votes_raw or not pool_usd:
            continue
        base_votes = max(0.0, float(bdry_votes_raw) - float(our_votes))
        denom = base_votes + float(our_votes)
        if denom > 0:
            exec_realized += (float(our_votes) / denom) * float(pool_usd)

    gap_pct = (bdry_opt - t1_real) / bdry_opt * 100
    vs_opt = exec_realized - bdry_opt
    vs_t1 = exec_realized - t1_real

    print(ROW.format(epoch, bdry_opt, t1_real, gap_pct, run_id, exec_realized, vs_opt, vs_t1))
    total_opt += bdry_opt
    total_t1 += t1_real
    total_exec += exec_realized

print(SEP)
print("{:<12}  ${:>8.2f}  ${:>11.2f}  {:>7}  {:>7}  ${:>12.2f}  {:>+10.2f}  {:>+10.2f}".format(
    "TOTAL", total_opt, total_t1, "", "", total_exec,
    total_exec - total_opt, total_exec - total_t1,
))
print()
print("Notes:")
print("  BdryOpt       = perfect-hindsight sweep (excl. our votes from base)")
print("  T1Real@Bdry   = T-1 strategy evaluated at actual boundary prices")
print("  ExecRealized  = our actual allocation evaluated at actual boundary prices")
print("  VsOpt         = ExecRealized minus BdryOpt  (should be <=0)")
print("  VsT1Real      = ExecRealized minus T1Real   (should be >=0 if we beat T-1)")

db.close()
