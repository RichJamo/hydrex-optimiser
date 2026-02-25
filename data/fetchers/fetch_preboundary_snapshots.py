#!/usr/bin/env python3
"""
P2 Collector: Materialize pre-boundary snapshot state from boundary tables.

MVP: Backfill historical epochs (offline backtest).
Phase 6+: Add real-time T-1/boundary decision support.

Usage:
  # Backfill 10 most recent epochs
  python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 10

  # Backfill specific epoch range
  python -m data.fetchers.fetch_preboundary_snapshots --start-epoch 1771372800 --end-epoch 1771977600

  # Resume incomplete collection
  python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 10 --resume

  # Check completeness for a single epoch
  python -m data.fetchers.fetch_preboundary_snapshots --check-epoch 1771372800
"""

import argparse
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, TextIO, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from web3 import Web3

from config.preboundary_settings import (
    DECISION_WINDOWS,
    LOGGING_DIR,
    PBOUNDARY_LOG_FILE,
    PREBOUNDARY_DB_PATH,
    make_logging_dir,
)
from src.preboundary_store import (
    DEFAULT_WINDOWS,
    get_incomplete_decision_windows,
    get_preboundary_epoch_snapshot_count,
    materialize_preboundary_snapshots_for_epoch,
    upsert_preboundary_snapshots,
    upsert_truth_labels_from_boundary,
    ensure_preboundary_tables,
)

console = Console()
load_dotenv()

ONE_E18 = 10**18

VOTER_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "_pool", "type": "address"},
            {"internalType": "uint256", "name": "_time", "type": "uint256"},
        ],
        "name": "weightsAt",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]

BRIBE_ABI = [
    {
        "inputs": [],
        "name": "rewardsListLength",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "name": "rewardTokens",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "", "type": "address"},
            {"internalType": "uint256", "name": "", "type": "uint256"},
        ],
        "name": "rewardData",
        "outputs": [
            {"internalType": "uint256", "name": "periodFinish", "type": "uint256"},
            {"internalType": "uint256", "name": "rewardsPerEpoch", "type": "uint256"},
            {"internalType": "uint256", "name": "lastUpdateTime", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "", "type": "address"}],
        "name": "isRewardToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    }
]

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"


def get_recent_boundary_epochs(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> List[int]:
    """
    Fetch N most recent epochs from boundary_gauge_values.

    Returns sorted list [oldest, ..., newest].

    Args:
        conn: database connection to live data.db
        limit: number of recent epochs to fetch

    Returns:
        List of epoch timestamps sorted ascending
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT epoch
        FROM boundary_gauge_values
        WHERE active_only = 1
        ORDER BY epoch DESC
        LIMIT ?
        """,
        (limit,),
    )
    epochs = [row[0] for row in cur.fetchall()]
    return sorted(epochs)


def collect_preboundary_snapshots_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    resume: bool = True,
    snapshot_source: str = "raw_asof",
    source_conn: Optional[sqlite3.Connection] = None,
    rpc_url: Optional[str] = None,
    max_gauges: int = 0,
    min_reward_usd: float = 0.0,
    log_file: Optional[TextIO] = None,
) -> Dict[str, int]:
    """
    Materialize + upsert snapshots for all decision windows in a single epoch.

    Args:
        conn: target database connection to preboundary DB
        epoch: boundary epoch to backfill
        resume: if True, skip windows already complete; if False, overwrite
        snapshot_source: snapshot materialization mode ("raw_asof" or "boundary_derived")
        source_conn: optional source DB connection (live data source)
        log_file: file handle for heartbeat logging

    Returns:
        Dict[window_name] -> rows_inserted

    Process:
      1. Check completeness: which windows already done?
      2. If resume=True, skip complete windows
      3. For each incomplete window:
         a. Materialize snapshot rows
         b. Upsert to preboundary_snapshots table
         c. Log heartbeat
      4. After all windows done for epoch:
         a. Materialize + upsert truth labels
         b. Log summary
      5. Return row counts
    """
    result = {}
    src_conn = source_conn or conn

    if resume:
        incomplete_windows = get_incomplete_decision_windows(conn, epoch, DEFAULT_WINDOWS)
    else:
        cur = conn.cursor()
        cur.execute("DELETE FROM preboundary_snapshots WHERE epoch = ?", (int(epoch),))
        cur.execute("DELETE FROM preboundary_truth_labels WHERE epoch = ?", (int(epoch),))
        conn.commit()
        incomplete_windows = list(DEFAULT_WINDOWS)

    if not incomplete_windows:
        console.print(f"[cyan]Epoch {epoch}: all windows complete, skipping[/cyan]")
        if log_file:
            log_file.write(f"[{_timestamp()}] Epoch {epoch}: all windows complete (resume=True), skipped\n")
            log_file.flush()
        return result

    console.print(f"[cyan]Starting epoch {epoch}; incomplete windows: {incomplete_windows}[/cyan]")
    if log_file:
        log_file.write(f"[{_timestamp()}] Starting epoch {epoch}; incomplete windows: {incomplete_windows}\n")
        log_file.flush()

    # Materialize snapshots for this epoch
    try:
        if snapshot_source in {"onchain_balances", "onchain_rewarddata"}:
            snapshots = _materialize_onchain_balance_snapshots_for_epoch(
                src_conn,
                epoch,
                rpc_url=rpc_url,
                decision_windows=tuple(DEFAULT_WINDOWS),
                min_reward_usd=float(min_reward_usd),
                max_gauges=int(max_gauges),
                reward_source=("rewarddata" if snapshot_source == "onchain_rewarddata" else "balances"),
                log_file=log_file,
            )
        else:
            snapshots = materialize_preboundary_snapshots_for_epoch(
                src_conn,
                epoch,
                snapshot_source=snapshot_source,
                min_reward_usd=float(min_reward_usd),
            )
    except Exception as e:
        console.print(f"[red]Error materializing snapshots for epoch {epoch}: {e}[/red]")
        if log_file:
            log_file.write(f"[{_timestamp()}] ERROR epoch {epoch}: {e}\n")
            log_file.flush()
        return result

    # Upsert each window's snapshots
    for window in incomplete_windows:
        if window not in snapshots:
            continue

        rows = snapshots[window]
        if not rows:
            console.print(f"[yellow]Epoch {epoch}, window {window}: no rows to insert[/yellow]")
            result[window] = 0
            if log_file:
                log_file.write(f"[{_timestamp()}] Epoch {epoch}, window {window}: 0 rows\n")
                log_file.flush()
            continue

        try:
            rows_inserted = upsert_preboundary_snapshots(conn, rows)
            result[window] = rows_inserted
            console.print(
                f"[green]✓ Epoch {epoch}, window {window}: {rows_inserted} rows inserted[/green]"
            )
            if log_file:
                log_file.write(
                    f"[{_timestamp()}] Epoch {epoch}, window {window}: {rows_inserted} rows upserted\n"
                )
                log_file.flush()
        except Exception as e:
            console.print(f"[red]Error upserting epoch {epoch}, window {window}: {e}[/red]")
            if log_file:
                log_file.write(f"[{_timestamp()}] ERROR epoch {epoch}, window {window}: {e}\n")
                log_file.flush()

    # After all windows, materialize truth labels
    try:
        # Query vote_epoch from source DB used by boundary collector
        cur = src_conn.cursor()
        cur.execute(
            "SELECT DISTINCT vote_epoch FROM boundary_gauge_values WHERE epoch = ? AND vote_epoch IS NOT NULL LIMIT 1",
            (epoch,),
        )
        vote_epoch_row = cur.fetchone()
        if vote_epoch_row and vote_epoch_row[0] is not None:
            vote_epoch = vote_epoch_row[0]
        else:
            # Fallback: use epoch as vote_epoch (same as boundary epoch)
            vote_epoch = epoch

        truth_rows = _upsert_truth_labels_from_boundary_source(conn, src_conn, epoch, vote_epoch, active_only=1)
        console.print(f"[green]✓ Epoch {epoch}: {truth_rows} truth labels materialized[/green]")
        if log_file:
            log_file.write(f"[{_timestamp()}] Epoch {epoch}: {truth_rows} truth labels materialized\n")
            log_file.flush()
    except Exception as e:
        console.print(f"[yellow]Warning: Could not materialize truth labels for epoch {epoch}: {e}[/yellow]")
        if log_file:
            log_file.write(f"[{_timestamp()}] WARNING: truth labels for epoch {epoch}: {e}\n")
            log_file.flush()

    total_inserted = sum(result.values())
    console.print(f"[green]✓ Epoch {epoch} complete: {total_inserted} total snapshots[/green]")
    if log_file:
        log_file.write(
            f"[{_timestamp()}] Epoch {epoch} complete: {total_inserted} total snapshots across windows\n"
        )
        log_file.flush()

    return result


def collect_preboundary_batch(
    db_path: str,
    epochs: List[int],
    live_db_path: str,
    resume: bool = True,
    snapshot_source: str = "raw_asof",
    rpc_url: Optional[str] = None,
    max_gauges: int = 0,
    min_reward_usd: float = 0.0,
    log_file: Optional[str] = None,
) -> Dict[int, Dict[str, int]]:
    """
    Backfill snapshots for multiple epochs.

    Args:
        db_path: path to preboundary database (e.g. data/db/preboundary_dev.db)
        epochs: list of boundary epochs to backfill
        live_db_path: path to live data.db (source of boundary tables)
        resume: if True, skip complete epochs
        log_file: path to durable log file (created if missing)

    Returns:
        Dict[epoch] -> Dict[window] -> rows_inserted
    """
    make_logging_dir()

    # Open connections
    pbconn = sqlite3.connect(db_path)
    ensure_preboundary_tables(pbconn)

    live_conn = sqlite3.connect(live_db_path)

    result = {}
    log_fh = None

    try:
        if log_file:
            log_fh = open(log_file, "a", buffering=1)
            log_fh.write(f"\n{'='*80}\n")
            log_fh.write(f"[{_timestamp()}] Starting batch collection for {len(epochs)} epochs\n")
            log_fh.flush()

        for epoch in epochs:
            epoch_result = collect_preboundary_snapshots_for_epoch(
                pbconn,
                epoch,
                resume=resume,
                snapshot_source=snapshot_source,
                source_conn=live_conn,
                rpc_url=rpc_url,
                max_gauges=max_gauges,
                min_reward_usd=min_reward_usd,
                log_file=log_fh,
            )
            result[epoch] = epoch_result

        # Final summary
        total_epochs = len(result)
        total_snapshots = sum(
            sum(windows.values())
            for windows in result.values()
        )
        console.print(f"\n[cyan]{'='*60}[/cyan]")
        console.print(
            f"[green]✓ Batch complete: {total_epochs} epochs, {total_snapshots} total snapshots[/green]"
        )
        console.print(f"[cyan]{'='*60}[/cyan]\n")

        if log_fh:
            log_fh.write(f"[{_timestamp()}] Batch complete: {total_epochs} epochs, {total_snapshots} total snapshots\n")
            log_fh.flush()

    finally:
        pbconn.close()
        live_conn.close()
        if log_fh:
            log_fh.close()

    return result


def _upsert_truth_labels_from_boundary_source(
    target_conn: sqlite3.Connection,
    source_conn: sqlite3.Connection,
    epoch: int,
    vote_epoch: int,
    active_only: int = 1,
) -> int:
    """Materialize truth labels into target DB using boundary tables from source DB."""
    src_cur = source_conn.cursor()
    rows = src_cur.execute(
        """
        SELECT
            g.epoch,
            g.vote_epoch,
            lower(g.gauge_address) AS gauge_address,
            COALESCE(g.votes_raw, 0.0) AS final_votes_raw,
            COALESCE(r.total_rewards_usd, 0.0) AS final_rewards_usd,
            'boundary_tables' AS source_tag
        FROM boundary_gauge_values g
        LEFT JOIN (
            SELECT
                epoch,
                vote_epoch,
                active_only,
                lower(gauge_address) AS gauge_address,
                SUM(COALESCE(total_usd, 0.0)) AS total_rewards_usd
            FROM boundary_reward_snapshots
            GROUP BY epoch, vote_epoch, active_only, lower(gauge_address)
        ) r
          ON r.epoch = g.epoch
         AND r.vote_epoch = g.vote_epoch
         AND r.active_only = g.active_only
         AND r.gauge_address = lower(g.gauge_address)
        WHERE g.epoch = ?
          AND g.vote_epoch = ?
          AND g.active_only = ?
        """,
        (int(epoch), int(vote_epoch), int(active_only)),
    ).fetchall()

    if not rows:
        return 0

    now_ts = int(time.time())
    tgt_cur = target_conn.cursor()
    tgt_cur.executemany(
        """
        INSERT OR REPLACE INTO preboundary_truth_labels(
            epoch, vote_epoch, gauge_address,
            final_votes_raw, final_rewards_usd,
            source_tag, computed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                int(row[0]),
                int(row[1]),
                str(row[2]),
                float(row[3] or 0.0),
                float(row[4] or 0.0),
                str(row[5]),
                now_ts,
            )
            for row in rows
        ],
    )
    target_conn.commit()
    return len(rows)


def _timestamp() -> str:
    """Return current timestamp in readable format."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _find_block_at_timestamp(w3: Web3, target_ts: int, tolerance: int = 60) -> int:
    left, right = 0, w3.eth.block_number
    best = right
    while left <= right:
        mid = (left + right) // 2
        block = w3.eth.get_block(mid)
        ts = int(block["timestamp"])
        if abs(ts - int(target_ts)) <= tolerance:
            return int(mid)
        if ts < int(target_ts):
            best = int(mid)
            left = mid + 1
        else:
            right = mid - 1
    return int(best)


def _load_epoch_boundary_block(conn: sqlite3.Connection, epoch: int) -> int:
    cur = conn.cursor()
    try:
        row = cur.execute(
            "SELECT boundary_block FROM epoch_boundaries WHERE epoch = ?",
            (int(epoch),),
        ).fetchone()
    except sqlite3.OperationalError:
        return 0

    if not row or row[0] is None:
        return 0
    return int(row[0])


def _load_epoch_gauge_context(
    conn: sqlite3.Connection,
    epoch: int,
    max_gauges: int,
) -> List[Tuple[str, str, str, str, int, int]]:
    cur = conn.cursor()
    rows = cur.execute(
        """
        SELECT
            lower(bg.gauge_address) AS gauge_address,
            lower(COALESCE(bg.pool_address, g.pool, bg.gauge_address)) AS pool_address,
            lower(COALESCE(m.internal_bribe, g.internal_bribe, '')) AS internal_bribe,
            lower(COALESCE(m.external_bribe, g.external_bribe, '')) AS external_bribe,
            COALESCE(bg.vote_epoch, bg.epoch) AS vote_epoch,
            COALESCE(bg.boundary_block, 0) AS boundary_block
        FROM boundary_gauge_values bg
        LEFT JOIN gauge_bribe_mapping m
          ON lower(m.gauge_address) = lower(bg.gauge_address)
        LEFT JOIN gauges g
          ON lower(g.address) = lower(bg.gauge_address)
        WHERE bg.epoch = ?
          AND bg.active_only = 1
          AND COALESCE(bg.votes_raw, 0) > 0
        ORDER BY COALESCE(bg.total_usd, 0) DESC, lower(bg.gauge_address)
        """,
        (int(epoch),),
    ).fetchall()
    if max_gauges and max_gauges > 0:
        rows = rows[: int(max_gauges)]
    return [
        (
            str(row[0] or "").lower(),
            str(row[1] or row[0] or "").lower(),
            str(row[2] or "").lower(),
            str(row[3] or "").lower(),
            int(row[4] or epoch),
            int(row[5] or 0),
        )
        for row in rows
        if row and row[0]
    ]


def _load_reward_token_metadata(conn: sqlite3.Connection) -> Dict[str, Tuple[int, float]]:
    cur = conn.cursor()
    token_meta: Dict[str, Tuple[int, float]] = {}

    # Prefer historical reward snapshot metadata for decimals + price.
    for token, decimals, usd_price in cur.execute(
        """
        SELECT lower(reward_token) AS token,
               MAX(COALESCE(token_decimals, 18)) AS token_decimals,
               MAX(COALESCE(usd_price, 0.0)) AS usd_price
        FROM boundary_reward_snapshots
        WHERE reward_token IS NOT NULL AND reward_token != ''
        GROUP BY lower(reward_token)
        """
    ).fetchall():
        token_meta[str(token).lower()] = (int(decimals or 18), float(usd_price or 0.0))

    # Overlay token_prices where available for fresher prices.
    try:
        for token, price in cur.execute(
            """
            SELECT lower(token_address) AS token, COALESCE(usd_price, 0.0) AS usd_price
            FROM token_prices
            WHERE token_address IS NOT NULL AND token_address != ''
            """
        ).fetchall():
            token_l = str(token).lower()
            dec, _ = token_meta.get(token_l, (18, 0.0))
            token_meta[token_l] = (dec, float(price or 0.0))
    except sqlite3.OperationalError:
        pass

    return token_meta


def _enumerate_reward_tokens(bribe_contract, block_identifier: int) -> List[str]:
    try:
        length = int(bribe_contract.functions.rewardsListLength().call(block_identifier=block_identifier))
    except Exception:
        return []

    tokens: List[str] = []
    for idx in range(min(length, 1000)):
        try:
            token = str(bribe_contract.functions.rewardTokens(idx).call(block_identifier=block_identifier)).lower()
            if not token or token == ZERO_ADDRESS:
                continue
            tokens.append(token)
        except Exception:
            continue

    return tokens


def _materialize_onchain_balance_snapshots_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    rpc_url: Optional[str],
    decision_windows: Tuple[str, ...],
    min_reward_usd: float,
    max_gauges: int,
    reward_source: str,
    log_file: Optional[TextIO],
) -> Dict[str, List[Tuple]]:
    from config.preboundary_settings import (
        DECISION_WINDOWS,
        INCLUSION_PROB_BY_WINDOW,
        BLOCK_TIME_ESTIMATE_SECONDS,
    )
    from config.settings import VOTER_ADDRESS

    if not rpc_url:
        raise ValueError("RPC URL is required for on-chain snapshot sources (set --rpc or RPC_URL)")

    if reward_source not in {"balances", "rewarddata"}:
        raise ValueError(f"Unsupported reward_source={reward_source}")

    gauge_context = _load_epoch_gauge_context(conn, epoch, max_gauges=max_gauges)
    if not gauge_context:
        return {window: [] for window in decision_windows}

    token_meta = _load_reward_token_metadata(conn)

    try:
        rpc_timeout = int(os.getenv("RPC_TIMEOUT", "30"))
    except Exception:
        rpc_timeout = 30
    rpc_timeout = max(5, rpc_timeout)

    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": rpc_timeout}))
    if not w3.is_connected():
        raise RuntimeError("Failed to connect to RPC provider")

    voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
    erc20_contract_cache = {}
    bribe_contract_cache = {}
    bribe_tokens_cache: Dict[Tuple[str, int], List[str]] = {}
    token_balance_cache: Dict[Tuple[str, str, int], int] = {}
    token_rewarddata_cache: Dict[Tuple[str, str, int, int], int] = {}
    vote_cache: Dict[Tuple[str, int, int], float] = {}

    boundary_timestamp = int(epoch)
    # Use epoch boundary anchor if available; otherwise fallback to snapshot table hints or timestamp search.
    boundary_block = _load_epoch_boundary_block(conn, epoch)
    if boundary_block <= 0:
        boundary_block_candidates = [ctx[5] for ctx in gauge_context if int(ctx[5]) > 0]
        boundary_block = max(boundary_block_candidates) if boundary_block_candidates else _find_block_at_timestamp(w3, boundary_timestamp)

    result = {window: [] for window in decision_windows}
    for window in decision_windows:
        if window not in DECISION_WINDOWS:
            continue

        seconds_before = int(DECISION_WINDOWS[window]["seconds_before_boundary"])
        decision_timestamp = int(boundary_timestamp - seconds_before)

        if seconds_before == 0:
            decision_block = int(boundary_block)
        else:
            # Estimate first to reduce RPC calls, then clamp with timestamp search for better as-of alignment.
            est_block = max(0, int(boundary_block) - (seconds_before // max(1, BLOCK_TIME_ESTIMATE_SECONDS)))
            try:
                decision_block = int(_find_block_at_timestamp(w3, decision_timestamp))
                if decision_block <= 0:
                    decision_block = int(est_block)
            except Exception:
                decision_block = int(est_block)

        inclusion_prob = float(INCLUSION_PROB_BY_WINDOW.get(window, 0.5))
        rows_for_window: List[Tuple] = []

        for gauge_addr, pool_addr, internal_bribe, external_bribe, vote_epoch, _ in gauge_context:
            # Votes: canonical weightsAt(pool, vote_epoch) at decision block.
            vote_key = (pool_addr, int(vote_epoch), int(decision_block))
            if vote_key in vote_cache:
                votes_now_raw = vote_cache[vote_key]
            else:
                try:
                    votes_wei = int(
                        voter.functions.weightsAt(Web3.to_checksum_address(pool_addr), int(vote_epoch)).call(
                            block_identifier=int(decision_block)
                        )
                    )
                    votes_now_raw = float(votes_wei) / float(ONE_E18)
                except Exception:
                    votes_now_raw = 0.0
                vote_cache[vote_key] = float(votes_now_raw)

            # Rewards source:
            # - rewarddata (canonical): sum rewardData(token, vote_epoch).rewardsPerEpoch
            # - balances (diagnostic): sum ERC20.balanceOf(bribe_contract)
            rewards_now_usd = 0.0
            for bribe_addr in (internal_bribe, external_bribe):
                if not bribe_addr or bribe_addr == ZERO_ADDRESS:
                    continue

                bribe_contract = bribe_contract_cache.get(bribe_addr)
                if bribe_contract is None:
                    try:
                        bribe_contract = w3.eth.contract(
                            address=Web3.to_checksum_address(bribe_addr),
                            abi=BRIBE_ABI,
                        )
                    except Exception:
                        bribe_contract = None
                    bribe_contract_cache[bribe_addr] = bribe_contract

                if bribe_contract is None:
                    continue

                token_list_key = (bribe_addr, int(decision_block))
                if token_list_key in bribe_tokens_cache:
                    reward_tokens = bribe_tokens_cache[token_list_key]
                else:
                    reward_tokens = _enumerate_reward_tokens(bribe_contract, int(decision_block))
                    bribe_tokens_cache[token_list_key] = reward_tokens

                for token_addr in reward_tokens:
                    dec, usd_price = token_meta.get(token_addr, (18, 0.0))

                    if reward_source == "rewarddata":
                        reward_key = (bribe_addr, token_addr, int(vote_epoch), int(decision_block))
                        if reward_key in token_rewarddata_cache:
                            reward_raw = token_rewarddata_cache[reward_key]
                        else:
                            try:
                                rd = bribe_contract.functions.rewardData(
                                    Web3.to_checksum_address(token_addr),
                                    int(vote_epoch),
                                ).call(block_identifier=int(decision_block))
                                reward_raw = int(rd[1])
                            except Exception:
                                reward_raw = 0
                            token_rewarddata_cache[reward_key] = int(reward_raw)

                        if reward_raw <= 0:
                            continue
                        rewards_now_usd += (float(reward_raw) / float(10 ** max(0, int(dec)))) * float(usd_price)
                    else:
                        bal_key = (bribe_addr, token_addr, int(decision_block))
                        if bal_key in token_balance_cache:
                            balance_raw = token_balance_cache[bal_key]
                        else:
                            token_contract = erc20_contract_cache.get(token_addr)
                            if token_contract is None:
                                try:
                                    token_contract = w3.eth.contract(
                                        address=Web3.to_checksum_address(token_addr),
                                        abi=ERC20_ABI,
                                    )
                                except Exception:
                                    token_contract = None
                                erc20_contract_cache[token_addr] = token_contract

                            if token_contract is None:
                                balance_raw = 0
                            else:
                                try:
                                    balance_raw = int(
                                        token_contract.functions.balanceOf(
                                            Web3.to_checksum_address(bribe_addr)
                                        ).call(block_identifier=int(decision_block))
                                    )
                                except Exception:
                                    balance_raw = 0
                            token_balance_cache[bal_key] = int(balance_raw)

                        if balance_raw <= 0:
                            continue
                        rewards_now_usd += (float(balance_raw) / float(10 ** max(0, int(dec)))) * float(usd_price)

            if votes_now_raw <= 0 and rewards_now_usd < float(min_reward_usd):
                continue

            if votes_now_raw > 0 and rewards_now_usd > 0:
                data_quality_score = 1.0
            elif votes_now_raw > 0 or rewards_now_usd > 0:
                data_quality_score = 0.75
            else:
                data_quality_score = 0.5

            source_tag = "onchain_weights_rewarddata" if reward_source == "rewarddata" else "onchain_weights_balance"
            rows_for_window.append(
                (
                    int(epoch),
                    str(window),
                    int(decision_timestamp),
                    int(decision_block),
                    int(boundary_timestamp),
                    int(boundary_block),
                    str(gauge_addr),
                    str(pool_addr),
                    float(votes_now_raw),
                    float(rewards_now_usd),
                    float(inclusion_prob),
                    float(data_quality_score),
                    source_tag,
                )
            )

        result[window] = rows_for_window
        console.print(
            f"[cyan]Epoch {epoch}, {window}: collected {len(rows_for_window)} on-chain rows (block={decision_block}, reward_source={reward_source})[/cyan]"
        )
        if log_file:
            log_file.write(
                f"[{_timestamp()}] Epoch {epoch}, {window}: on-chain rows={len(rows_for_window)} block={decision_block} reward_source={reward_source}\n"
            )
            log_file.flush()

    return result


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Backfill pre-boundary snapshots")
    parser.add_argument(
        "--recent-epochs",
        type=int,
        default=None,
        help="Backfill N most recent epochs",
    )
    parser.add_argument(
        "--start-epoch",
        type=int,
        default=None,
        help="Start epoch timestamp (Unix)",
    )
    parser.add_argument(
        "--end-epoch",
        type=int,
        default=None,
        help="End epoch timestamp (Unix)",
    )
    parser.add_argument(
        "--check-epoch",
        type=int,
        default=None,
        help="Check completeness for single epoch",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=True,
        help="Skip already complete partitions (default: True)",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Overwrite complete partitions (force re-run)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default=PREBOUNDARY_DB_PATH,
        help="Path to preboundary database",
    )
    parser.add_argument(
        "--live-db-path",
        type=str,
        default="data/db/data.db",
        help="Path to live data.db (boundary tables source)",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=PBOUNDARY_LOG_FILE,
        help="Log file path",
    )
    parser.add_argument(
        "--snapshot-source",
        choices=["raw_asof", "boundary_derived", "onchain_balances", "onchain_rewarddata"],
        default="raw_asof",
        help="Snapshot source mode: raw_asof (DB as-of), boundary_derived (legacy/debug), onchain_rewarddata (weightsAt + rewardData), or onchain_balances (diagnostic: weightsAt + token balances)",
    )
    parser.add_argument(
        "--rpc",
        type=str,
        default=None,
        help="RPC URL override (default: RPC_URL from .env)",
    )
    parser.add_argument(
        "--max-gauges",
        type=int,
        default=0,
        help="Optional gauge cap for faster smoke runs (0 = all)",
    )
    parser.add_argument(
        "--min-reward-usd",
        type=float,
        default=0.0,
        help="Minimum reward USD filter for snapshot inclusion",
    )

    args = parser.parse_args()
    resume = not args.no_resume
    rpc_url = args.rpc or os.getenv("RPC_URL", "")

    # Single epoch check mode
    if args.check_epoch is not None:
        pbconn = sqlite3.connect(args.db_path)
        ensure_preboundary_tables(pbconn)

        from src.preboundary_store import get_preboundary_completeness

        summary = get_preboundary_completeness(pbconn, args.check_epoch)

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Setting", width=28)
        table.add_column("Value", width=60)
        table.add_row("Epoch", str(summary["epoch"]))
        table.add_row("Windows complete", str(summary["snapshots_complete"]))
        table.add_row("Windows present", ", ".join(summary["snapshots_windows_present"]) or "—")
        table.add_row("Expected windows", ", ".join(summary["expected_windows"]))

        console.print()
        console.print(table)
        pbconn.close()
        return

    # Determine epochs to process
    epochs = []
    if args.recent_epochs is not None:
        live_conn = sqlite3.connect(args.live_db_path)
        epochs = get_recent_boundary_epochs(live_conn, args.recent_epochs)
        live_conn.close()
        console.print(f"[cyan]Fetched {len(epochs)} recent epochs[/cyan]")
    elif args.start_epoch is not None and args.end_epoch is not None:
        live_conn = sqlite3.connect(args.live_db_path)
        cur = live_conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT epoch FROM boundary_gauge_values
            WHERE epoch >= ? AND epoch <= ? AND active_only = 1
            ORDER BY epoch
            """,
            (args.start_epoch, args.end_epoch),
        )
        epochs = sorted([row[0] for row in cur.fetchall()])
        live_conn.close()
        console.print(f"[cyan]Fetched {len(epochs)} epochs in range [{args.start_epoch}, {args.end_epoch}][/cyan]")
    else:
        console.print(
            "[red]Error: Must specify --recent-epochs or (--start-epoch and --end-epoch)[/red]"
        )
        parser.print_help()
        return

    if not epochs:
        console.print("[yellow]No epochs found matching criteria[/yellow]")
        return

    # Backfill
    collect_preboundary_batch(
        args.db_path,
        epochs,
        args.live_db_path,
        resume=resume,
        snapshot_source=args.snapshot_source,
        rpc_url=rpc_url,
        max_gauges=args.max_gauges,
        min_reward_usd=args.min_reward_usd,
        log_file=args.log_file,
    )


if __name__ == "__main__":
    main()
