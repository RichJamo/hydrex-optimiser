"""Phase 2 of claim_and_swap_rewards.py: epoch/gauge/bribe/token discovery and the
Phase 1-2 JSON artifact export.
"""

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from eth_utils import to_checksum_address
from rich.console import Console
from rich.table import Table
from web3 import Web3

from config import BRIBE_ABI
from config.settings import (
    DUST_THRESHOLD_USD,
    HYDREX_FACTORY_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    SLIPPAGE_START_PCT,
    SWAP_DEADLINE_SECONDS,
    SWAP_RETRY_COUNT,
    USDC_ADDRESS,
)

from src.claims.swaps import ERC20_ABI

logger = logging.getLogger(__name__)

console = Console()


def parse_address_list(raw: Optional[str]) -> List[str]:
    """Parse comma/newline/space separated addresses into an ordered unique list."""
    if not raw:
        return []

    parts = re.split(r"[\s,]+", str(raw).strip())
    ordered: List[str] = []
    seen: Set[str] = set()
    for part in parts:
        if not part:
            continue
        normalized = part.strip()
        lowered = normalized.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        ordered.append(normalized)
    return ordered


# ═══ Phase 2: Epoch Resolution ═══
def resolve_target_epoch(
    conn: sqlite3.Connection, override_epoch: Optional[int]
) -> int:
    """
    Resolve target epoch for reward claiming.

    If override_epoch provided: use it (no validation)
    Otherwise: query executed_allocations for MAX(epoch) from latest closed week

    Returns: epoch (int, >= 0)
    """
    if override_epoch is not None:
        logger.info(f"Using override epoch: {override_epoch}")
        return override_epoch

    # Auto-detect latest closed epoch from executed_allocations
    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT MAX(epoch) FROM executed_allocations
        """
        )
        result = cursor.fetchone()

        if result and result[0] is not None:
            epoch = result[0]
        else:
            epoch = 0  # Default if no records

        logger.info(f"Auto-detected epoch from executed_allocations: {epoch}")
        return epoch

    except Exception as e:
        logger.warning(f"Failed to auto-detect epoch: {e}. Using default: 0")
        return 0


# ═══ Phase 2: Gauge Discovery ═══
def discover_voted_gauges(
    conn: sqlite3.Connection,
    epoch: int,
    signer_address: str,
) -> List[str]:
    """
    Discover gauges that signer voted on in target epoch.

    Primary: Query executed_allocations for gauges at (epoch, signer_address)
    Fallback: If no results, return alive gauges from gauges table

    Returns: List of checksummed gauge addresses
    """
    signer_address = to_checksum_address(signer_address)

    try:
        cursor = conn.cursor()

        # Primary: Query executed_allocations
        # executed_allocations stores per-gauge vote rows keyed by gauge_address.
        # The table does not currently persist signer address, so epoch is the selector.
        cursor.execute(
            """
            SELECT DISTINCT gauge_address
            FROM executed_allocations
            WHERE epoch = ?
        """,
            (epoch,),
        )

        gauges = [to_checksum_address(row[0]) for row in cursor.fetchall()]

        if gauges:
            logger.info(
                f"Found {len(gauges)} voted gauges in executed_allocations for epoch {epoch}"
            )
            return gauges

        # Fallback: Use alive gauges
        logger.warning(
            f"No records in executed_allocations for epoch {epoch}. Using alive gauges..."
        )
        cursor.execute(
            """
            SELECT DISTINCT address FROM gauges WHERE is_alive = 1
        """
        )
        gauges = [to_checksum_address(row[0]) for row in cursor.fetchall()]
        logger.info(f"Fallback: {len(gauges)} alive gauges from gauges table")
        return gauges

    except Exception as e:
        logger.error(f"Error discovering gauges: {e}")
        return []


def resolve_manual_claim_gauges(
    conn: sqlite3.Connection,
    gauge_addresses: Optional[str],
    pool_addresses: Optional[str],
) -> List[str]:
    """Resolve operator-supplied gauge/pool addresses into a checksummed gauge list."""
    manual_gauges = parse_address_list(gauge_addresses)
    manual_pools = parse_address_list(pool_addresses)

    if not manual_gauges and not manual_pools:
        return []

    cursor = conn.cursor()
    resolved: List[str] = []
    seen: Set[str] = set()

    unresolved_gauges: List[str] = []
    for gauge in manual_gauges:
        if not Web3.is_address(gauge):
            raise ValueError(f"Invalid gauge address: {gauge}")
        checksum = to_checksum_address(gauge)
        row = cursor.execute(
            "SELECT 1 FROM gauges WHERE lower(address) = ? LIMIT 1",
            (checksum.lower(),),
        ).fetchone()
        if not row:
            unresolved_gauges.append(gauge)
            continue
        if checksum.lower() not in seen:
            seen.add(checksum.lower())
            resolved.append(checksum)

    unresolved_pools: List[str] = []
    if manual_pools:
        for pool in manual_pools:
            if not Web3.is_address(pool):
                raise ValueError(f"Invalid pool address: {pool}")

        placeholders = ",".join("?" * len(manual_pools))
        rows = cursor.execute(
            f"""
            SELECT address, COALESCE(pool, '') AS pool_address
            FROM gauges
            WHERE lower(COALESCE(pool, '')) IN ({placeholders})
            """,
            [pool.lower() for pool in manual_pools],
        ).fetchall()

        gauge_by_pool = {
            str(pool_address).lower(): to_checksum_address(address)
            for address, pool_address in rows
            if address and pool_address
        }
        for pool in manual_pools:
            matched = gauge_by_pool.get(pool.lower())
            if not matched:
                unresolved_pools.append(pool)
                continue
            if matched.lower() not in seen:
                seen.add(matched.lower())
                resolved.append(matched)

    if unresolved_gauges or unresolved_pools:
        message_parts = []
        if unresolved_gauges:
            message_parts.append(f"unresolved gauges={unresolved_gauges}")
        if unresolved_pools:
            message_parts.append(f"unresolved pools={unresolved_pools}")
        raise ValueError(
            "Could not resolve manual claim targets: " + "; ".join(message_parts)
        )

    logger.info(
        "Resolved manual claim target override: gauges=%s pools=%s final_gauges=%s",
        len(manual_gauges),
        len(manual_pools),
        len(resolved),
    )
    return resolved


# ═══ Phase 2: Bribe Mapping ═══
def map_gauges_to_bribes(
    conn: sqlite3.Connection,
    gauges: List[str],
) -> Dict[str, Tuple[str, str]]:
    """
    Map gauges to (internal_bribe, external_bribe) contracts.

    Queries gauges table for internal_bribe and external_bribe columns.

    Returns: Dict[gauge_address] = (internal_bribe_address, external_bribe_address)
    """
    if not gauges:
        logger.warning("No gauges to map")
        return {}

    try:
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(gauges))

        lower_gauges = [g.lower() for g in gauges]

        cursor.execute(
            f"""
            SELECT address, internal_bribe, external_bribe
            FROM gauges
            WHERE lower(address) IN ({placeholders})
        """,
            lower_gauges,
        )

        mapping = {}
        for row in cursor.fetchall():
            gauge_addr = to_checksum_address(row[0])
            internal_bribe = to_checksum_address(row[1]) if row[1] else None
            external_bribe = to_checksum_address(row[2]) if row[2] else None
            mapping[gauge_addr] = (internal_bribe, external_bribe)

        logger.info(f"Mapped {len(mapping)} gauges to bribes")
        return mapping

    except Exception as e:
        logger.error(f"Error mapping gauges to bribes: {e}")
        return {}


# ═══ Phase 2: Token Enumeration ═══
def enumerate_reward_tokens_from_bribes(
    w3: Web3,
    conn: sqlite3.Connection,
    bribe_contracts: List[str],
    force_onchain_refresh: bool = False,
) -> Dict[str, Dict]:
    """
        Discover reward tokens for bribe contracts.

        Cache-first strategy:
            1. Try loading from bribe_reward_tokens + token_metadata tables
            2. If cache miss (or force_onchain_refresh), query BribeV2 contracts on-chain

    For each bribe contract:
      1. Call rewardsListLength() to get count
      2. Call rewardTokens(i) for i in 0..count-1
      3. Deduplicate and checksum

    Returns: Dict[token_address] = {
      "symbol": str,
      "decimals": int,
      "bribes": [bribe_address_1, ...]
    }
    """
    if not bribe_contracts:
        logger.warning("No bribe contracts to enumerate")
        return {}

    bribe_contracts_clean = [to_checksum_address(b) for b in bribe_contracts if b]
    if not bribe_contracts_clean:
        return {}

    if not force_onchain_refresh:
        cached_tokens = load_reward_tokens_from_cache(conn, bribe_contracts_clean)
        if cached_tokens:
            logger.info(
                f"Loaded {len(cached_tokens)} reward tokens from DB cache "
                f"for {len(bribe_contracts_clean)} bribe contracts"
            )
            return cached_tokens

        logger.info(
            "No cached reward-token mappings found; falling back to on-chain enumeration"
        )

    token_to_bribes: Dict[str, Set[str]] = {}

    try:
        for bribe_addr in bribe_contracts_clean:
            if not bribe_addr:
                continue

            try:
                bribe_contract = w3.eth.contract(address=bribe_addr, abi=BRIBE_ABI)

                # Get token count
                length = bribe_contract.functions.rewardsListLength().call()

                if length == 0:
                    logger.debug(f"Bribe {bribe_addr} has no reward tokens")
                    continue

                # Enumerate tokens
                logger.debug(f"Enumerating {length} tokens from bribe {bribe_addr}")
                for i in range(
                    min(length, 500)
                ):  # Safety limit: 500 tokens per contract
                    try:
                        token_addr = bribe_contract.functions.rewardTokens(i).call()
                        token_addr = to_checksum_address(token_addr)

                        if token_addr not in token_to_bribes:
                            token_to_bribes[token_addr] = set()
                        token_to_bribes[token_addr].add(bribe_addr)

                    except Exception as e:
                        logger.debug(f"Error fetching token {i} from {bribe_addr}: {e}")
                        continue

            except Exception as e:
                logger.warning(f"Error enumerating tokens from {bribe_addr}: {e}")
                continue

    except Exception as e:
        logger.error(f"Error in token enumeration: {e}")
        return {}

    # Fetch token metadata (symbol, decimals)
    token_metadata = {}
    for token_addr in token_to_bribes:
        try:
            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)

            symbol = token_contract.functions.symbol().call()
            decimals = token_contract.functions.decimals().call()

            token_metadata[token_addr] = {
                "symbol": symbol,
                "decimals": decimals,
                "bribes": sorted(list(token_to_bribes[token_addr])),
            }
            logger.debug(
                f"Fetched metadata for {token_addr}: {symbol} ({decimals} decimals)"
            )

        except Exception as e:
            logger.warning(f"Error fetching metadata for {token_addr}: {e}")
            token_metadata[token_addr] = {
                "symbol": "UNKNOWN",
                "decimals": 18,
                "bribes": sorted(list(token_to_bribes[token_addr])),
            }

    logger.info(f"Enumerated {len(token_metadata)} unique reward tokens")
    return token_metadata


def load_reward_tokens_from_cache(
    conn: sqlite3.Connection,
    bribe_contracts: List[str],
) -> Dict[str, Dict]:
    """Load reward token mappings from local DB cache tables.

    Uses:
      - bribe_reward_tokens(bribe_contract, reward_token, is_reward_token)
      - token_metadata(token_address, symbol, decimals)
    """
    if not bribe_contracts:
        return {}

    try:
        cursor = conn.cursor()
        placeholders = ",".join("?" * len(bribe_contracts))
        lower_bribes = [b.lower() for b in bribe_contracts]

        cursor.execute(
            f"""
            SELECT
                lower(brt.reward_token) AS reward_token,
                brt.bribe_contract,
                tm.symbol,
                tm.decimals
            FROM bribe_reward_tokens brt
            LEFT JOIN token_metadata tm
                ON lower(tm.token_address) = lower(brt.reward_token)
            WHERE lower(brt.bribe_contract) IN ({placeholders})
              AND brt.is_reward_token = 1
            """,
            lower_bribes,
        )

        rows = cursor.fetchall()
        if not rows:
            return {}

        token_metadata: Dict[str, Dict] = {}
        for reward_token, bribe_contract, symbol, decimals in rows:
            token_addr = to_checksum_address(reward_token)
            bribe_addr = to_checksum_address(bribe_contract)

            if token_addr not in token_metadata:
                token_metadata[token_addr] = {
                    "symbol": symbol or "UNKNOWN",
                    "decimals": int(decimals) if decimals is not None else 18,
                    "bribes": [],
                }

            if bribe_addr not in token_metadata[token_addr]["bribes"]:
                token_metadata[token_addr]["bribes"].append(bribe_addr)

        for token_addr in token_metadata:
            token_metadata[token_addr]["bribes"].sort()

        return token_metadata

    except Exception as e:
        logger.warning(f"Failed to load reward token cache: {e}")
        return {}


def ensure_reward_token_metadata(
    w3: Web3,
    conn: sqlite3.Connection,
    reward_tokens: Dict[str, Dict],
    token_address: str,
) -> None:
    """Ensure a token has symbol/decimals metadata in reward token map."""
    token_cs = to_checksum_address(token_address)
    if token_cs in reward_tokens:
        return

    symbol: Optional[str] = None
    decimals: Optional[int] = None

    try:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT symbol, decimals
            FROM token_metadata
            WHERE lower(token_address) = lower(?)
            LIMIT 1
            """,
            (token_cs,),
        )
        row = cursor.fetchone()
        if row:
            symbol = row[0]
            decimals = int(row[1]) if row[1] is not None else None
    except Exception as e:
        logger.warning(f"Could not load token metadata from DB for {token_cs}: {e}")

    if symbol is None or decimals is None:
        try:
            token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
            symbol = symbol or token_contract.functions.symbol().call()
            decimals = (
                decimals
                if decimals is not None
                else int(token_contract.functions.decimals().call())
            )
        except Exception as e:
            logger.warning(f"Could not fetch on-chain metadata for {token_cs}: {e}")

    reward_tokens[token_cs] = {
        "symbol": symbol or "UNKNOWN",
        "decimals": int(decimals) if decimals is not None else 18,
        "bribes": [],
    }
    logger.info(
        f"Added reward token metadata for swap tracking: {token_cs} "
        f"({reward_tokens[token_cs]['symbol']})"
    )


# ═══ Phase 2: Claim Summary Output ═══
def build_claim_summary(
    gauges: List[str],
    gauge_to_bribes: Dict[str, Tuple[str, str]],
    reward_tokens: Dict[str, Dict],
) -> None:
    """
    Build and display Rich table showing claim targets.

    Table columns:
      - Gauge Address (checksum)
      - Internal Bribe
      - External Bribe
      - Token Count
    """
    table = Table(
        title="Claim Targets Summary (Phase 2)",
        show_header=True,
        header_style="bold cyan",
    )

    table.add_column("Gauge Address", style="dim")
    table.add_column("Internal Bribe", style="green")
    table.add_column("External Bribe", style="blue")
    table.add_column("Tokens", justify="right")

    for gauge_addr in sorted(gauges):
        internal_bribe, external_bribe = gauge_to_bribes.get(gauge_addr, (None, None))

        gauge_bribes = set()
        if internal_bribe:
            gauge_bribes.add(internal_bribe)
        if external_bribe:
            gauge_bribes.add(external_bribe)

        # Count tokens attached to this gauge's bribe contracts.
        token_count = sum(
            1
            for token_info in reward_tokens.values()
            if gauge_bribes.intersection(set(token_info.get("bribes", [])))
        )

        table.add_row(
            gauge_addr,
            internal_bribe or "-",
            external_bribe or "-",
            str(token_count),
        )

    console.print(table)


# ═══ Export Artifact (JSON) ═══
def export_claim_artifact(
    output_file: str,
    epoch: int,
    signer_address: str,
    gauges: List[str],
    gauge_to_bribes: Dict[str, Tuple[str, str]],
    reward_tokens: Dict[str, Dict],
    claim_results: Optional[List[Dict]] = None,
    swap_results: Optional[List[Dict]] = None,
) -> None:
    """
    Export claim targets as JSON artifact for Phase 3 handoff.

    Schema: {
      "phase": "1_2",
      "timestamp": timestamp,
      "epoch": int,
      "signer": address,
      "gauges": [address, ...],
      "gauge_to_bribes": {gauge_addr: [internal, external], ...},
      "reward_tokens": {token_addr: {symbol, decimals, bribes}, ...},
      "config": {router, factory, usdc, slippage, dust, ...}
    }
    """
    signer_address = to_checksum_address(signer_address)

    artifact = {
        "phase": "1_2",
        "timestamp": int(time.time()),
        "epoch": epoch,
        "signer": signer_address,
        "gauges": sorted([to_checksum_address(g) for g in gauges]),
        "gauge_to_bribes": {
            to_checksum_address(k): [v[0], v[1]] for k, v in gauge_to_bribes.items()
        },
        "reward_tokens": {to_checksum_address(k): v for k, v in reward_tokens.items()},
        "config": {
            "hydrex_router": HYDREX_ROUTER_ADDRESS,
            "hydrex_factory": HYDREX_FACTORY_ADDRESS,
            "usdc": USDC_ADDRESS,
            "dust_threshold_usd": DUST_THRESHOLD_USD,
            "slippage_start_pct": SLIPPAGE_START_PCT,
            "swap_retry_count": SWAP_RETRY_COUNT,
            "swap_deadline_seconds": SWAP_DEADLINE_SECONDS,
        },
        "claim_results": claim_results or [],
        "swap_results": swap_results or [],
    }

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    with open(output_file, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=True)

    logger.info(f"Exported claim artifact to: {output_file}")
