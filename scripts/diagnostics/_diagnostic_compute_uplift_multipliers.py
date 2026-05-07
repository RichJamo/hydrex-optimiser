"""
_diagnostic_compute_uplift_multipliers.py

Compute data-driven competition uplift multipliers by comparing:
  - T-1 vote snapshot (preboundary_dev.db preboundary_snapshots, decision_window='T-1')
  - Actual boundary votes (data.db boundary_gauge_values)

Groups pools by T-1 vote tier to derive tier-based uplift percentiles
suitable for use in analysis/recommender.py.
"""
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LIVE_DB = "data/db/data.db"
PRE_DB  = "data/db/preboundary_dev.db"

live = sqlite3.connect(LIVE_DB)
pre  = sqlite3.connect(PRE_DB)

# Epochs present in both DBs
bgv_epochs = set(
    r[0] for r in live.execute(
        "SELECT DISTINCT epoch FROM boundary_gauge_values WHERE active_only=1"
    ).fetchall()
)
t1_epochs = set(
    r[0] for r in pre.execute(
        "SELECT DISTINCT epoch FROM preboundary_snapshots WHERE decision_window='T-1'"
    ).fetchall()
)
overlap = sorted(bgv_epochs & t1_epochs)
print(f"Overlapping epochs: {len(overlap)} (spanning {len(overlap)} weeks)")

# For each overlapping epoch, compare T-1 votes vs actual boundary votes
rows = []  # (epoch, gauge, t1_votes, boundary_votes, uplift_ratio)

for epoch in overlap:
    t1_snap = {
        str(g).lower(): float(v or 0)
        for g, v in pre.execute(
            "SELECT lower(gauge_address), votes_now_raw FROM preboundary_snapshots "
            "WHERE epoch=? AND decision_window='T-1'",
            (epoch,),
        ).fetchall()
    }
    bndry = {
        str(g).lower(): float(v or 0)
        for g, v in live.execute(
            "SELECT lower(gauge_address), votes_raw FROM boundary_gauge_values "
            "WHERE epoch=? AND active_only=1",
            (epoch,),
        ).fetchall()
    }
    for gauge in set(t1_snap) & set(bndry):
        t1 = t1_snap[gauge]
        b  = bndry[gauge]
        if t1 < 10_000 or b <= 0:
            continue
        ratio = b / t1
        rows.append((epoch, gauge, t1, b, ratio))

print(f"Total (epoch, gauge) pairs with valid data: {len(rows)}\n")

# ── Tier-based analysis ──────────────────────────────────────────────────────
import statistics
from collections import defaultdict

TIERS = [
    ("< 100k",         0,           100_000),
    ("100k – 500k",    100_000,     500_000),
    ("500k – 2M",      500_000,   2_000_000),
    ("2M – 5M",      2_000_000,   5_000_000),
    ("5M – 15M",     5_000_000,  15_000_000),
    ("> 15M",       15_000_000, float("inf")),
]

print(f"{'Tier':<20} {'N':>5} {'median_ratio':>14} {'p75_ratio':>12} {'p90_ratio':>12}  {'median_%uplift':>16}")
print("─" * 85)

tier_stats = {}
for label, lo, hi in TIERS:
    ratios = [r for _, _, t1, _, r in rows if lo <= t1 < hi]
    if not ratios:
        continue
    ratios_sorted = sorted(ratios)
    n = len(ratios_sorted)
    med  = statistics.median(ratios_sorted)
    p75  = ratios_sorted[int(n * 0.75)]
    p90  = ratios_sorted[int(n * 0.90)]
    pct  = (med - 1.0) * 100
    tier_stats[label] = {"median": med, "p75": p75, "p90": p90, "lo": lo, "hi": hi, "n": n}
    print(f"  {label:<18} {n:>5}  {med:>14.3f}  {p75:>12.3f}  {p90:>12.3f}   {pct:>+14.1f}%")

# ── Per-gauge uplift for pools with >3 observations ─────────────────────────
print("\n\n── Per-gauge uplift (pools with ≥3 epochs of data, avg T-1 > 500k) ──")
from collections import defaultdict

gauge_ratios = defaultdict(list)
gauge_t1 = defaultdict(list)
for _, g, t1, b, r in rows:
    gauge_ratios[g].append(r)
    gauge_t1[g].append(t1)

# Pool address lookup
pool_map = {
    str(g).lower(): str(p).lower()
    for g, p in live.execute(
        "SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)) "
        "FROM boundary_gauge_values WHERE active_only=1"
    ).fetchall()
}

print(f"{'Gauge (short)':<16} {'N':>4} {'avg_t1_M':>10} {'med_ratio':>12} {'p75_ratio':>12}  {'interpreted_as':>30}")
print("─" * 90)

high_uplift = []
for g, ratios in sorted(gauge_ratios.items(), key=lambda x: -statistics.median(x[1])):
    if len(ratios) < 3:
        continue
    avg_t1 = sum(gauge_t1[g]) / len(gauge_t1[g])
    if avg_t1 < 500_000:
        continue
    n = len(ratios)
    rs = sorted(ratios)
    med = statistics.median(rs)
    p75 = rs[int(n * 0.75)] if n >= 4 else rs[-1]
    pool = pool_map.get(g, g)
    print(f"  {g[:14]}  {n:>4}  {avg_t1/1e6:>9.2f}M  {med:>12.3f}  {p75:>12.3f}  → pool {pool[:30]}")
    high_uplift.append((g, avg_t1, med, p75))

# ── Recommended multiplier table ────────────────────────────────────────────
print("\n\n── RECOMMENDED COMPETITION_MULTIPLIER TABLE ──")
print("(Use these as the _competition_multiplier() lookup in recommender.py)\n")

print("  # Boundaries below are T-1 vote counts (what the optimizer observes).")
print("  # Multiplier = conservative p75 of observed boundary/T-1 ratio, rounded up.")
print("  # Tuned from", len(rows), "observations across", len(overlap), "epochs.\n")

for label, lo, hi in TIERS:
    if label not in tier_stats:
        continue
    s = tier_stats[label]
    # Use p75 as the "reasonably conservative" estimate (not p90 which is too punishing)
    # Round to nearest 0.05
    mult = round(s["p75"] / 0.05) * 0.05
    mult = max(mult, 1.0)
    lo_str = f"{lo/1e6:.1f}M" if lo >= 1_000_000 else (f"{lo//1000}k" if lo > 0 else "0")
    hi_str = f"{hi/1e6:.0f}M" if hi < float("inf") else "∞"
    hi_repr = str(hi) if hi != float("inf") else 'float("inf")'
    print(f"  # {label:<20}  n={s['n']:>4}  p75_ratio={s['p75']:.3f}  → multiplier={mult:.2f}")
    print(f"  if {lo} <= votes < {hi_repr}: return {mult}")

live.close()
pre.close()
