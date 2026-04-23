"""Check voting power used per epoch vs current (1,774,908 EP)"""
import sqlite3

DB = "data/db/data.db"
RUNS = [8, 15, 33, 36, 45, 54, 61]
EPOCHS = [1772668800, 1773273600, 1773878400, 1774483200, 1775088000, 1775692800, 1776297600]
EXEC_REALIZED = [786.37, 530.38, 572.57, 496.26, 304.82, 598.51, 544.47]
T1_REAL = [1856.44, 1452.71, 1033.91, 1522.13, 617.54, 898.80, 541.34]
BDRY_OPT = [2495.39, 2080.13, 1465.15, 2066.73, 908.85, 1051.95, 577.72]

db = sqlite3.connect(DB)

CURRENT_VP = 1_774_908
print("{:<12}  {:>7}  {:>14}  {:>10}  {:>12}  {:>12}  {:>13}  {:>11}".format(
    "Epoch", "RunID", "ActualVotes", "Pools",
    "ExecReal", "ExecNorm@CurVP", "T1Real@CurVP", "Gap(T1-Exec)%"))
print("-" * 112)

for run_id, epoch, exec_real, t1_real, bdry_opt in zip(RUNS, EPOCHS, EXEC_REALIZED, T1_REAL, BDRY_OPT):
    tag = "auto_voter_run_{}".format(run_id)
    row = db.execute(
        "SELECT SUM(executed_votes), COUNT(*) FROM executed_allocations WHERE strategy_tag=? AND epoch=?",
        (tag, epoch),
    ).fetchone()
    actual_votes = row[0] or 1
    pools = row[1] or 0

    # Scale exec_real to what it would be at current VP (linear approximation)
    scale = CURRENT_VP / actual_votes
    exec_norm = exec_real * scale

    gap = (t1_real - exec_norm) / t1_real * 100

    print("{:<12}  {:>7}  {:>14,}  {:>10}  ${:>11.2f}  ${:>11.2f}  ${:>11.2f}  {:>+11.1f}%".format(
        epoch, run_id, int(actual_votes), pools,
        exec_real, exec_norm, t1_real, gap,
    ))

db.close()
