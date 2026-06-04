#!/usr/bin/env python3
"""
Two-part late-bribe analysis.

PART 1 – Narrow window
  For each epoch, compare the bribe state at our query_block (when the
  optimizer ran) vs the final state captured in boundary_reward_snapshots
  (at the actual flip).  Delta = bribes deposited after our snapshot but
  before the flip — money we couldn't have voted for.

PART 2 – Allocation decision
  Given the final bribe state (boundary_reward_snapshots) and the final
  vote state (boundary_gauge_values), compute the optimizer-optimal
  allocation and compare it to what we actually executed.  This reveals
  whether we were sub-optimal with the information we had PLUS any
  narrow-window additions.

  The two sources of "missed return" are therefore:
    A = narrow-window bribes (Part 1)   – structurally unactionable
    B = allocation sub-optimality       – potentially fixable

NOTE: For epoch 1778112000 (May 7-14) the single vote run fired 7 hours
before the flip; its "narrow window" is really a 7-hour window.  Output
flags this with window_mins so you can weight it accordingly.

Usage:
  python -m scripts.diagnostics._diagnostic_late_bribe_analysis [--epochs N]
"""
import argparse
import csv
import os
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from dotenv import load_dotenv
from multicall import Call, Multicall
from web3 import Web3

load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from config.settings import DATABASE_PATH
from src.optimizer import solve_marginal_allocation

ONE_E18 = 10 ** 18
RPC_URL = os.getenv("RPC_URL", "https://mainnet.base.org")
OUR_VOTING_POWER = 1_774_908   # veHYDX — update if escrow changes


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Data-loading helpers
# ---------------------------------------------------------------------------

@dataclass
class EpochRun:
    vote_epoch: int          # epoch being voted for
    eb_epoch: int            # epoch_boundaries.epoch (flip timestamp)
    boundary_block: int      # flip block (may be off for some epochs)
    query_block: int         # block at which we queried bribe data
    query_ts: int            # vote_sent_at unix timestamp
    flip_ts: int             # eb_epoch (the flip unix timestamp)
    window_secs: int         # flip_ts - query_ts  (how late we voted)
    run_id: int
    strategy_tag: str


def load_epochs(conn: sqlite3.Connection, n: int) -> List[EpochRun]:
    """
    Return the last n epochs that have a completed vote run, oldest first.
    Takes the highest query_block run per vote_epoch (the most information-rich).
    """
    rows = conn.execute(
        """
        SELECT avr.vote_epoch, eb.epoch, eb.boundary_block,
               avr.query_block, avr.vote_sent_at, avr.id
        FROM auto_vote_runs avr
        JOIN epoch_boundaries eb ON eb.vote_epoch = avr.vote_epoch
        WHERE avr.status = 'tx_success' AND avr.dry_run = 0
          AND avr.query_block = (
              SELECT MAX(a2.query_block)
              FROM auto_vote_runs a2
              WHERE a2.vote_epoch = avr.vote_epoch
                AND a2.status = 'tx_success' AND a2.dry_run = 0
          )
        ORDER BY avr.vote_sent_at DESC
        LIMIT ?
        """,
        (n,),
    ).fetchall()

    result = []
    for vote_epoch, eb_epoch, boundary_block, query_block, query_ts, run_id in rows:
        window_secs = eb_epoch - query_ts

        # Look up matching strategy_tag from executed_allocations
        tag_row = conn.execute(
            "SELECT strategy_tag FROM executed_allocations WHERE epoch = ? LIMIT 1",
            (vote_epoch,),
        ).fetchone()
        strategy_tag = tag_row[0] if tag_row else ""

        result.append(
            EpochRun(
                vote_epoch=vote_epoch,
                eb_epoch=eb_epoch,
                boundary_block=boundary_block,
                query_block=query_block,
                query_ts=query_ts,
                flip_ts=eb_epoch,
                window_secs=window_secs,
                run_id=run_id,
                strategy_tag=strategy_tag,
            )
        )
    return list(reversed(result))  # oldest first


def load_final_bribe_per_gauge(conn: sqlite3.Connection, eb_epoch: int) -> Dict[str, float]:
    """Sum total_usd across all bribe contracts and tokens, keyed by gauge_address."""
    rows = conn.execute(
        "SELECT gauge_address, SUM(total_usd) FROM boundary_reward_snapshots WHERE epoch = ? GROUP BY gauge_address",
        (eb_epoch,),
    ).fetchall()
    return {g.lower(): float(u or 0) for g, u in rows}


def load_final_bribe_pairs(conn: sqlite3.Connection, eb_epoch: int) -> List[Tuple]:
    """
    Return (gauge, bribe_contract, reward_token, rewards_raw_int, total_usd, usd_price, token_decimals)
    for all non-zero entries at the final snapshot.
    rewards_raw_int is the raw on-chain integer (not yet divided by decimals).
    total_usd = (rewards_raw_int / 10**decimals) * usd_price  (already computed correctly in DB).
    """
    rows = conn.execute(
        """
        SELECT gauge_address, bribe_contract, reward_token,
               CAST(rewards_raw AS INTEGER), total_usd, usd_price,
               COALESCE(token_decimals, 18)
        FROM boundary_reward_snapshots
        WHERE epoch = ? AND total_usd > 0
        """,
        (eb_epoch,),
    ).fetchall()
    return [(g.lower(), b.lower(), r.lower(), int(rr), float(u), float(p or 0), int(dec))
            for g, b, r, rr, u, p, dec in rows]


def load_final_votes_per_gauge(conn: sqlite3.Connection, eb_epoch: int) -> Dict[str, float]:
    """Total votes per gauge at the flip (boundary_gauge_values)."""
    rows = conn.execute(
        """
        SELECT gauge_address, CAST(votes_raw AS REAL)
        FROM boundary_gauge_values
        WHERE epoch = ? AND active_only = 1 AND votes_raw > 0
        """,
        (eb_epoch,),
    ).fetchall()
    return {g.lower(): float(v) for g, v in rows}


def load_our_allocation(conn: sqlite3.Connection, vote_epoch: int, strategy_tag: str) -> Dict[str, int]:
    """Our executed votes per gauge for the given strategy run."""
    rows = conn.execute(
        "SELECT gauge_address, executed_votes FROM executed_allocations WHERE epoch = ? AND strategy_tag = ?",
        (vote_epoch, strategy_tag),
    ).fetchall()
    return {g.lower(): int(v) for g, v in rows}


def load_gauge_to_pool(conn: sqlite3.Connection) -> Dict[str, str]:
    rows = conn.execute("SELECT address, pool FROM gauges WHERE pool IS NOT NULL").fetchall()
    return {g.lower(): p.lower() for g, p in rows}


# ---------------------------------------------------------------------------
# On-chain query
# ---------------------------------------------------------------------------

def query_rewards_at_block(
    w3: Web3,
    pairs: List[Tuple[str, str]],   # (bribe_contract, reward_token)
    vote_epoch: int,
    block: int,
    batch_size: int = 150,
) -> Dict[Tuple[str, str], int]:
    """
    Query rewardData(token, vote_epoch) at `block` for all pairs.
    Returns {(bribe, token): rewards_per_epoch_raw_int} — raw on-chain integer,
    same unit as boundary_reward_snapshots.rewards_raw.  Caller divides by
    10**token_decimals to get token amount.
    """
    results: Dict[Tuple[str, str], int] = {}
    total = (len(pairs) + batch_size - 1) // batch_size

    for start in range(0, len(pairs), batch_size):
        batch = pairs[start : start + batch_size]
        calls = [
            Call(
                Web3.to_checksum_address(bc),
                ["rewardData(address,uint256)((uint256,uint256,uint256))",
                 Web3.to_checksum_address(rt), vote_epoch],
                [(f"{bc}_{rt}", lambda ok, v: v if ok else None)],
            )
            for bc, rt in batch
        ]
        try:
            res = Multicall(calls, _w3=w3, block_id=block, require_success=False)()
        except Exception as e:
            print(f"      multicall error (batch {start//batch_size+1}/{total}): {e}")
            continue

        for bc, rt in batch:
            data = res.get(f"{bc}_{rt}")
            if data is None or isinstance(data, bool):
                continue
            if isinstance(data, (list, tuple)) and len(data) == 1:
                data = data[0]
            if isinstance(data, (list, tuple)) and len(data) == 3:
                _period_finish, rpe, _last_update = data
                if rpe and int(rpe) > 0:
                    results[(bc, rt)] = int(rpe)   # raw integer, same as DB rewards_raw

    return results


# ---------------------------------------------------------------------------
# Part 1: Narrow window
# ---------------------------------------------------------------------------

def analyze_narrow_window(
    run: EpochRun,
    final_pairs: List[Tuple],
    query_rewards: Dict[Tuple[str, str], int],
    gauge_to_pool: Dict[str, str],
) -> List[dict]:
    """
    Compare final bribe state vs query_block state per pool.

    final_pairs entries: (gauge, bc, rt, rr_final_raw, total_usd_final, price, decimals)
    query_rewards: {(bc, rt): raw_int}  — same units as rr_final_raw

    late_usd = total_usd_final - query_usd
    where query_usd = (rr_query_raw / 10**decimals) * price
    """
    delta_per_gauge: Dict[str, Tuple[float, float]] = {}  # gauge → (query_usd, final_usd)

    for gauge, bc, rt, rr_final_raw, total_usd_final, price, decimals in final_pairs:
        if price <= 0:
            continue
        rr_query_raw = query_rewards.get((bc, rt), 0)
        query_usd = (rr_query_raw / (10 ** decimals)) * price

        if gauge not in delta_per_gauge:
            delta_per_gauge[gauge] = (0.0, 0.0)
        q, f = delta_per_gauge[gauge]
        delta_per_gauge[gauge] = (q + query_usd, f + total_usd_final)

    rows = []
    for gauge, (query_usd, final_usd) in delta_per_gauge.items():
        late_usd = final_usd - query_usd
        if late_usd < 1.0:
            continue
        pool = gauge_to_pool.get(gauge, gauge)
        rows.append({
            "vote_epoch": run.vote_epoch,
            "epoch_date": fmt_ts(run.vote_epoch)[:10],
            "window_mins": round(run.window_secs / 60, 1),
            "gauge": gauge,
            "pool": pool,
            "query_usd": round(query_usd, 2),
            "final_usd": round(final_usd, 2),
            "late_usd": round(late_usd, 2),
            "late_pct": round(100 * late_usd / final_usd, 1) if final_usd > 0 else 0.0,
        })

    rows.sort(key=lambda r: -r["late_usd"])
    return rows


# ---------------------------------------------------------------------------
# Part 2: Allocation decision
# ---------------------------------------------------------------------------

def analyze_allocation_decision(
    run: EpochRun,
    final_bribe_per_gauge: Dict[str, float],
    final_votes_per_gauge: Dict[str, float],
    our_allocation: Dict[str, int],
    gauge_to_pool: Dict[str, str],
) -> List[dict]:
    """
    Re-run the optimizer on the final bribe + vote state and compare
    against what we actually executed.

    states tuple: (gauge, pool, base_votes_ex_ours, rewards_usd)
    base_votes_ex_ours = total_votes - our_votes (so the optimizer can re-add them).
    """
    from src.optimizer import expected_return_usd

    gauges = sorted(
        set(final_bribe_per_gauge) & set(final_votes_per_gauge),
        key=lambda g: -final_bribe_per_gauge.get(g, 0),
    )
    if not gauges:
        return []

    states = []
    for g in gauges:
        pool = gauge_to_pool.get(g, g)
        base_v = max(0.0, final_votes_per_gauge[g] - our_allocation.get(g, 0))
        states.append((g, pool, base_v, final_bribe_per_gauge[g]))

    try:
        optimal_alloc = solve_marginal_allocation(
            states=states,
            total_votes=OUR_VOTING_POWER,
            min_per_pool=0,
            max_selected_pools=max(1, len([s for s in states if s[3] > 0])),
            chunk_size=1000,
        )
    except Exception as e:
        print(f"      optimizer error for {run.vote_epoch}: {e}")
        return []

    rows = []
    for i, (gauge, pool, base_v, reward_usd) in enumerate(states):
        our_v = our_allocation.get(gauge, 0)
        opt_v = int(optimal_alloc[i])
        delta_v = opt_v - our_v

        our_ret = expected_return_usd(reward_usd, base_v, our_v)
        opt_ret = expected_return_usd(reward_usd, base_v, opt_v)
        gain = opt_ret - our_ret

        if abs(delta_v) < 1000 and abs(gain) < 1.0:
            continue

        rows.append({
            "vote_epoch": run.vote_epoch,
            "epoch_date": fmt_ts(run.vote_epoch)[:10],
            "gauge": gauge,
            "pool": pool,
            "final_bribe_usd": round(reward_usd, 2),
            "our_votes": our_v,
            "optimal_votes": opt_v,
            "delta_votes": delta_v,
            "our_expected_usd": round(our_ret, 2),
            "optimal_expected_usd": round(opt_ret, 2),
            "gain_usd": round(gain, 2),
        })

    rows.sort(key=lambda r: -abs(r["gain_usd"]))
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(n_epochs: int, out_narrow: str, out_alloc: str) -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    w3 = Web3(Web3.HTTPProvider(RPC_URL, request_kwargs={"timeout": 60}))
    gauge_to_pool = load_gauge_to_pool(conn)

    epochs = load_epochs(conn, n_epochs)
    print(f"Analysing {len(epochs)} epochs\n")

    all_narrow: List[dict] = []
    all_alloc: List[dict] = []

    for run_info in epochs:
        label = (
            f"Epoch {fmt_ts(run_info.vote_epoch)[:10]} "
            f"(window {run_info.window_secs}s"
            + ("  ⚠️  ANOMALOUS — 7h window" if run_info.window_secs > 3600 else "")
            + ")"
        )
        print(f"=== {label} ===")
        print(f"  query_block={run_info.query_block}  boundary_block={run_info.boundary_block}")

        final_pairs = load_final_bribe_pairs(conn, run_info.eb_epoch)
        final_bribe_per_gauge = load_final_bribe_per_gauge(conn, run_info.eb_epoch)
        final_votes = load_final_votes_per_gauge(conn, run_info.eb_epoch)
        our_alloc = load_our_allocation(conn, run_info.vote_epoch, run_info.strategy_tag)

        print(f"  Final snapshot: {len(final_pairs)} (bribe,token) pairs across "
              f"{len(final_bribe_per_gauge)} gauges  |  our allocation: {len(our_alloc)} gauges")

        # --- Part 1: narrow window ---
        pairs_to_query = [(b, r) for _, b, r, _, _, _, _ in final_pairs]
        print(f"  Querying {len(pairs_to_query)} pairs at query_block {run_info.query_block}...")
        query_rewards = query_rewards_at_block(w3, pairs_to_query, run_info.vote_epoch, run_info.query_block)

        narrow_rows = analyze_narrow_window(run_info, final_pairs, query_rewards, gauge_to_pool)
        total_late = sum(r["late_usd"] for r in narrow_rows)
        print(f"  Narrow window late bribes: ${total_late:.2f} across {len(narrow_rows)} pools")
        for r in narrow_rows:
            print(f"    {r['pool'][:42]:42s}  late ${r['late_usd']:>8.2f}  ({r['late_pct']:.0f}% of pool total)")
        all_narrow.extend(narrow_rows)

        # --- Part 2: allocation decision ---
        alloc_rows = analyze_allocation_decision(
            run_info, final_bribe_per_gauge, final_votes, our_alloc, gauge_to_pool
        )
        total_gain = sum(r["gain_usd"] for r in alloc_rows if r["gain_usd"] > 0)
        print(f"  Allocation gaps vs final-state optimal: ${total_gain:.2f} potential gain  "
              f"({len(alloc_rows)} pools differ materially)")
        for r in alloc_rows[:8]:
            arrow = "▲" if r["delta_votes"] > 0 else "▼"
            print(f"    {arrow} {r['pool'][:38]:38s}  "
                  f"our {r['our_votes']:>8,}  opt {r['optimal_votes']:>8,}  "
                  f"gain ${r['gain_usd']:>7.2f}")
        all_alloc.extend(alloc_rows)
        print()

    conn.close()

    # Write narrow window CSV
    if all_narrow:
        os.makedirs(os.path.dirname(out_narrow), exist_ok=True)
        with open(out_narrow, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_narrow[0].keys())
            writer.writeheader(); writer.writerows(all_narrow)
        print(f"Part 1 → {out_narrow}  ({len(all_narrow)} rows)")

    # Write allocation decision CSV
    if all_alloc:
        os.makedirs(os.path.dirname(out_alloc), exist_ok=True)
        with open(out_alloc, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=all_alloc[0].keys())
            writer.writeheader(); writer.writerows(all_alloc)
        print(f"Part 2 → {out_alloc}  ({len(all_alloc)} rows)")

    # Cross-epoch summary
    if all_narrow:
        print("\n=== NARROW WINDOW SUMMARY (pools in ≥2 epochs) ===")
        pool_late: Dict[str, List] = defaultdict(list)
        for r in all_narrow:
            pool_late[r["pool"]].append((r["epoch_date"], r["late_usd"]))
        for pool, entries in sorted(pool_late.items(), key=lambda x: -sum(v for _, v in x[1])):
            if len(entries) >= 2:
                total = sum(v for _, v in entries)
                dates = ", ".join(d for d, _ in entries)
                print(f"  {pool[:42]:42s}  total_late ${total:.2f}  epochs: {dates}")

    if all_alloc:
        total_gap = sum(r["gain_usd"] for r in all_alloc if r["gain_usd"] > 0)
        print(f"\n=== ALLOCATION DECISION SUMMARY ===")
        print(f"  Total potential gain if we had voted optimally on final state: ${total_gap:.2f}")
        print(f"  (This includes narrow-window bribes we structurally couldn't see)")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--out-narrow", default="analysis/pre_boundary/narrow_window_late_bribes.csv")
    p.add_argument("--out-alloc", default="analysis/pre_boundary/allocation_decision.csv")
    args = p.parse_args()
    run(args.epochs, args.out_narrow, args.out_alloc)
