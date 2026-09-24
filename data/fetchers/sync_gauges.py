#!/usr/bin/env python3
"""
Keep the local gauge universe in step with the Voter contract.

Every consumer of candidate gauges (live snapshot, boundary votes, postmortem)
reads the `gauges` / `gauge_bribe_mapping` / `bribe_reward_tokens` tables. Nothing
refreshed them after Feb 2026, so by Sep 2026 123 of 414 on-chain gauges were
invisible to the optimizer. This module adds any gauge the Voter knows about that
the DB does not.

Contract for sync_new_gauges():
  Preconditions:
    - `gauges` exists (legacy table; created here with IF NOT EXISTS if absent).
  Postconditions:
    - Every on-chain gauge fully read this run is present in `gauges`,
      `gauge_bribe_mapping`, and has its approved reward tokens in
      `bribe_reward_tokens`.
    - Existing rows are never modified (INSERT OR IGNORE), so is_alive and
      historical data written elsewhere are left alone.
  Invariants:
    - A gauge is written only when its pool, both bribe addresses, its isAlive
      flag and both reward token lists were read successfully. A partial read writes nothing for that
      gauge, so the next run retries it instead of freezing an incomplete row.
    - All writes for one run commit in a single transaction.

Usage:
  python -m data.fetchers.sync_gauges            # sync against latest block
  python -m data.fetchers.sync_gauges --dry-run  # report only
"""

import argparse
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from multicall import Call, Multicall
from rich.console import Console
from web3 import Web3

from config.settings import DATABASE_PATH, VOTER_ADDRESS
from src.schema import BRIBE_REWARD_TOKENS, GAUGE_BRIBE_MAPPING, GAUGES_LEGACY

load_dotenv()
console = Console()

ZERO_ADDRESS = "0x" + "0" * 40
MULTICALL_BATCH_SIZE = 200
MAX_REWARD_TOKENS_PER_BRIBE = 500  # same safety limit as enumerate_bribe_tokens

# Runs before this sync's own inserts, so it only counts gauges that were in the
# table previously without a mapping row.
_UNMAPPED_GAUGES_WHERE = """
    WHERE address IS NOT NULL
      AND LOWER(address) NOT IN (SELECT LOWER(gauge_address) FROM gauge_bribe_mapping)
"""


def _ok(success, value):
    return value if success else None


def _is_set(address: Optional[str]) -> bool:
    return bool(address) and str(address).lower() != ZERO_ADDRESS


class VoterChainReader:
    """Batched on-chain reads needed by the sync. Returns None for failed calls."""

    def __init__(self, w3: Web3, voter_address: str, block="latest"):
        self.w3 = w3
        self.voter = Web3.to_checksum_address(voter_address)
        self.block = block

    def _multicall(self, calls: List[Call]) -> Dict:
        results: Dict = {}
        for start in range(0, len(calls), MULTICALL_BATCH_SIZE):
            batch = calls[start : start + MULTICALL_BATCH_SIZE]
            results.update(
                Multicall(
                    batch, _w3=self.w3, block_id=self.block, require_success=False
                )()
            )
        return results

    def pool_count(self) -> int:
        out = self._multicall([Call(self.voter, ["length()(uint256)"], [("n", _ok)])])
        if out.get("n") is None:
            raise RuntimeError("Voter.length() call failed")
        return int(out["n"])

    def pools(self, count: int) -> Dict[int, Optional[str]]:
        return self._multicall(
            [
                Call(self.voter, ["pools(uint256)(address)", i], [(i, _ok)])
                for i in range(count)
            ]
        )

    def gauges_for_pools(self, pools: List[str]) -> Dict[str, Optional[str]]:
        return self._multicall(
            [
                Call(
                    self.voter,
                    ["gauges(address)(address)", Web3.to_checksum_address(p)],
                    [(p, _ok)],
                )
                for p in pools
            ]
        )

    def bribes_for_gauges(self, gauges: List[str]) -> Dict[Tuple[str, str], object]:
        """Keys: ("internal"|"external", gauge) -> bribe address, ("alive", gauge) -> bool."""
        calls = []
        for g in gauges:
            checksum = Web3.to_checksum_address(g)
            calls.append(
                Call(
                    self.voter,
                    ["internal_bribes(address)(address)", checksum],
                    [(("internal", g), _ok)],
                )
            )
            calls.append(
                Call(
                    self.voter,
                    ["external_bribes(address)(address)", checksum],
                    [(("external", g), _ok)],
                )
            )
            calls.append(
                Call(
                    self.voter,
                    ["isAlive(address)(bool)", checksum],
                    [(("alive", g), _ok)],
                )
            )
        return self._multicall(calls)

    def reward_tokens_for_bribes(
        self, bribes: List[str]
    ) -> Dict[str, Optional[List[str]]]:
        """Approved reward tokens per bribe; None when the list could not be read."""
        lengths = self._multicall(
            [
                Call(
                    Web3.to_checksum_address(b),
                    ["rewardsListLength()(uint256)"],
                    [(b, _ok)],
                )
                for b in bribes
            ]
        )
        token_calls = []
        for b in bribes:
            if lengths.get(b) is None:
                continue
            for i in range(min(int(lengths[b]), MAX_REWARD_TOKENS_PER_BRIBE)):
                token_calls.append(
                    Call(
                        Web3.to_checksum_address(b),
                        ["rewardTokens(uint256)(address)", i],
                        [((b, i), _ok)],
                    )
                )
        token_at = self._multicall(token_calls)

        approval_calls = [
            Call(
                Web3.to_checksum_address(b),
                ["isRewardToken(address)(bool)", Web3.to_checksum_address(t)],
                [((b, t.lower()), _ok)],
            )
            for (b, _i), t in token_at.items()
            if _is_set(t)
        ]
        approved = self._multicall(approval_calls)

        result: Dict[str, Optional[List[str]]] = {}
        for b in bribes:
            if lengths.get(b) is None:
                result[b] = None
                continue
            count = min(int(lengths[b]), MAX_REWARD_TOKENS_PER_BRIBE)
            tokens: List[str] = []
            complete = True
            for i in range(count):
                token = token_at.get((b, i))
                if token is None:
                    complete = False
                    break
                if not _is_set(token):
                    continue
                flag = approved.get((b, token.lower()))
                if flag is None:
                    complete = False
                    break
                if flag:
                    tokens.append(token.lower())
            result[b] = sorted(set(tokens)) if complete else None
        return result


@dataclass
class GaugeSyncResult:
    on_chain_pools: int
    known_gauges: int
    new_gauges: List[Tuple[str, str]] = field(default_factory=list)  # (gauge, pool)
    skipped_incomplete: int = 0
    reward_pairs_added: int = 0
    mappings_backfilled: int = 0


def ensure_gauge_tables(conn: sqlite3.Connection) -> None:
    conn.execute(GAUGES_LEGACY)
    conn.execute(GAUGE_BRIBE_MAPPING)
    conn.execute(BRIBE_REWARD_TOKENS)
    conn.commit()


def sync_new_gauges(
    conn: sqlite3.Connection,
    reader: VoterChainReader,
    dry_run: bool = False,
    now_ts: Optional[int] = None,
) -> GaugeSyncResult:
    """Add on-chain gauges missing from the DB. See module docstring for the contract."""
    ensure_gauge_tables(conn)
    now_ts = int(now_ts if now_ts is not None else time.time())

    known: Set[str] = {
        str(r[0]).lower()
        for r in conn.execute("SELECT address FROM gauges WHERE address IS NOT NULL")
    }
    count = reader.pool_count()
    result = GaugeSyncResult(on_chain_pools=count, known_gauges=len(known))

    pools_by_index = reader.pools(count)
    pools = [str(p).lower() for p in pools_by_index.values() if _is_set(p)]
    unreadable_pools = count - len(pools)
    gauge_by_pool = reader.gauges_for_pools(pools)

    candidates: List[Tuple[str, str]] = []  # (gauge, pool)
    for pool in pools:
        gauge = gauge_by_pool.get(pool)
        if gauge is None:
            unreadable_pools += 1
            continue
        if _is_set(gauge) and gauge.lower() not in known:
            candidates.append((gauge.lower(), pool))

    bribes = reader.bribes_for_gauges([g for g, _ in candidates]) if candidates else {}
    bribe_addresses = sorted(
        {
            str(b).lower()
            for key, b in bribes.items()
            if key[0] != "alive" and _is_set(b)
        }
    )
    tokens_by_bribe = (
        reader.reward_tokens_for_bribes(bribe_addresses) if bribe_addresses else {}
    )

    gauge_rows, mapping_rows, token_rows = [], [], set()
    for gauge, pool in candidates:
        internal = bribes.get(("internal", gauge))
        external = bribes.get(("external", gauge))
        alive = bribes.get(("alive", gauge))
        if internal is None or external is None or alive is None:
            result.skipped_incomplete += 1
            continue
        gauge_bribes = [str(b).lower() for b in (internal, external) if _is_set(b)]
        if any(tokens_by_bribe.get(b) is None for b in gauge_bribes):
            result.skipped_incomplete += 1
            continue

        internal_l = str(internal).lower() if _is_set(internal) else ""
        external_l = str(external).lower() if _is_set(external) else ""
        # is_alive is a point-in-time read; the live snapshot re-checks liveness at
        # its own query block, but claim discovery filters on this column directly.
        gauge_rows.append(
            (gauge, pool, internal_l, external_l, 1 if alive else 0, now_ts, now_ts)
        )
        mapping_rows.append((gauge, internal_l, external_l, now_ts))
        for b in gauge_bribes:
            for token in tokens_by_bribe[b]:
                token_rows.add((b, token, now_ts))
        result.new_gauges.append((gauge, pool))

    result.skipped_incomplete += unreadable_pools
    result.reward_pairs_added = len(token_rows)
    # Gauges already in `gauges` but never mapped (the one-time mapping build only
    # took gauges with past bribes) are otherwise excluded from every snapshot.
    result.mappings_backfilled = int(
        conn.execute(
            f"SELECT COUNT(*) FROM gauges {_UNMAPPED_GAUGES_WHERE}"
        ).fetchone()[0]
    )

    if dry_run:
        return result

    with conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO gauges
            (address, pool, internal_bribe, external_bribe, is_alive, created_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            gauge_rows,
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO gauge_bribe_mapping
            (gauge_address, internal_bribe, external_bribe, created_at)
            VALUES (?, ?, ?, ?)
            """,
            mapping_rows,
        )
        conn.execute(
            f"""
            INSERT OR IGNORE INTO gauge_bribe_mapping
            (gauge_address, internal_bribe, external_bribe, created_at)
            SELECT LOWER(address), LOWER(COALESCE(internal_bribe, '')),
                   LOWER(COALESCE(external_bribe, '')), ?
            FROM gauges {_UNMAPPED_GAUGES_WHERE}
            """,
            (now_ts,),
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO bribe_reward_tokens
            (bribe_contract, reward_token, is_reward_token, updated_at)
            VALUES (?, ?, 1, ?)
            """,
            sorted(token_rows),
        )
    return result


def print_sync_result(result: GaugeSyncResult, elapsed_s: float) -> None:
    colour = "yellow" if result.new_gauges or result.skipped_incomplete else "green"
    console.print(
        f"[{colour}]Gauge sync: on-chain pools={result.on_chain_pools}, known={result.known_gauges}, "
        f"added={len(result.new_gauges)}, skipped_incomplete={result.skipped_incomplete}, "
        f"reward_pairs_added={result.reward_pairs_added}, "
        f"mappings_backfilled={result.mappings_backfilled} ({elapsed_s:.2f}s)[/{colour}]"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add Voter gauges missing from the local DB"
    )
    parser.add_argument("--db-path", default=DATABASE_PATH)
    parser.add_argument("--rpc", default=os.getenv("RPC_URL", ""))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be added without writing",
    )
    args = parser.parse_args()

    if not args.rpc:
        console.print("[red]RPC_URL missing[/red]")
        raise SystemExit(1)

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    conn = sqlite3.connect(args.db_path)
    started = time.time()
    result = sync_new_gauges(
        conn, VoterChainReader(w3, VOTER_ADDRESS), dry_run=args.dry_run
    )
    print_sync_result(result, time.time() - started)
    for gauge, pool in result.new_gauges[:20]:
        console.print(
            f"  [dim]{'would add' if args.dry_run else 'added'} gauge={gauge} pool={pool}[/dim]"
        )
    if len(result.new_gauges) > 20:
        console.print(f"  [dim]... and {len(result.new_gauges) - 20} more[/dim]")
    conn.close()


if __name__ == "__main__":
    main()
