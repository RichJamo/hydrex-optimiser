#!/usr/bin/env python3
"""
Claim and Swap Rewards: Phase 1-3 - Discovery and Claim Execution.

Orchestrates batch reward claiming and USDC swap planning:

PHASE 1: Wallet Integration & Preflight Checks
  - Load wallet from 1Password CLI, file, environment variable, or raw key
  - Validate RPC connectivity, chain ID (8453), signer nonce, gas price
  - Preflight checks ensure safe execution environment

PHASE 2: Claim Target Construction & Discovery
  - Resolve target epoch (auto-detect latest closed or user override)
  - Discover voted gauges from executed_allocations or fallback to alive gauges
  - Map gauges to internal/external bribe contracts via gauges table
  - Enumerate reward tokens from BribeV2 contracts via bulk rewardTokens() queries
  - Build claim summary table (Rich output)
  - Export JSON artifact for Phase 3 (Batch Claim Execution) handoff

Safety Features:
  - Dry-run mode by default (--dry-run=true explicit flag required)
  - Wallet loading with 3-source fallback (1Password → file/env → error)
  - 1Password integration via op CLI subprocess (FileNotFoundError handled)
  - Preflight validation before business logic
  - Read-only discovery phase (no transactions, no DB writes)
  - Comprehensive module-level logging
  - Rich console output for CLI clarity

Configuration:
  - Hydrex Router: 0x6f4bE24d7dC93b6ffcCAb3Fd0747c5817Cea3F9e
  - Hydrex Factory/Deployer: 0x36077D39cdC65E1e3FB65810430E5b2c4D5fA29E
  - VoterV5: 0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b
  - USDC (Base): 0x833589fcd6edb6e08f4c7c32d4f71b54bda02913
  - Dust threshold: $1 USD
  - Slippage: 0.5% starting (retries up to 5% via Phase 4)

Usage (Dry-Run, Read-Only):
  python scripts/claim_and_swap_rewards.py \\
    --wallet "op://vault/item/field" \\
    --epoch 100 \\
    --dry-run true

Usage (Phase 3+ requires explicit --broadcast flag):
  python scripts/claim_and_swap_rewards.py \\
    --wallet "op://vault/item/field" \\
    --epoch 100 \\
    --broadcast true

Next Phase: Phase 3 (Batch Claim Execution)
  - Constructs claimBribes/claimFees batch calls
  - Estimates gas with 1.2× buffer
  - Submits transactions for claim execution
"""

import argparse
import json
import logging
import os
import sqlite3
import subprocess
import sys
import time
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from eth_account import Account
from eth_utils import to_checksum_address
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from web3 import Web3
from web3.exceptions import TransactionNotFound

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    DATABASE_PATH,
    DUST_THRESHOLD_USD,
    HYDREX_FACTORY_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    RPC_URL,
    SLIPPAGE_START_PCT,
    SWAP_DEADLINE_SECONDS,
    SWAP_RETRY_COUNT,
    USDC_ADDRESS,
    VOTER_ADDRESS,
    WEEK,
)

load_dotenv()

# ═══ Logging Setup ═══
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

console = Console()

# ═══ Constants ═══
ONE_E18 = 10**18
CHAIN_ID = 8453  # Base mainnet


def parse_bool(value: str) -> bool:
    """Parse common truthy/falsey CLI values."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


# ═══ Load ABIs ═══
def _load_abi(filename: str) -> List[Dict]:
    """Load ABI from JSON file in workspace root."""
    abi_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        filename
    )
    with open(abi_path, "r") as f:
        return json.load(f)


VOTER_ABI = _load_abi("voterv5_abi.json")
BRIBE_ABI = _load_abi("bribev2_abi.json")

# Standard ERC20 ABI (minimal for token operations)
ERC20_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# ═══ Wallet Loading (Phase 1) ═══
def load_wallet_from_1password(vault_item_field: str) -> Account:
    """
    Load wallet from 1Password CLI.
    
    vault_item_field format: "vault/item/field"
    Example: "Personal/my_hot_wallet/private_key"
    
    Calls: op item get vault/item --fields field
    
    Returns: eth_account.Account object
    Raises: FileNotFoundError if `op` CLI not installed
    Raises: Exception if op CLI fails
    """
    try:
        parts = vault_item_field.split("/")
        if len(parts) != 3:
            raise ValueError(f"Invalid format: {vault_item_field}. Expected vault/item/field.")
        
        vault, item, field = parts
        cmd = ["op", "item", "get", f"{vault}/{item}", "--fields", field, "--reveal"]
        
        logger.info(f"Fetching private key from 1Password: op://{vault_item_field}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        
        if result.returncode != 0:
            raise Exception(f"op CLI failed: {result.stderr}")
        
        private_key = result.stdout.strip()
        if not private_key:
            raise ValueError("Empty private key returned from 1Password")
        
        # Remove 0x prefix if present
        if private_key.startswith("0x"):
            private_key = private_key[2:]
        
        account = Account.from_key(private_key)
        logger.info(f"Loaded wallet from 1Password: {account.address}")
        return account
    
    except FileNotFoundError:
        logger.error("op CLI not found. Install 1Password CLI: https://developer.1password.com/docs/cli/")
        raise


def load_wallet_from_file_or_env(source: str) -> Account:
    """
    Load wallet from file path, $ENV_VAR, or raw private key.
    
    Examples:
      - "/path/to/key.txt" -> reads file
      - "$MY_PK_ENV_VAR" -> reads environment variable
      - "0x..." -> treats as raw key
    
    Returns: eth_account.Account object
    Raises: FileNotFoundError if file doesn't exist
    Raises: KeyError if env var not found
    """
    source = source.strip()
    
    # If starts with $, treat as environment variable
    if source.startswith("$"):
        env_var = source[1:]
        private_key = os.getenv(env_var)
        if not private_key:
            raise KeyError(f"Environment variable {env_var} not found")
        logger.info(f"Loaded wallet from env var: {env_var}")
    
    # If file exists, read it
    elif os.path.isfile(source):
        with open(source, "r") as f:
            private_key = f.read().strip()
        logger.info(f"Loaded wallet from file: {source}")
    
    # Otherwise treat as raw key
    else:
        private_key = source
        logger.info("Using raw private key source")
    
    # Remove 0x prefix if present
    if private_key.startswith("0x"):
        private_key = private_key[2:]
    
    account = Account.from_key(private_key)
    logger.info(f"Loaded wallet: {account.address}")
    return account


def load_wallet(wallet_source: Optional[str]) -> Account:
    """
    Load wallet with 3-source fallback chain:
    
    1. CLI argument (if provided)
    2. TEST_WALLET_PK environment variable
    3. Error (no wallet source available)
    
    Returns: eth_account.Account object
    """
    # Priority 1: CLI argument
    if wallet_source:
        if wallet_source.startswith("op://"):
            # 1Password format
            vault_item_field = wallet_source[5:]  # Strip "op://" prefix
            return load_wallet_from_1password(vault_item_field)
        else:
            # File, env var, or raw key
            return load_wallet_from_file_or_env(wallet_source)
    
    # Priority 2: TEST_WALLET_PK env var
    test_wallet_pk = os.getenv("TEST_WALLET_PK")
    if test_wallet_pk:
        logger.info("Using TEST_WALLET_PK environment variable")
        return load_wallet_from_file_or_env(test_wallet_pk)
    
    # Priority 3: Error
    raise ValueError(
        "No wallet source provided. Use --wallet flag or set TEST_WALLET_PK env var."
    )


# ═══ Preflight Checks (Phase 1) ═══
def preflight_checks(w3: Web3, signer: Account) -> None:
    """
    Validate execution environment before business logic.
    
    Checks:
      - RPC connectivity (web3.isConnected())
      - Chain ID is Base mainnet (8453)
      - Signer has valid nonce
      - Gas price is available
    
    Raises: Exception if any check fails
    """
    logger.info("Running preflight checks...")
    
    # Check RPC connectivity
    if not w3.is_connected():
        raise Exception("RPC not connected")
    logger.info(f"✓ RPC connected: {RPC_URL}")
    
    # Check chain ID
    chain_id = w3.eth.chain_id
    if chain_id != CHAIN_ID:
        raise Exception(f"Wrong chain ID: got {chain_id}, expected {CHAIN_ID}")
    logger.info(f"✓ Chain ID correct: {chain_id}")
    
    # Check signer exists
    signer_address = to_checksum_address(signer.address)
    logger.info(f"✓ Signer address: {signer_address}")
    
    # Check signer has nonce (exists as EOA)
    try:
        nonce = w3.eth.get_transaction_count(signer_address)
        logger.info(f"✓ Signer nonce: {nonce}")
    except Exception as e:
        raise Exception(f"Failed to fetch signer nonce: {e}")
    
    # Check gas price available
    try:
        gas_price = w3.eth.gas_price
        logger.info(f"✓ Gas price available: {w3.from_wei(gas_price, 'gwei')} gwei")
    except Exception as e:
        raise Exception(f"Failed to fetch gas price: {e}")
    
    logger.info("✓ All preflight checks passed")


def wait_for_receipt(
    w3: Web3,
    tx_hash,
    timeout_seconds: int = 300,
    poll_seconds: float = 2.0,
):
    """Wait for receipt with polling to keep progress explicit in logs."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            return w3.eth.get_transaction_receipt(tx_hash)
        except TransactionNotFound:
            time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for tx receipt: {tx_hash.hex()}")


def invert_reward_tokens_to_bribes(reward_tokens: Dict[str, Dict]) -> Dict[str, List[str]]:
    """Convert token-centric map into bribe-centric token lists."""
    bribe_to_tokens: Dict[str, Set[str]] = {}

    for token_addr, token_info in reward_tokens.items():
        for bribe_addr in token_info.get("bribes", []):
            if not bribe_addr:
                continue
            bribe_cs = to_checksum_address(bribe_addr)
            token_cs = to_checksum_address(token_addr)
            if bribe_cs not in bribe_to_tokens:
                bribe_to_tokens[bribe_cs] = set()
            bribe_to_tokens[bribe_cs].add(token_cs)

    return {k: sorted(list(v)) for k, v in bribe_to_tokens.items()}


def chunk_claim_inputs(
    bribe_to_tokens: Dict[str, List[str]],
    batch_size: int,
) -> List[Tuple[List[str], List[List[str]]]]:
    """Split bribe/token arrays into contract-call-safe batch chunks."""
    if batch_size <= 0:
        raise ValueError("batch_size must be > 0")

    items = [(k, v) for k, v in bribe_to_tokens.items() if v]
    chunks: List[Tuple[List[str], List[List[str]]]] = []
    for i in range(0, len(items), batch_size):
        part = items[i : i + batch_size]
        chunks.append(([x[0] for x in part], [x[1] for x in part]))
    return chunks


def execute_claim_batches(
    w3: Web3,
    voter_contract,
    signer: Account,
    claim_for: str,
    recipient: str,
    fee_bribes: Dict[str, List[str]],
    external_bribes: Dict[str, List[str]],
    claim_batch_size: int,
    broadcast: bool,
    claim_mode: str,
) -> List[Dict]:
    """Execute or simulate Phase 3 fee/bribe claims in batches."""
    results: List[Dict] = []

    fee_chunks = chunk_claim_inputs(fee_bribes, claim_batch_size) if fee_bribes else []
    bribe_chunks = chunk_claim_inputs(external_bribes, claim_batch_size) if external_bribes else []

    actions: List[Tuple[str, List[Tuple[List[str], List[List[str]]]], str]] = []
    if claim_mode in {"all", "fees"}:
        actions.append(("fees", fee_chunks, "claimFeesToRecipientByAddress(address[],address[][],address,address)"))
    if claim_mode in {"all", "bribes"}:
        actions.append(("bribes", bribe_chunks, "claimBribesToRecipientByAddress(address[],address[][],address,address)"))

    if not actions or all(not x[1] for x in actions):
        logger.info("No claimable batch inputs found for selected mode")
        return results

    nonce = w3.eth.get_transaction_count(signer.address)
    gas_price = w3.eth.gas_price

    for action_type, chunks, signature in actions:
        if not chunks:
            continue

        fn = voter_contract.get_function_by_signature(signature)
        for batch_index, (bribes, tokens) in enumerate(chunks, start=1):
            call = fn(bribes, tokens, claim_for, recipient)
            result = {
                "action": action_type,
                "batch_index": batch_index,
                "batch_count": len(chunks),
                "bribes": bribes,
                "token_count": sum(len(x) for x in tokens),
            }

            try:
                estimated_gas = call.estimate_gas({"from": signer.address})
            except Exception as e:
                estimated_gas = 1_500_000
                logger.warning(f"Gas estimation failed for {action_type} batch {batch_index}: {e}")

            tx = call.build_transaction(
                {
                    "from": signer.address,
                    "chainId": CHAIN_ID,
                    "nonce": nonce,
                    "gas": int(estimated_gas * 1.2),
                    "gasPrice": gas_price,
                }
            )

            if not broadcast:
                logger.info(
                    f"DRY RUN {action_type} batch {batch_index}/{len(chunks)}: "
                    f"bribes={len(bribes)} tokens={result['token_count']} gas={tx['gas']}"
                )
                result.update({
                    "status": "dry_run",
                    "nonce": nonce,
                    "gas": tx["gas"],
                    "gas_price_wei": gas_price,
                })
                results.append(result)
                nonce += 1
                continue

            try:
                signed = w3.eth.account.sign_transaction(tx, signer.key)
                tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                receipt = wait_for_receipt(w3, tx_hash)

                result.update(
                    {
                        "status": "success" if receipt.status == 1 else "reverted",
                        "nonce": nonce,
                        "gas": tx["gas"],
                        "gas_price_wei": gas_price,
                        "tx_hash": tx_hash.hex(),
                        "block_number": receipt.blockNumber,
                    }
                )
                logger.info(
                    f"Broadcast {action_type} batch {batch_index}/{len(chunks)} "
                    f"status={result['status']} tx={tx_hash.hex()}"
                )
            except Exception as e:
                result.update(
                    {
                        "status": "error",
                        "nonce": nonce,
                        "gas": tx["gas"],
                        "gas_price_wei": gas_price,
                        "error": str(e),
                    }
                )
                logger.error(f"Claim tx failed for {action_type} batch {batch_index}: {e}")

            results.append(result)
            nonce += 1

    return results


def build_claim_execution_summary_table(results: List[Dict]) -> None:
    """Render concise Phase 3 batch execution summary."""
    if not results:
        return

    table = Table(title="Phase 3 Claim Execution Summary", header_style="bold cyan")
    table.add_column("Type")
    table.add_column("Batch")
    table.add_column("Bribes", justify="right")
    table.add_column("Tokens", justify="right")
    table.add_column("Status")
    table.add_column("Tx Hash")

    for r in results:
        table.add_row(
            r.get("action", "-"),
            f"{r.get('batch_index', 0)}/{r.get('batch_count', 0)}",
            str(len(r.get("bribes", []))),
            str(r.get("token_count", 0)),
            r.get("status", "-"),
            (r.get("tx_hash", "-")[:18] + "...") if r.get("tx_hash") else "-",
        )
    console.print(table)


# ═══ Phase 2: Epoch Resolution ═══
def resolve_target_epoch(conn: sqlite3.Connection, override_epoch: Optional[int]) -> int:
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
        cursor.execute("""
            SELECT MAX(epoch) FROM executed_allocations
        """)
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
        cursor.execute("""
            SELECT DISTINCT gauge_address
            FROM executed_allocations
            WHERE epoch = ?
        """, (epoch,))
        
        gauges = [to_checksum_address(row[0]) for row in cursor.fetchall()]
        
        if gauges:
            logger.info(f"Found {len(gauges)} voted gauges in executed_allocations for epoch {epoch}")
            return gauges
        
        # Fallback: Use alive gauges
        logger.warning(f"No records in executed_allocations for epoch {epoch}. Using alive gauges...")
        cursor.execute("""
            SELECT DISTINCT address FROM gauges WHERE is_alive = 1
        """)
        gauges = [to_checksum_address(row[0]) for row in cursor.fetchall()]
        logger.info(f"Fallback: {len(gauges)} alive gauges from gauges table")
        return gauges
    
    except Exception as e:
        logger.error(f"Error discovering gauges: {e}")
        return []


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

        cursor.execute(f"""
            SELECT address, internal_bribe, external_bribe
            FROM gauges
            WHERE lower(address) IN ({placeholders})
        """, lower_gauges)
        
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

        logger.info("No cached reward-token mappings found; falling back to on-chain enumeration")
    
    token_to_bribes: Dict[str, Set[str]] = {}
    
    try:
        for bribe_addr in bribe_contracts_clean:
            if not bribe_addr:
                continue
            
            try:
                bribe_contract = w3.eth.contract(
                    address=bribe_addr,
                    abi=BRIBE_ABI
                )
                
                # Get token count
                length = bribe_contract.functions.rewardsListLength().call()
                
                if length == 0:
                    logger.debug(f"Bribe {bribe_addr} has no reward tokens")
                    continue
                
                # Enumerate tokens
                logger.debug(f"Enumerating {length} tokens from bribe {bribe_addr}")
                for i in range(min(length, 500)):  # Safety limit: 500 tokens per contract
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
            token_contract = w3.eth.contract(
                address=token_addr,
                abi=ERC20_ABI
            )
            
            symbol = token_contract.functions.symbol().call()
            decimals = token_contract.functions.decimals().call()
            
            token_metadata[token_addr] = {
                "symbol": symbol,
                "decimals": decimals,
                "bribes": sorted(list(token_to_bribes[token_addr])),
            }
            logger.debug(f"Fetched metadata for {token_addr}: {symbol} ({decimals} decimals)")
        
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
        internal_bribe, external_bribe = gauge_to_bribes.get(
            gauge_addr,
            (None, None)
        )

        gauge_bribes = set()
        if internal_bribe:
            gauge_bribes.add(internal_bribe)
        if external_bribe:
            gauge_bribes.add(external_bribe)
        
        # Count tokens attached to this gauge's bribe contracts.
        token_count = sum(
            1 for token_info in reward_tokens.values()
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
            to_checksum_address(k): [v[0], v[1]]
            for k, v in gauge_to_bribes.items()
        },
        "reward_tokens": {
            to_checksum_address(k): v
            for k, v in reward_tokens.items()
        },
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
    }
    
    with open(output_file, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=True)
    
    logger.info(f"Exported claim artifact to: {output_file}")


# ═══ Main Orchestration ═══
def main():
    """
    Phase 1 & 2 Orchestration:
    
    1. Parse arguments
    2. Initialize Web3 connection
    3. Load wallet (1Password → file/env → error)
    4. Run preflight checks
    5. Resolve target epoch
    6. Discover voted gauges
    7. Map gauges to bribes
    8. Enumerate reward tokens
    9. Display claim summary (Rich table)
    10. Export JSON artifact for Phase 3
    """
    parser = argparse.ArgumentParser(
        description="Claim and Swap Rewards: Phase 1 & 2 (Safety Rails & Wallet Integration)"
    )
    
    parser.add_argument(
        "--wallet",
        type=str,
        default=None,
        help="Wallet source: op://vault/item/field | /path/to/key | $ENV_VAR | raw_key",
    )
    
    parser.add_argument(
        "--epoch",
        type=int,
        default=None,
        help="Target epoch (auto-detect if not provided)",
    )
    
    parser.add_argument(
        "--dry-run",
        type=str,
        default="true",
        help="Dry-run mode (default: true). Phase 1 & 2 is always read-only.",
    )
    
    parser.add_argument(
        "--output",
        type=str,
        default="phase1_2_artifact.json",
        help="Output artifact file (JSON)",
    )

    parser.add_argument(
        "--refresh-reward-token-cache",
        action="store_true",
        help="Bypass DB cache and re-enumerate reward tokens from on-chain bribe contracts",
    )

    parser.add_argument(
        "--broadcast",
        action="store_true",
        help="Broadcast Phase 3 claim transactions (default is dry-run only)",
    )

    parser.add_argument(
        "--claim-mode",
        type=str,
        default="all",
        choices=["all", "fees", "bribes"],
        help="Which claim calls to run in Phase 3",
    )

    parser.add_argument(
        "--claim-batch-size",
        type=int,
        default=20,
        help="Number of bribe contracts per claim transaction batch",
    )

    parser.add_argument(
        "--claim-for",
        type=str,
        default=None,
        help="Address to claim for (defaults to signer address)",
    )

    parser.add_argument(
        "--claim-recipient",
        type=str,
        default=None,
        help="Recipient of claimed tokens (defaults to signer address)",
    )
    
    parser.add_argument(
        "--loglevel",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    
    args = parser.parse_args()
    
    # Adjust logging level
    logging.getLogger().setLevel(getattr(logging, args.loglevel))
    
    logger.info("═══ Claim and Swap Rewards: Phase 1-3 ═══")
    dry_run = (not args.broadcast) or parse_bool(args.dry_run)
    if args.broadcast and parse_bool(args.dry_run):
        dry_run = False

    logger.info(f"Dry-run: {dry_run}")
    logger.info(f"Broadcast enabled: {args.broadcast}")
    
    try:
        # Initialize Web3
        logger.info(f"Connecting to RPC: {RPC_URL}")
        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        
        # Load wallet
        logger.info("Phase 1: Loading wallet...")
        signer = load_wallet(args.wallet)
        signer_address = to_checksum_address(signer.address)
        
        # Preflight checks
        logger.info("Phase 1: Running preflight checks...")
        preflight_checks(w3, signer)
        
        # Connect to database
        logger.info(f"Connecting to database: {DATABASE_PATH}")
        if not os.path.exists(DATABASE_PATH):
            raise FileNotFoundError(f"Database not found: {DATABASE_PATH}")
        
        conn = sqlite3.connect(DATABASE_PATH)
        
        # Phase 2: Epoch resolution
        logger.info("Phase 2: Resolving target epoch...")
        target_epoch = resolve_target_epoch(conn, args.epoch)
        
        # Phase 2: Gauge discovery
        logger.info("Phase 2: Discovering voted gauges...")
        gauges = discover_voted_gauges(conn, target_epoch, signer_address)
        
        if not gauges:
            console.print(
                Panel(
                    "[yellow]Warning:[/yellow] No gauges found for epoch {target_epoch}",
                    title="Claim Targets",
                )
            )
            conn.close()
            return
        
        # Phase 2: Bribe mapping
        logger.info("Phase 2: Mapping gauges to bribes...")
        gauge_to_bribes = map_gauges_to_bribes(conn, gauges)
        
        # Flatten bribe addresses for token enumeration
        all_bribes = set()
        for internal, external in gauge_to_bribes.values():
            if internal:
                all_bribes.add(internal)
            if external:
                all_bribes.add(external)
        
        # Phase 2: Token enumeration
        logger.info("Phase 2: Enumerating reward tokens...")
        if args.refresh_reward_token_cache:
            logger.info("Reward token cache refresh requested: forcing on-chain enumeration")
            reward_tokens = enumerate_reward_tokens_from_bribes(
                w3,
                conn,
                list(all_bribes),
                force_onchain_refresh=True,
            )
        else:
            reward_tokens = enumerate_reward_tokens_from_bribes(
                w3,
                conn,
                list(all_bribes),
                force_onchain_refresh=False,
            )
        
        # Display summary
        logger.info("Phase 2: Building claim summary...")
        build_claim_summary(gauges, gauge_to_bribes, reward_tokens)

        # Phase 3: Build and execute claim batches
        logger.info("Phase 3: Preparing claim batches...")
        claim_for = to_checksum_address(args.claim_for) if args.claim_for else signer_address
        claim_recipient = (
            to_checksum_address(args.claim_recipient) if args.claim_recipient else signer_address
        )

        token_by_bribe = invert_reward_tokens_to_bribes(reward_tokens)
        fee_bribes: Dict[str, List[str]] = {}
        external_bribes: Dict[str, List[str]] = {}
        for _, (internal_bribe, external_bribe) in gauge_to_bribes.items():
            if internal_bribe and internal_bribe in token_by_bribe:
                fee_bribes[internal_bribe] = token_by_bribe[internal_bribe]
            if external_bribe and external_bribe in token_by_bribe:
                external_bribes[external_bribe] = token_by_bribe[external_bribe]

        voter_contract = w3.eth.contract(address=to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
        claim_results = execute_claim_batches(
            w3=w3,
            voter_contract=voter_contract,
            signer=signer,
            claim_for=claim_for,
            recipient=claim_recipient,
            fee_bribes=fee_bribes,
            external_bribes=external_bribes,
            claim_batch_size=args.claim_batch_size,
            broadcast=args.broadcast and not dry_run,
            claim_mode=args.claim_mode,
        )
        build_claim_execution_summary_table(claim_results)
        
        # Export artifact
        logger.info(f"Exporting claim artifact...")
        export_claim_artifact(
            args.output,
            target_epoch,
            signer_address,
            gauges,
            gauge_to_bribes,
            reward_tokens,
            claim_results=claim_results,
        )
        
        # Final summary
        summary_text = f"""
Phase 1-3 Complete: Discovery + Claim Batch Execution

Epoch: {target_epoch}
Signer: {signer_address}
Gauges: {len(gauges)}
Reward Tokens: {len(reward_tokens)}
Bribe Contracts: {len(all_bribes)}
Claim Batches: {len(claim_results)}

Next Phase: Phase 4 (Swap Execution)
    - Uses claimed balances as swap inputs
    - Applies slippage ladder with retries
    - Writes weekly-review outputs

Artifact: {args.output}
"""
        
        console.print(
            Panel(summary_text.strip(), title="✓ Phase 1-3 Complete", style="green")
        )
        
        logger.info("Phase 1-3 completed successfully")
        conn.close()
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    
    except Exception as e:
        logger.error(f"Error in Phase 1-3 flow: {e}", exc_info=True)
        console.print(
            Panel(
                f"[red]Error:[/red] {e}",
                title="Phase 1-3 Failed",
                style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
