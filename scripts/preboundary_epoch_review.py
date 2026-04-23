#!/usr/bin/env python3
"""
Historical boundary review: boundary-optimal vs mocked T-1 live prediction.

For each target epoch:
1) Build boundary states from boundary_gauge_values (active_only=1)
2) Find boundary optimal k via auto-k sweep
3) Build T-1 states from preboundary_snapshots decision_window='T-1'
4) Find mocked T-1 optimal k via auto-k sweep
5) Score mocked T-1 allocation on boundary truth and compute opportunity gap

Outputs CSV summary plus console progress heartbeats for long runs.
"""

import argparse
import csv
import logging
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.optimizer import expected_return_usd, solve_marginal_allocation


@dataclass
class AllocationRow:
    gauge: str
    pool: str
    alloc_votes: int
    base_votes: float
    rewards_usd: float
    expected_usd: float


@dataclass
class EpochReviewRow:
    epoch: int
    boundary_block: int
    boundary_gauges: int
    t1_gauges: int
    boundary_opt_k: int
    boundary_opt_expected_usd: float
    boundary_opt_per_1k: float
    t1_pred_k: int
    t1_pred_expected_usd: float
    t1_pred_per_1k: float
    t1_realized_at_boundary_usd: float
    t1_realized_at_boundary_per_1k: float
    opportunity_gap_usd: float
    opportunity_gap_pct: float


def configure_logging(log_file: Optional[str], verbose: bool) -> logging.Logger:
    logger = logging.getLogger("preboundary_epoch_review")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.handlers = []

    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def parse_epochs_csv(raw: str) -> List[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def load_target_epochs(
    conn: sqlite3.Connection,
    pre_conn: sqlite3.Connection,
    recent_epochs: int,
    explicit_epochs: Optional[Sequence[int]],
    decision_window: str,
    logger: logging.Logger,
) -> List[int]:
    if explicit_epochs:
        return sorted({int(e) for e in explicit_epochs})

    cur = conn.cursor()
    pre_cur = pre_conn.cursor()

    boundary_rows = cur.execute(
        """
        SELECT DISTINCT epoch
        FROM boundary_reward_snapshots
        WHERE active_only = 1
        ORDER BY epoch DESC
        """
    ).fetchall()
    boundary_epochs = [int(r[0]) for r in boundary_rows if r and r[0] is not None]

    pre_rows = pre_cur.execute(
        """
        SELECT DISTINCT epoch
        FROM preboundary_snapshots
        WHERE decision_window = ?
          AND COALESCE(CAST(rewards_now_usd AS REAL), 0.0) > 0
        ORDER BY epoch DESC
        """,
        (str(decision_window),),
    ).fetchall()
    pre_epochs = [int(r[0]) for r in pre_rows if r and r[0] is not None]

    boundary_set = set(boundary_epochs)
    pre_set = set(pre_epochs)
    eligible_desc = [e for e in boundary_epochs if e in pre_set]
    eligible = sorted(eligible_desc[: max(1, int(recent_epochs))])

    if not eligible:
        logger.warning(
            "No eligible epochs found with both boundary rewards and preboundary %s data.",
            decision_window,
        )
        logger.warning(
            "Boundary epochs with rewards (latest 5): %s",
            boundary_epochs[:5],
        )
        logger.warning(
            "Preboundary epochs for %s (latest 5): %s",
            decision_window,
            pre_epochs[:5],
        )

    return eligible


def load_boundary_block(conn: sqlite3.Connection, epoch: int) -> int:
    cur = conn.cursor()
    row = cur.execute(
        "SELECT boundary_block FROM epoch_boundaries WHERE epoch = ? LIMIT 1",
        (int(epoch),),
    ).fetchone()
    if not row or row[0] is None:
        return -1
    return int(row[0])


def load_token_prices_asof(conn: sqlite3.Connection, cutoff_ts: int) -> Dict[str, float]:
    cur = conn.cursor()
    price_map: Dict[str, float] = {}

    try:
        rows = cur.execute(
            """
            WITH latest AS (
                SELECT lower(token_address) AS token_address, MAX(timestamp) AS ts
                FROM historical_token_prices
                WHERE timestamp <= ? AND COALESCE(usd_price, 0) > 0
                GROUP BY lower(token_address)
            )
            SELECT lower(h.token_address), h.usd_price
            FROM historical_token_prices h
            JOIN latest l
              ON lower(h.token_address) = l.token_address
             AND h.timestamp = l.ts
            WHERE COALESCE(h.usd_price, 0) > 0
            """,
            (int(cutoff_ts),),
        ).fetchall()
        for token, usd in rows:
            price_map[str(token).lower()] = float(usd)
    except sqlite3.OperationalError:
        pass

    try:
        rows = cur.execute(
            """
            SELECT lower(token_address), usd_price
            FROM token_prices
            WHERE COALESCE(usd_price, 0) > 0
            """
        ).fetchall()
        for token, usd in rows:
            token_l = str(token).lower()
            price_map.setdefault(token_l, float(usd))
    except sqlite3.OperationalError:
        pass

    return price_map


def load_executed_votes(conn: sqlite3.Connection, epoch: int) -> Dict[str, int]:
    """Return {gauge_address: executed_votes} for the auto-voter run in this epoch, if any."""
    cur = conn.cursor()
    try:
        vote_epoch_row = cur.execute(
            "SELECT vote_epoch FROM epoch_boundaries WHERE epoch = ? LIMIT 1", (int(epoch),)
        ).fetchone()
        if not vote_epoch_row:
            return {}
        vote_epoch = int(vote_epoch_row[0])

        run_row = cur.execute(
            """
            SELECT id FROM auto_vote_runs
            WHERE status = 'tx_success'
              AND vote_sent_at IS NOT NULL
              AND vote_sent_at >= ?
              AND vote_sent_at < ?
            ORDER BY vote_sent_at DESC
            LIMIT 1
            """,
            (vote_epoch, int(epoch)),
        ).fetchone()
        if not run_row:
            return {}

        strategy_tag = f"auto_voter_run_{int(run_row[0])}"
        rows = cur.execute(
            """
            SELECT lower(gauge_address), executed_votes
            FROM executed_allocations
            WHERE epoch = ? AND strategy_tag = ?
            """,
            (int(epoch), strategy_tag),
        ).fetchall()
        return {str(g): int(v) for g, v in rows if g and v is not None and int(v) > 0}
    except sqlite3.OperationalError:
        return {}


def subtract_executed_votes(
    states: List[Tuple[str, str, float, float]],
    executed_votes: Dict[str, int],
) -> List[Tuple[str, str, float, float]]:
    """Subtract our executed votes from boundary votes_raw so boundary_opt doesn't double-count them."""
    if not executed_votes:
        return states
    result = []
    for gauge, pool, votes_raw, rewards_usd in states:
        our_votes = float(executed_votes.get(gauge, 0))
        adjusted = max(0.0, float(votes_raw) - our_votes)
        result.append((gauge, pool, adjusted, rewards_usd))
    return result


def load_boundary_states(conn: sqlite3.Connection, epoch: int) -> List[Tuple[str, str, float, float]]:
    cur = conn.cursor()
    price_map = load_token_prices_asof(conn, int(epoch))

    rewards_rows = cur.execute(
        """
        SELECT lower(brs.gauge_address) AS gauge_address,
               lower(brs.reward_token) AS reward_token,
               brs.rewards_raw,
               COALESCE(brs.token_decimals, tm.decimals, 18) AS token_decimals,
               COALESCE(brs.usd_price, 0.0) AS usd_price,
               COALESCE(brs.total_usd, 0.0) AS total_usd
        FROM boundary_reward_snapshots brs
        LEFT JOIN token_metadata tm
          ON lower(tm.token_address) = lower(brs.reward_token)
        WHERE brs.epoch = ?
          AND brs.active_only = 1
        """,
        (int(epoch),),
    ).fetchall()

    rewards_by_gauge: Dict[str, float] = {}
    for gauge, token, rewards_raw, decimals, usd_price, total_usd in rewards_rows:
        gauge_l = str(gauge or "").lower()
        if not gauge_l:
            continue

        total_usd_f = float(total_usd or 0.0)
        if total_usd_f > 0:
            rewards_by_gauge[gauge_l] = rewards_by_gauge.get(gauge_l, 0.0) + total_usd_f
            continue

        token_l = str(token or "").lower()
        dec_i = int(decimals or 18)
        try:
            reward_amt = float(int(str(rewards_raw or "0"))) / float(10 ** max(0, dec_i))
        except Exception:
            reward_amt = 0.0
        if reward_amt <= 0:
            continue

        price = float(usd_price or 0.0)
        if price <= 0:
            price = float(price_map.get(token_l, 0.0))
        if price <= 0:
            continue

        rewards_by_gauge[gauge_l] = rewards_by_gauge.get(gauge_l, 0.0) + (reward_amt * price)

    rows = cur.execute(
        """
        SELECT lower(gauge_address) AS gauge_address,
               lower(COALESCE(pool_address, gauge_address)) AS pool_address,
               CAST(votes_raw AS REAL) AS votes_raw
        FROM boundary_gauge_values
        WHERE epoch = ?
          AND active_only = 1
        """,
        (int(epoch),),
    ).fetchall()

    merged = []
    for gauge, pool, votes in rows:
        gauge_l = str(gauge or "").lower()
        pool_l = str(pool or gauge_l).lower()
        reward_usd = float(rewards_by_gauge.get(gauge_l, 0.0))
        if not gauge_l or not pool_l or reward_usd <= 0:
            continue
        merged.append((gauge_l, pool_l, float(votes or 0.0), reward_usd))

    merged.sort(key=lambda x: x[3], reverse=True)
    return merged


def load_preboundary_states(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
) -> List[Tuple[str, str, float, float]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT lower(gauge_address), lower(COALESCE(pool_address, gauge_address)),
               CAST(votes_now_raw AS REAL), CAST(rewards_now_usd AS REAL)
        FROM preboundary_snapshots
        WHERE epoch = ?
          AND decision_window = ?
          AND COALESCE(CAST(rewards_now_usd AS REAL), 0.0) > 0
        ORDER BY CAST(rewards_now_usd AS REAL) DESC
        """,
        (int(epoch), str(decision_window)),
    ).fetchall()
    return [
        (str(g), str(p), float(v or 0.0), float(r or 0.0))
        for g, p, v, r in rows
        if g and p
    ]


def calculate_allocation_from_states(
    states: List[Tuple[str, str, float, float]],
    voting_power: int,
    top_k: int,
    candidate_pools: int,
    min_votes_per_pool: int,
) -> List[AllocationRow]:
    if not states or voting_power <= 0:
        return []

    reference_vote_size = float(voting_power) / float(max(1, top_k))

    scored = []
    for gauge_addr, pool_addr, votes_raw, rewards_usd in states:
        base_votes = float(votes_raw or 0.0)
        rewards = float(rewards_usd or 0.0)
        single_pool_return = expected_return_usd(rewards, base_votes, reference_vote_size)
        adjusted_roi = rewards / max(1.0, (base_votes + reference_vote_size))
        scored.append((gauge_addr, pool_addr, base_votes, rewards, single_pool_return, adjusted_roi))

    scored.sort(key=lambda x: (x[4], x[5]), reverse=True)

    k = min(int(top_k), len(scored))
    if k <= 0:
        return []

    candidate_n = max(k, min(int(candidate_pools), len(scored)))
    candidates = [(g, p, b, r) for g, p, b, r, _sr, _roi in scored[:candidate_n]]

    effective_min_votes = int(max(0, min_votes_per_pool))
    if k * effective_min_votes > int(voting_power):
        effective_min_votes = int(voting_power // max(1, k))

    alloc_votes = solve_marginal_allocation(
        states=candidates,
        total_votes=int(voting_power),
        min_per_pool=effective_min_votes,
        max_selected_pools=k,
        chunk_size=1000,
    )

    selected: List[AllocationRow] = []
    for (gauge, pool, base_votes, rewards_usd), votes_alloc in zip(candidates, alloc_votes):
        votes_i = int(votes_alloc)
        if votes_i <= 0:
            continue
        expected_to_us = expected_return_usd(float(rewards_usd), float(base_votes), float(votes_i))
        selected.append(
            AllocationRow(
                gauge=str(gauge),
                pool=str(pool),
                alloc_votes=votes_i,
                base_votes=float(base_votes),
                rewards_usd=float(rewards_usd),
                expected_usd=float(expected_to_us),
            )
        )

    selected.sort(key=lambda x: (x.alloc_votes, x.expected_usd), reverse=True)
    return selected[:k]


def auto_select_k(
    states: List[Tuple[str, str, float, float]],
    voting_power: int,
    candidate_pools: int,
    min_votes_per_pool: int,
    k_min: int,
    k_max: int,
    k_step: int,
    logger: logging.Logger,
    context_label: str,
    epoch: int,
    progress_every_k: int,
) -> Tuple[int, List[AllocationRow], float]:
    best_k = max(1, int(k_min))
    best_alloc: List[AllocationRow] = []
    best_expected = -1.0

    checked = 0
    started = time.perf_counter()
    for k in range(max(1, int(k_min)), max(1, int(k_max)) + 1, max(1, int(k_step))):
        alloc = calculate_allocation_from_states(
            states=states,
            voting_power=int(voting_power),
            top_k=int(k),
            candidate_pools=max(int(candidate_pools), int(k)),
            min_votes_per_pool=int(min_votes_per_pool),
        )
        expected = sum(r.expected_usd for r in alloc)

        if expected > best_expected + 1e-9 or (abs(expected - best_expected) <= 0.01 and int(k) < best_k):
            best_expected = float(expected)
            best_k = int(k)
            best_alloc = alloc

        checked += 1
        if progress_every_k > 0 and (checked % progress_every_k == 0 or k == int(k_max)):
            elapsed = time.perf_counter() - started
            logger.info(
                "Epoch %s %s sweep progress: k=%s/%s checked=%s best_k=%s best_return=$%.2f elapsed=%.2fs",
                epoch,
                context_label,
                k,
                int(k_max),
                checked,
                best_k,
                best_expected,
                elapsed,
            )

    return best_k, best_alloc, max(0.0, best_expected)


def realized_on_boundary(
    allocation: List[AllocationRow],
    boundary_states: Dict[str, Tuple[float, float]],
) -> float:
    total = 0.0
    for row in allocation:
        base_votes, rewards_usd = boundary_states.get(row.gauge, (0.0, 0.0))
        total += expected_return_usd(float(rewards_usd), float(base_votes), float(row.alloc_votes))
    return float(total)


def write_results_csv(path: str, rows: List[EpochReviewRow]) -> None:
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "epoch",
                "boundary_block",
                "boundary_gauges",
                "t1_gauges",
                "boundary_opt_k",
                "boundary_opt_expected_usd",
                "boundary_opt_per_1k",
                "t1_pred_k",
                "t1_pred_expected_usd",
                "t1_pred_per_1k",
                "t1_realized_at_boundary_usd",
                "t1_realized_at_boundary_per_1k",
                "opportunity_gap_usd",
                "opportunity_gap_pct",
            ]
        )
        for r in rows:
            writer.writerow(
                [
                    r.epoch,
                    r.boundary_block,
                    r.boundary_gauges,
                    r.t1_gauges,
                    r.boundary_opt_k,
                    f"{r.boundary_opt_expected_usd:.6f}",
                    f"{r.boundary_opt_per_1k:.6f}",
                    r.t1_pred_k,
                    f"{r.t1_pred_expected_usd:.6f}",
                    f"{r.t1_pred_per_1k:.6f}",
                    f"{r.t1_realized_at_boundary_usd:.6f}",
                    f"{r.t1_realized_at_boundary_per_1k:.6f}",
                    f"{r.opportunity_gap_usd:.6f}",
                    f"{r.opportunity_gap_pct:.6f}",
                ]
            )


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Review boundary-optimal vs mocked T-1 preboundary prediction across epochs"
    )
    parser.add_argument("--db-path", default="data/db/data.db", help="Main DB path (boundary tables)")
    parser.add_argument(
        "--preboundary-db-path",
        default="data/db/preboundary_dev.db",
        help="Preboundary DB path (preboundary_snapshots)",
    )
    parser.add_argument(
        "--recent-epochs",
        type=int,
        default=2,
        help="Analyze N most recent epochs with boundary data",
    )
    parser.add_argument("--epochs", type=str, default="", help="Comma-separated explicit epochs")
    parser.add_argument(
        "--decision-window",
        type=str,
        default="T-1",
        help="Preboundary decision window to use for predicted allocation (default: T-1)",
    )
    parser.add_argument(
        "--voting-power",
        type=int,
        default=int(os.getenv("YOUR_VOTING_POWER", "0")),
        help="Voting power used for all epochs",
    )
    parser.add_argument("--candidate-pools", type=int, default=60, help="Candidate pool cap per k sweep")
    parser.add_argument(
        "--min-votes-per-pool",
        type=int,
        default=int(os.getenv("MIN_VOTE_ALLOCATION", "1000")),
        help="Minimum votes per selected pool",
    )
    parser.add_argument("--k-min", type=int, default=1, help="Minimum k in auto-k sweep")
    parser.add_argument("--k-max", type=int, default=50, help="Maximum k in auto-k sweep")
    parser.add_argument("--k-step", type=int, default=1, help="Step in auto-k sweep")
    parser.add_argument(
        "--progress-every-k",
        type=int,
        default=5,
        help="Emit sweep heartbeat every N checked k values",
    )
    parser.add_argument(
        "--output-csv",
        default="analysis/pre_boundary/epoch_boundary_vs_t1_review.csv",
        help="Output CSV path",
    )
    parser.add_argument("--log-file", default="data/db/logs/preboundary_epoch_review.log", help="Log file path")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    logger = configure_logging(args.log_file, args.verbose)

    if int(args.voting_power) <= 0:
        raise SystemExit("--voting-power must be > 0")
    if int(args.k_min) > int(args.k_max):
        raise SystemExit("--k-min cannot be greater than --k-max")

    logger.info("=" * 88)
    logger.info("Starting epoch boundary vs T-1 review")
    logger.info("Main DB: %s", args.db_path)
    logger.info("Preboundary DB: %s", args.preboundary_db_path)
    logger.info(
        "Config: voting_power=%s, decision_window=%s, k_range=[%s..%s], candidate_pools=%s",
        args.voting_power,
        args.decision_window,
        args.k_min,
        args.k_max,
        args.candidate_pools,
    )
    logger.info("=" * 88)

    main_conn = sqlite3.connect(args.db_path)
    pre_conn = sqlite3.connect(args.preboundary_db_path)

    try:
        explicit_epochs = parse_epochs_csv(args.epochs) if args.epochs else None
        epochs = load_target_epochs(
            conn=main_conn,
            pre_conn=pre_conn,
            recent_epochs=int(args.recent_epochs),
            explicit_epochs=explicit_epochs,
            decision_window=str(args.decision_window),
            logger=logger,
        )
        if not epochs:
            raise SystemExit("No target epochs found")

        logger.info("Target epochs (%s): %s", len(epochs), epochs)

        rows: List[EpochReviewRow] = []
        started_all = time.perf_counter()

        for idx, epoch in enumerate(epochs, start=1):
            logger.info("--- Epoch %s (%s/%s) ---", epoch, idx, len(epochs))
            epoch_started = time.perf_counter()

            boundary_block = load_boundary_block(main_conn, int(epoch))
            boundary_states = load_boundary_states(main_conn, int(epoch))
            executed_votes = load_executed_votes(main_conn, int(epoch))
            boundary_states_for_sweep = subtract_executed_votes(boundary_states, executed_votes)
            t1_states = load_preboundary_states(pre_conn, int(epoch), str(args.decision_window))

            logger.info(
                "Epoch %s data sizes: boundary_gauges=%s, %s_gauges=%s, boundary_block=%s",
                epoch,
                len(boundary_states_for_sweep),
                args.decision_window,
                len(t1_states),
                boundary_block,
            )

            if not boundary_states_for_sweep:
                logger.warning("Epoch %s skipped: no boundary states with rewards", epoch)
                continue
            if not t1_states:
                logger.warning("Epoch %s skipped: no %s states with rewards", epoch, args.decision_window)
                continue

            boundary_best_k, boundary_best_alloc, boundary_best_expected = auto_select_k(
                states=boundary_states_for_sweep,
                voting_power=int(args.voting_power),
                candidate_pools=int(args.candidate_pools),
                min_votes_per_pool=int(args.min_votes_per_pool),
                k_min=int(args.k_min),
                k_max=int(args.k_max),
                k_step=int(args.k_step),
                logger=logger,
                context_label="boundary",
                epoch=int(epoch),
                progress_every_k=int(args.progress_every_k),
            )

            pred_best_k, pred_best_alloc, pred_expected_t1 = auto_select_k(
                states=t1_states,
                voting_power=int(args.voting_power),
                candidate_pools=int(args.candidate_pools),
                min_votes_per_pool=int(args.min_votes_per_pool),
                k_min=int(args.k_min),
                k_max=int(args.k_max),
                k_step=int(args.k_step),
                logger=logger,
                context_label=str(args.decision_window),
                epoch=int(epoch),
                progress_every_k=int(args.progress_every_k),
            )

            boundary_lookup = {g: (v, r) for g, _p, v, r in boundary_states_for_sweep}
            pred_realized_on_boundary = realized_on_boundary(pred_best_alloc, boundary_lookup)

            boundary_per_1k = (boundary_best_expected * 1000.0) / max(1.0, float(args.voting_power))
            pred_per_1k = (pred_expected_t1 * 1000.0) / max(1.0, float(args.voting_power))
            pred_realized_per_1k = (pred_realized_on_boundary * 1000.0) / max(1.0, float(args.voting_power))
            opportunity_gap_usd = boundary_best_expected - pred_realized_on_boundary
            opportunity_gap_pct = (
                (opportunity_gap_usd / boundary_best_expected) * 100.0 if boundary_best_expected > 0 else 0.0
            )

            rows.append(
                EpochReviewRow(
                    epoch=int(epoch),
                    boundary_block=int(boundary_block),
                    boundary_gauges=len(boundary_states),
                    t1_gauges=len(t1_states),
                    boundary_opt_k=int(boundary_best_k),
                    boundary_opt_expected_usd=float(boundary_best_expected),
                    boundary_opt_per_1k=float(boundary_per_1k),
                    t1_pred_k=int(pred_best_k),
                    t1_pred_expected_usd=float(pred_expected_t1),
                    t1_pred_per_1k=float(pred_per_1k),
                    t1_realized_at_boundary_usd=float(pred_realized_on_boundary),
                    t1_realized_at_boundary_per_1k=float(pred_realized_per_1k),
                    opportunity_gap_usd=float(opportunity_gap_usd),
                    opportunity_gap_pct=float(opportunity_gap_pct),
                )
            )

            elapsed_epoch = time.perf_counter() - epoch_started
            logger.info(
                "Epoch %s complete in %.2fs | boundary(k=%s,$%.2f) | %s_pred(k=%s,$%.2f) | %s_realized_boundary=$%.2f | gap=$%.2f (%.2f%%)",
                epoch,
                elapsed_epoch,
                boundary_best_k,
                boundary_best_expected,
                args.decision_window,
                pred_best_k,
                pred_expected_t1,
                args.decision_window,
                pred_realized_on_boundary,
                opportunity_gap_usd,
                opportunity_gap_pct,
            )

        write_results_csv(args.output_csv, rows)

        total_elapsed = time.perf_counter() - started_all
        logger.info("=" * 88)
        logger.info("Review complete: epochs_analyzed=%s, output=%s, elapsed=%.2fs", len(rows), args.output_csv, total_elapsed)
        if rows:
            avg_gap = sum(r.opportunity_gap_usd for r in rows) / float(len(rows))
            avg_boundary_k = sum(r.boundary_opt_k for r in rows) / float(len(rows))
            avg_pred_k = sum(r.t1_pred_k for r in rows) / float(len(rows))
            logger.info(
                "Averages | boundary_opt_k=%.2f | %s_pred_k=%.2f | opportunity_gap_usd=$%.2f",
                avg_boundary_k,
                args.decision_window,
                avg_pred_k,
                avg_gap,
            )
        logger.info("=" * 88)

    finally:
        main_conn.close()
        pre_conn.close()


if __name__ == "__main__":
    main()
