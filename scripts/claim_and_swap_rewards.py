#!/usr/bin/env python3
"""
Claim and Swap Rewards: Phase 1-6 - Discovery, Claim, Swap, Persistence, and Weekly Reporting.

Orchestrates batch reward claiming and USDC swap execution:

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
    - Hydrex Router: 0x6f4bE24d7dC93b6ffcBAb3Fd0747c5817Cea3F9e
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
import csv
import json
import logging
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from typing import Dict, List, Optional, Set, Tuple

from dotenv import load_dotenv
from eth_account import Account
from eth_utils import keccak
from eth_utils import to_checksum_address
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from web3 import Web3
from web3.exceptions import TransactionNotFound

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import (
    DATABASE_PATH,
    DELEGATED_INFLIGHT_MAX_RETRIES,
    DELEGATED_INFLIGHT_RETRY_SECONDS,
    DUST_THRESHOLD_USD,
    ESCROW_ADDRESS,
    HYDREX_FACTORY_ADDRESS,
    HYDREX_MULTI_ROUTER_ADDRESS,
    HYDREX_REWARDS_DISTRIBUTOR_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    HYDREX_ROUTING_API_URL,
    HYDREX_ROUTING_ORIGIN,
    HYDREX_ROUTING_SLIPPAGE_BPS,
    HYDREX_ROUTING_SOURCE,
    HYDREX_SWAP_EXECUTION_MODE,
    HYDREX_SWAP_SKIP_TOKENS,
    HYDREX_SWAP_DEPLOYER_ADDRESS,
    PENDING_NONCE_POLL_SECONDS,
    PENDING_NONCE_WAIT_SECONDS,
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


def build_error_selector_map(abi: List[Dict]) -> Dict[str, str]:
    """Build selector -> custom error signature map from ABI."""
    mapping: Dict[str, str] = {}
    for item in abi:
        if item.get("type") != "error":
            continue
        name = item["name"]
        types = ",".join(inp["type"] for inp in item.get("inputs", []))
        signature = f"{name}({types})"
        selector = keccak(text=signature)[:4].hex()
        mapping[selector] = signature
    return mapping


VOTER_ERROR_SELECTORS = build_error_selector_map(VOTER_ABI)

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
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
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
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
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

ROUTER_ABI = [
    {
        "inputs": [],
        "name": "poolDeployer",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "tokenIn", "type": "address"},
                    {"internalType": "address", "name": "tokenOut", "type": "address"},
                    {"internalType": "address", "name": "deployer", "type": "address"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "uint256", "name": "deadline", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountIn", "type": "uint256"},
                    {"internalType": "uint256", "name": "amountOutMinimum", "type": "uint256"},
                    {"internalType": "uint160", "name": "limitSqrtPrice", "type": "uint160"},
                ],
                "internalType": "struct ISwapRouter.ExactInputSingleParams",
                "name": "params",
                "type": "tuple",
            }
        ],
        "name": "exactInputSingle",
        "outputs": [{"internalType": "uint256", "name": "amountOut", "type": "uint256"}],
        "stateMutability": "payable",
        "type": "function",
    }
]

MULTI_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "router", "type": "address"},
                    {"internalType": "address", "name": "inputAsset", "type": "address"},
                    {"internalType": "address", "name": "outputAsset", "type": "address"},
                    {"internalType": "uint256", "name": "inputAmount", "type": "uint256"},
                    {"internalType": "uint256", "name": "minOutputAmount", "type": "uint256"},
                    {"internalType": "bytes", "name": "callData", "type": "bytes"},
                    {"internalType": "address", "name": "recipient", "type": "address"},
                    {"internalType": "string", "name": "origin", "type": "string"},
                    {"internalType": "address", "name": "referral", "type": "address"},
                    {"internalType": "uint256", "name": "referralFeeBps", "type": "uint256"},
                ],
                "internalType": "struct HydrexMultiRouter.SwapData[]",
                "name": "swaps",
                "type": "tuple[]",
            },
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "executeSwaps",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

DISTRIBUTOR_ABI = [
    {
        "inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}],
        "name": "claim",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "_tokenId", "type": "uint256"}],
        "name": "claimable",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "token",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
]

ESCROW_ABI = [
    {
        "inputs": [
            {"internalType": "address[]", "name": "feeAddresses", "type": "address[]"},
            {"internalType": "address[]", "name": "bribeAddresses", "type": "address[]"},
            {"internalType": "address[]", "name": "claimTokens", "type": "address[]"},
        ],
        "name": "claimRewards",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
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
            receipt = w3.eth.get_transaction_receipt(tx_hash)
            if receipt is not None:
                return receipt
        except TransactionNotFound:
            pass
        time.sleep(poll_seconds)
    raise TimeoutError(f"Timed out waiting for tx receipt: {tx_hash.hex()}")


def wait_for_pending_nonce_drain(
    w3: Web3,
    signer_address: str,
    timeout_seconds: int,
    poll_seconds: float,
) -> None:
    """Wait until pending nonce no longer exceeds latest nonce."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        latest_nonce = w3.eth.get_transaction_count(signer_address, "latest")
        pending_nonce = w3.eth.get_transaction_count(signer_address, "pending")
        if pending_nonce <= latest_nonce:
            return
        logger.info(
            "Waiting for delegated pending tx slot: latest_nonce=%s pending_nonce=%s",
            latest_nonce,
            pending_nonce,
        )
        time.sleep(poll_seconds)

    latest_nonce = w3.eth.get_transaction_count(signer_address, "latest")
    pending_nonce = w3.eth.get_transaction_count(signer_address, "pending")
    raise TimeoutError(
        "Pending nonce did not drain before timeout "
        f"(latest_nonce={latest_nonce}, pending_nonce={pending_nonce})"
    )


def is_delegated_inflight_error(exc: Exception) -> bool:
    """Detect provider errors emitted for delegated-account in-flight limits."""
    text = str(exc).lower()
    return "in-flight" in text and "delegated" in text and "limit" in text


def is_nonce_too_low_error(exc: Exception) -> bool:
    """Detect stale nonce errors emitted by RPC provider."""
    return "nonce too low" in str(exc).lower()


def signed_tx_raw_bytes(signed_tx):
    """Return signed tx bytes across web3/eth-account versions."""
    raw = getattr(signed_tx, "raw_transaction", None)
    if raw is None:
        raw = getattr(signed_tx, "rawTransaction", None)
    if raw is None:
        raise AttributeError("Signed transaction has neither raw_transaction nor rawTransaction")
    return raw


def decode_voter_revert(exc: Exception) -> Optional[str]:
    """Decode 4-byte revert selector from exception text when available."""
    text = str(exc)
    match = re.search(r"0x([0-9a-fA-F]{8})", text)
    if not match:
        return None
    selector = match.group(1).lower()
    return VOTER_ERROR_SELECTORS.get(selector)


def preflight_claim_authorization(
    voter_contract,
    signer: Account,
    claim_for: str,
    recipient: str,
    fee_bribes: Dict[str, List[str]],
    external_bribes: Dict[str, List[str]],
    claim_mode: str,
) -> None:
    """Run a lightweight static call to fail fast on permission issues."""
    checks: List[Tuple[str, str, Dict[str, List[str]]]] = []
    if claim_mode in {"all", "fees"} and fee_bribes:
        checks.append(
            ("fees", "claimFeesToRecipientByAddress(address[],address[][],address,address)", fee_bribes)
        )
    if claim_mode in {"all", "bribes"} and external_bribes:
        checks.append(
            (
                "bribes",
                "claimBribesToRecipientByAddress(address[],address[][],address,address)",
                external_bribes,
            )
        )

    for action_type, signature, mapping in checks:
        bribe, tokens = next(((b, t) for b, t in mapping.items() if t), (None, None))
        if not bribe or not tokens:
            continue

        try:
            fn = voter_contract.get_function_by_signature(signature)
            fn([bribe], [tokens], claim_for, recipient).call({"from": signer.address})
            logger.info(f"Phase 3 preflight authorization passed for {action_type} claims")
            return
        except Exception as e:
            decoded = decode_voter_revert(e)
            if decoded == "NotApprovedOrOwner()":
                raise PermissionError(
                    "Claim authorization failed: signer is not approved or owner for requested claim context. "
                    f"signer={signer.address} claim_for={claim_for} recipient={recipient}"
                ) from e
            logger.warning(
                f"Phase 3 preflight call for {action_type} returned {decoded or str(e)}; "
                "continuing to batch simulation."
            )
            return


def preflight_distributor_claim_authorization(
    distributor_contract,
    signer: Account,
    token_id: int,
) -> None:
    """Fail-fast authorization/precondition check for distributor claim(tokenId)."""
    try:
        claimable_amt = distributor_contract.functions.claimable(int(token_id)).call()
        logger.info(f"Phase 3 distributor preflight: tokenId={token_id} claimable={claimable_amt}")
    except Exception as e:
        logger.warning(f"Could not read claimable({token_id}) on distributor: {e}")

    try:
        distributor_contract.functions.claim(int(token_id)).estimate_gas({"from": signer.address})
        logger.info("Phase 3 distributor authorization preflight passed")
    except Exception as e:
        raise PermissionError(
            "Distributor claim authorization failed. "
            f"signer={signer.address} token_id={token_id} error={e}"
        ) from e


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
                decoded = decode_voter_revert(e)
                logger.warning(
                    f"Gas estimation failed for {action_type} batch {batch_index}: "
                    f"{decoded or str(e)}"
                )

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
                tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
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


def execute_distributor_claim(
    w3: Web3,
    distributor_contract,
    signer: Account,
    distributor_token_id: int,
    broadcast: bool,
) -> List[Dict]:
    """Execute/simulate HydrexRewardsDistributor.claim(tokenId)."""
    nonce = w3.eth.get_transaction_count(signer.address)
    gas_price = w3.eth.gas_price
    call = distributor_contract.functions.claim(int(distributor_token_id))

    try:
        estimated_gas = call.estimate_gas({"from": signer.address})
    except Exception as e:
        estimated_gas = 400000
        logger.warning(f"Distributor claim gas estimation failed: {e}")

    tx = call.build_transaction(
        {
            "from": signer.address,
            "chainId": CHAIN_ID,
            "nonce": nonce,
            "gas": int(estimated_gas * 1.2),
            "gasPrice": gas_price,
        }
    )

    result: Dict = {
        "action": "distributor_claim",
        "token_id": int(distributor_token_id),
        "nonce": nonce,
        "gas": tx["gas"],
        "gas_price_wei": gas_price,
    }

    if not broadcast:
        logger.info(
            f"DRY RUN distributor claim tokenId={distributor_token_id} gas={tx['gas']}"
        )
        result["status"] = "dry_run"
        return [result]

    try:
        signed = w3.eth.account.sign_transaction(tx, signer.key)
        tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
        receipt = wait_for_receipt(w3, tx_hash)
        result.update(
            {
                "status": "success" if receipt.status == 1 else "reverted",
                "tx_hash": tx_hash.hex(),
                "block_number": receipt.blockNumber,
            }
        )
    except Exception as e:
        result.update({"status": "error", "error": str(e)})
        logger.error(f"Distributor claim failed: {e}")

    return [result]


def execute_escrow_claim_rewards(
    w3: Web3,
    escrow_contract,
    signer: Account,
    fee_bribes: Dict[str, List[str]],
    external_bribes: Dict[str, List[str]],
    claim_mode: str,
    broadcast: bool,
) -> List[Dict]:
    """Execute/simulate Escrow.claimRewards in per-address batches for safe token/address alignment."""
    results: List[Dict] = []
    nonce = w3.eth.get_transaction_count(signer.address)
    gas_price = w3.eth.gas_price

    work_items: List[Tuple[str, str, List[str]]] = []
    if claim_mode in {"all", "fees"}:
        for bribe, tokens in fee_bribes.items():
            if tokens:
                work_items.append(("fees", bribe, sorted(tokens)))
    if claim_mode in {"all", "bribes"}:
        for bribe, tokens in external_bribes.items():
            if tokens:
                work_items.append(("bribes", bribe, sorted(tokens)))

    if not work_items:
        logger.info("No escrow claimRewards work items found for selected mode")
        return results

    for idx, (action_type, bribe_addr, claim_tokens) in enumerate(work_items, start=1):
        fee_addresses = [bribe_addr] if action_type == "fees" else []
        bribe_addresses = [bribe_addr] if action_type == "bribes" else []
        call = escrow_contract.functions.claimRewards(fee_addresses, bribe_addresses, claim_tokens)

        try:
            estimated_gas = call.estimate_gas({"from": signer.address})
        except Exception as e:
            logger.warning(
                "Escrow claimRewards preflight failed for item "
                f"{idx}/{len(work_items)} type={action_type} bribe={bribe_addr}: {e}"
            )
            results.append(
                {
                    "action": "escrow_claim_rewards",
                    "claim_type": action_type,
                    "batch_index": idx,
                    "batch_count": len(work_items),
                    "bribes": [bribe_addr],
                    "token_count": len(claim_tokens),
                    "status": "error",
                    "error": str(e),
                }
            )
            continue

        tx = call.build_transaction(
            {
                "from": signer.address,
                "chainId": CHAIN_ID,
                "nonce": nonce,
                "gas": int(estimated_gas * 1.2),
                "gasPrice": gas_price,
            }
        )

        result: Dict = {
            "action": "escrow_claim_rewards",
            "claim_type": action_type,
            "batch_index": idx,
            "batch_count": len(work_items),
            "bribes": [bribe_addr],
            "token_count": len(claim_tokens),
            "nonce": nonce,
            "gas": tx["gas"],
            "gas_price_wei": gas_price,
        }

        if not broadcast:
            logger.info(
                "DRY RUN escrow claimRewards "
                f"{idx}/{len(work_items)} type={action_type} bribe={bribe_addr} "
                f"tokens={len(claim_tokens)} gas={tx['gas']}"
            )
            result["status"] = "dry_run"
            results.append(result)
            nonce += 1
            continue

        # Retry loop: handles "in-flight transaction limit" and "gapped-nonce tx" from
        # Alchemy RPC for EIP-7702 delegated accounts.  Back off and re-sync nonce on
        # each rate-limit rejection so subsequent batches don't gap.
        _RATE_LIMIT_SIGNALS = ("in-flight transaction limit", "gapped-nonce tx")
        _backoffs = [0, 20, 40, 60]  # seconds to wait before each attempt
        _sent = False
        for _attempt, _sleep in enumerate(_backoffs):
            if _sleep:
                logger.warning(
                    f"Delegated-account rate limit on claim {idx}/{len(work_items)} "
                    f"({action_type} {bribe_addr}): retrying in {_sleep}s "
                    f"(attempt {_attempt + 1}/{len(_backoffs)})"
                )
                time.sleep(_sleep)
                nonce = w3.eth.get_transaction_count(signer.address)
                tx = call.build_transaction(
                    {
                        "from": signer.address,
                        "chainId": CHAIN_ID,
                        "nonce": nonce,
                        "gas": tx["gas"],
                        "gasPrice": gas_price,
                    }
                )
            try:
                signed = w3.eth.account.sign_transaction(tx, signer.key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
                receipt = wait_for_receipt(w3, tx_hash)
                result.update(
                    {
                        "status": "success" if receipt.status == 1 else "reverted",
                        "tx_hash": tx_hash.hex(),
                        "block_number": receipt.blockNumber,
                    }
                )
                nonce += 1
                _sent = True
                break
            except Exception as e:
                err_str = str(e)
                is_rate = any(sig in err_str for sig in _RATE_LIMIT_SIGNALS)
                if is_rate and _attempt < len(_backoffs) - 1:
                    # Will retry — don't record error yet
                    logger.warning(
                        f"Rate-limit rejection for {action_type} {bribe_addr}: {e}"
                    )
                    continue
                # Non-rate-limit error, or exhausted retries
                result.update({"status": "error", "error": err_str})
                logger.error(
                    f"Escrow claimRewards failed for {action_type} {bribe_addr}: {e}"
                )
                # Re-sync nonce from chain so subsequent txs don't inherit a gap
                try:
                    nonce = w3.eth.get_transaction_count(signer.address)
                except Exception:
                    pass
                _sent = True
                break
        if not _sent:
            # All retries exhausted on rate limit
            result.update({"status": "error", "error": "delegated-account rate limit: all retries exhausted"})
            logger.error(
                f"Escrow claimRewards rate limit for {action_type} {bribe_addr}: "
                "all backoff retries exhausted"
            )
            try:
                nonce = w3.eth.get_transaction_count(signer.address)
            except Exception:
                pass

        results.append(result)

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


def fetch_token_price_usd(conn: sqlite3.Connection, token_address: str) -> Optional[float]:
    """Fetch USD price for token from local cache table."""
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT usd_price
        FROM token_prices
        WHERE lower(token_address) = lower(?)
        ORDER BY updated_at DESC
        LIMIT 1
        """,
        (token_address,),
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def build_swap_intents(
    w3: Web3,
    conn: sqlite3.Connection,
    signer_address: str,
    reward_tokens: Dict[str, Dict],
) -> List[Dict]:
    """Build swap intents for non-USDC tokens above USD dust threshold."""
    intents: List[Dict] = []
    usdc_addr = to_checksum_address(USDC_ADDRESS)
    skip_entries = {
        entry.strip().lower()
        for entry in HYDREX_SWAP_SKIP_TOKENS.split(",")
        if entry.strip()
    }

    for token_addr, token_info in reward_tokens.items():
        token_cs = to_checksum_address(token_addr)
        if token_cs == usdc_addr:
            continue

        symbol = token_info.get("symbol", "UNKNOWN")
        if token_cs.lower() in skip_entries or symbol.lower() in skip_entries:
            logger.info(
                "Skipping %s (%s) - configured in HYDREX_SWAP_SKIP_TOKENS",
                symbol,
                token_cs,
            )
            continue

        token_contract = w3.eth.contract(address=token_cs, abi=ERC20_ABI)
        raw_balance = token_contract.functions.balanceOf(signer_address).call()
        if raw_balance <= 0:
            continue

        decimals = int(token_info.get("decimals", 18))
        balance_units = raw_balance / (10 ** decimals)

        usd_price = fetch_token_price_usd(conn, token_cs)
        if usd_price is None:
            logger.warning(f"Skipping {symbol} ({token_cs}) - no USD price in token_prices cache")
            continue

        usd_value = balance_units * usd_price
        if usd_value < DUST_THRESHOLD_USD:
            logger.info(
                f"Skipping {symbol} ({token_cs}) - below dust threshold "
                f"${usd_value:.4f} < ${DUST_THRESHOLD_USD:.2f}"
            )
            continue

        expected_usdc_out = usd_value
        intent = {
            "token": token_cs,
            "symbol": symbol,
            "decimals": decimals,
            "balance_raw": int(raw_balance),
            "balance_units": balance_units,
            "usd_price": usd_price,
            "usd_value": usd_value,
            "expected_usdc_out": expected_usdc_out,
        }
        intents.append(intent)

    intents.sort(key=lambda x: x["usd_value"], reverse=True)
    return intents


def send_contract_transaction(
    w3: Web3,
    signer: Account,
    tx: Dict,
) -> Tuple[str, int]:
    """Sign, send, and wait for receipt for a prepared transaction."""
    signed = w3.eth.account.sign_transaction(tx, signer.key)
    tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
    receipt = wait_for_receipt(w3, tx_hash)
    return tx_hash.hex(), receipt.status


def build_swap_execution_summary_table(results: List[Dict]) -> None:
    """Render concise Phase 4 swap execution summary."""
    if not results:
        return

    table = Table(title="Phase 4 Swap Execution Summary", header_style="bold cyan")
    table.add_column("Token")
    table.add_column("USD", justify="right")
    table.add_column("Attempts", justify="right")
    table.add_column("Status")
    table.add_column("Tx Hash")

    for r in results:
        table.add_row(
            r.get("symbol", "UNKNOWN"),
            f"{r.get('usd_value', 0):.2f}",
            str(r.get("attempts", 0)),
            r.get("status", "-"),
            (r.get("tx_hash", "-")[:18] + "...") if r.get("tx_hash") else "-",
        )

    console.print(table)


def ensure_claim_swap_log_table(conn: sqlite3.Connection) -> None:
    """Create claim/swap execution log table if missing."""
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS claim_swap_execution_log (
            run_ts INTEGER NOT NULL,
            epoch INTEGER NOT NULL,
            phase TEXT NOT NULL,
            action_type TEXT,
            token_address TEXT,
            token_symbol TEXT,
            bribe_count INTEGER,
            token_count INTEGER,
            amount_in_raw TEXT,
            usd_value REAL,
            slippage_pct REAL,
            status TEXT NOT NULL,
            tx_hash TEXT,
            error_text TEXT,
            metadata_json TEXT,
            PRIMARY KEY (run_ts, phase, action_type, token_address, tx_hash)
        )
        """
    )
    conn.commit()


def persist_phase_results(
    conn: sqlite3.Connection,
    run_ts: int,
    epoch: int,
    claim_results: List[Dict],
    swap_results: List[Dict],
) -> None:
    """Persist claim and swap outputs for weekly review analytics."""
    ensure_claim_swap_log_table(conn)
    cursor = conn.cursor()

    for r in claim_results:
        cursor.execute(
            """
            INSERT OR REPLACE INTO claim_swap_execution_log (
                run_ts, epoch, phase, action_type, token_address, token_symbol,
                bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
                status, tx_hash, error_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_ts,
                epoch,
                "phase3_claim",
                r.get("action"),
                None,
                None,
                len(r.get("bribes", [])),
                r.get("token_count", 0),
                None,
                None,
                None,
                r.get("status", "unknown"),
                r.get("tx_hash"),
                r.get("error"),
                json.dumps(r, sort_keys=True),
            ),
        )

    for r in swap_results:
        cursor.execute(
            """
            INSERT OR REPLACE INTO claim_swap_execution_log (
                run_ts, epoch, phase, action_type, token_address, token_symbol,
                bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
                status, tx_hash, error_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_ts,
                epoch,
                "phase4_swap",
                "swap",
                r.get("token"),
                r.get("symbol"),
                None,
                None,
                str(r.get("amount_in", "")),
                r.get("usd_value"),
                r.get("slippage_pct"),
                r.get("status", "unknown"),
                r.get("tx_hash"),
                r.get("error"),
                json.dumps(r, sort_keys=True),
            ),
        )

    cursor.execute(
        """
        INSERT OR REPLACE INTO claim_swap_execution_log (
            run_ts, epoch, phase, action_type, token_address, token_symbol,
            bribe_count, token_count, amount_in_raw, usd_value, slippage_pct,
            status, tx_hash, error_text, metadata_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_ts,
            epoch,
            "phase5_summary",
            "run_summary",
            "__run__",
            "RUN",
            len(claim_results),
            len(swap_results),
            None,
            None,
            None,
            "ok",
            None,
            None,
            json.dumps(
                {
                    "claim_results_count": len(claim_results),
                    "swap_results_count": len(swap_results),
                },
                sort_keys=True,
            ),
        ),
    )

    conn.commit()


def generate_weekly_rollup(
    conn: sqlite3.Connection,
    lookback_days: int,
) -> Dict:
    """Aggregate recent claim/swap run data for weekly review."""
    cutoff_ts = int(time.time()) - (lookback_days * 24 * 60 * 60)
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT phase, status, COUNT(*)
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
        GROUP BY phase, status
        ORDER BY phase, status
        """,
        (cutoff_ts,),
    )
    phase_status_counts = [
        {"phase": row[0], "status": row[1], "count": int(row[2])}
        for row in cursor.fetchall()
    ]

    cursor.execute(
        """
        SELECT
            COALESCE(token_symbol, 'UNKNOWN') AS token_symbol,
            COALESCE(token_address, '') AS token_address,
            COUNT(*) AS swaps,
            SUM(COALESCE(usd_value, 0.0)) AS total_usd,
            SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) AS success_count,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS error_count,
            SUM(CASE WHEN status = 'dry_run' THEN 1 ELSE 0 END) AS dry_run_count
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
          AND phase = 'phase4_swap'
        GROUP BY token_symbol, token_address
        ORDER BY total_usd DESC
        """,
        (cutoff_ts,),
    )
    swap_rollup = [
        {
            "token_symbol": row[0],
            "token_address": row[1],
            "swaps": int(row[2]),
            "total_usd": float(row[3] or 0.0),
            "success_count": int(row[4]),
            "error_count": int(row[5]),
            "dry_run_count": int(row[6]),
        }
        for row in cursor.fetchall()
    ]

    cursor.execute(
        """
        SELECT run_ts, epoch, bribe_count, token_count
        FROM claim_swap_execution_log
        WHERE run_ts >= ?
          AND phase = 'phase5_summary'
          AND action_type = 'run_summary'
        ORDER BY run_ts DESC
        """,
        (cutoff_ts,),
    )
    run_summaries = [
        {
            "run_ts": int(row[0]),
            "epoch": int(row[1]),
            "claim_results_count": int(row[2] or 0),
            "swap_results_count": int(row[3] or 0),
        }
        for row in cursor.fetchall()
    ]

    return {
        "generated_ts": int(time.time()),
        "lookback_days": lookback_days,
        "cutoff_ts": cutoff_ts,
        "phase_status_counts": phase_status_counts,
        "swap_rollup": swap_rollup,
        "run_summaries": run_summaries,
    }


def write_weekly_rollup_json(report: Dict, output_path: str) -> None:
    """Write rollup JSON file."""
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)


def write_weekly_rollup_csv(report: Dict, output_path: str) -> None:
    """Write swap rollup section as CSV."""
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "token_symbol",
                "token_address",
                "swaps",
                "total_usd",
                "success_count",
                "error_count",
                "dry_run_count",
            ],
        )
        writer.writeheader()
        for row in report.get("swap_rollup", []):
            writer.writerow(row)


def print_weekly_rollup(report: Dict) -> None:
    """Render weekly rollup in Rich tables."""
    phase_table = Table(title="Phase 6 Weekly Rollup: Phase/Status", header_style="bold cyan")
    phase_table.add_column("Phase")
    phase_table.add_column("Status")
    phase_table.add_column("Count", justify="right")
    for row in report.get("phase_status_counts", []):
        phase_table.add_row(row["phase"], row["status"], str(row["count"]))
    console.print(phase_table)

    swap_table = Table(title="Phase 6 Weekly Rollup: Swap Tokens", header_style="bold cyan")
    swap_table.add_column("Token")
    swap_table.add_column("Swaps", justify="right")
    swap_table.add_column("Total USD", justify="right")
    swap_table.add_column("Success", justify="right")
    swap_table.add_column("Errors", justify="right")
    swap_table.add_column("Dry-Run", justify="right")
    for row in report.get("swap_rollup", []):
        swap_table.add_row(
            row["token_symbol"],
            str(row["swaps"]),
            f"{row['total_usd']:.2f}",
            str(row["success_count"]),
            str(row["error_count"]),
            str(row["dry_run_count"]),
        )
    console.print(swap_table)


def execute_swap_intents(
    w3: Web3,
    signer: Account,
    swap_recipient: str,
    intents: List[Dict],
    broadcast: bool,
    continue_on_error: bool = True,
) -> List[Dict]:
    """Execute Phase 4 swaps with exact approvals and slippage retry ladder."""
    results: List[Dict] = []
    if not intents:
        logger.info("No swap intents generated for Phase 4")
        return results

    router_addr = to_checksum_address(HYDREX_ROUTER_ADDRESS)
    router_code = w3.eth.get_code(router_addr)
    if len(router_code) == 0:
        raise RuntimeError(
            f"Hydrex router address has no contract bytecode on chain {CHAIN_ID}: {router_addr}. "
            "Aborting swaps to avoid no-op transactions."
        )

    router = w3.eth.contract(address=router_addr, abi=ROUTER_ABI)
    usdc_addr = to_checksum_address(USDC_ADDRESS)
    usdc_code = w3.eth.get_code(usdc_addr)
    if len(usdc_code) == 0:
        raise RuntimeError(
            f"USDC address has no contract bytecode on chain {CHAIN_ID}: {usdc_addr}."
        )

    nonce = w3.eth.get_transaction_count(signer.address)
    gas_price = w3.eth.gas_price

    for intent in intents:
        token = intent["token"]
        symbol = intent["symbol"]
        amount_in = int(intent["balance_raw"])
        expected_usdc_out_raw = int(math.floor(intent["expected_usdc_out"] * 1_000_000))

        swap_result = {
            "token": token,
            "symbol": symbol,
            "amount_in": amount_in,
            "usd_value": intent["usd_value"],
            "status": "skipped",
            "attempts": 0,
        }

        for attempt in range(SWAP_RETRY_COUNT):
            slippage = SLIPPAGE_START_PCT + attempt
            min_out = int(expected_usdc_out_raw * (1 - slippage / 100.0))
            deadline = int(time.time()) + SWAP_DEADLINE_SECONDS

            swap_result["attempts"] = attempt + 1
            swap_result["slippage_pct"] = slippage
            swap_result["amount_out_minimum"] = max(min_out, 0)

            if not broadcast:
                logger.info(
                    f"DRY RUN swap {symbol}: amount_in={amount_in} "
                    f"min_out={swap_result['amount_out_minimum']} slippage={slippage:.2f}%"
                )
                swap_result["status"] = "dry_run"
                break

            token_addr = to_checksum_address(token)
            token_code = w3.eth.get_code(token_addr)
            if len(token_code) == 0:
                raise RuntimeError(
                    f"Token address has no contract bytecode on chain {CHAIN_ID}: {token_addr}"
                )

            token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
            router_allowance = token_contract.functions.allowance(
                signer.address,
                router_addr,
            ).call()
            needs_approve = router_allowance < amount_in

            transient_retries = 0
            attempt_finished = False
            while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
                try:
                    wait_for_pending_nonce_drain(
                        w3,
                        signer.address,
                        timeout_seconds=PENDING_NONCE_WAIT_SECONDS,
                        poll_seconds=PENDING_NONCE_POLL_SECONDS,
                    )
                    nonce = w3.eth.get_transaction_count(signer.address, "pending")
                    gas_price = w3.eth.gas_price

                    if needs_approve:
                        approve_tx = token_contract.functions.approve(
                            to_checksum_address(HYDREX_ROUTER_ADDRESS),
                            amount_in,
                        ).build_transaction(
                            {
                                "from": signer.address,
                                "chainId": CHAIN_ID,
                                "nonce": nonce,
                                "gas": 120000,
                                "gasPrice": gas_price,
                            }
                        )
                        _, approve_status = send_contract_transaction(w3, signer, approve_tx)
                        nonce += 1
                        if approve_status != 1:
                            raise RuntimeError("approve transaction reverted")
                        needs_approve = False

                    swap_deployer = to_checksum_address(HYDREX_SWAP_DEPLOYER_ADDRESS)
                    swap_params = (
                        to_checksum_address(token),
                        usdc_addr,
                        swap_deployer,
                        to_checksum_address(swap_recipient),
                        deadline,
                        amount_in,
                        swap_result["amount_out_minimum"],
                        0,
                    )
                    swap_tx = router.functions.exactInputSingle(swap_params).build_transaction(
                        {
                            "from": signer.address,
                            "chainId": CHAIN_ID,
                            "nonce": nonce,
                            "gas": 900000,
                            "gasPrice": gas_price,
                            "value": 0,
                        }
                    )
                    tx_hash, swap_status = send_contract_transaction(w3, signer, swap_tx)
                    nonce += 1

                    if swap_status == 1:
                        swap_receipt = w3.eth.get_transaction_receipt(tx_hash)
                        if len(swap_receipt.logs) == 0:
                            raise RuntimeError(
                                "swap transaction mined with zero logs (probable no-op); "
                                "router/address configuration likely invalid"
                            )
                        swap_result["status"] = "success"
                        swap_result["tx_hash"] = tx_hash
                        logger.info(f"Swap success {symbol} tx={tx_hash}")
                        attempt_finished = True
                        break

                    raise RuntimeError("swap transaction reverted")
                except Exception as e:
                    if is_delegated_inflight_error(e) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                        transient_retries += 1
                        logger.warning(
                            "Delegated in-flight limit for %s (retry %s/%s). Backing off %.1fs",
                            symbol,
                            transient_retries,
                            DELEGATED_INFLIGHT_MAX_RETRIES,
                            DELEGATED_INFLIGHT_RETRY_SECONDS,
                        )
                        time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                        continue

                    if is_nonce_too_low_error(e) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                        transient_retries += 1
                        logger.warning(
                            "Nonce sync race for %s (retry %s/%s). Re-syncing nonce after %.1fs",
                            symbol,
                            transient_retries,
                            DELEGATED_INFLIGHT_MAX_RETRIES,
                            PENDING_NONCE_POLL_SECONDS,
                        )
                        time.sleep(PENDING_NONCE_POLL_SECONDS)
                        continue

                    swap_result["error"] = str(e)
                    logger.warning(
                        f"Swap attempt {attempt + 1}/{SWAP_RETRY_COUNT} failed for {symbol}: {e}"
                    )
                    if attempt == SWAP_RETRY_COUNT - 1:
                        swap_result["status"] = "error"
                    if not continue_on_error:
                        results.append(swap_result)
                        raise
                    attempt_finished = True
                    break

            if swap_result.get("status") == "success":
                break
            if attempt_finished:
                continue

        results.append(swap_result)

    return results


def get_multi_quote(
    intents: List[Dict],
    taker: str,
    slippage_bps: int,
    source: str,
    origin: str,
) -> Dict:
    """
    Call POST /quote/multi on the Hydrex routing API.

    Returns the parsed JSON response dict.
    Raises RuntimeError on HTTP errors or missing transaction fields.

    Args:
        intents:      swap intent objects built by build_swap_intents()
        taker:        wallet that will send the executeSwaps tx (must hold the
                      approved tokens); USDC output lands here before the optional
                      forward step.
        slippage_bps: execution slippage in BPS (50 = 0.5 %)
        source:       comma-separated aggregator sources (e.g. "KYBERSWAP")
        origin:       origin label for routing attribution
    """
    swap_items = [
        {
            "fromTokenAddress": intent["token"],
            "toTokenAddress": USDC_ADDRESS,
            "amount": str(intent["balance_raw"]),
        }
        for intent in intents
    ]

    payload: Dict = {
        "taker": taker,
        "chainId": str(CHAIN_ID),
        "slippage": str(slippage_bps),
        "origin": origin,
        "swaps": swap_items,
    }
    if source:
        payload["source"] = source

    body = json.dumps(payload).encode("utf-8")
    url = f"{HYDREX_ROUTING_API_URL}/quote/multi"

    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Origin": "https://router.api.hydrex.fi",
            "Referer": "https://router.api.hydrex.fi/",
        },
        method="POST",
    )

    logger.info("Requesting multi-quote from routing API: %s (%d legs)", url, len(swap_items))
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Routing API HTTP {exc.code} for multi-quote: {body_text[:500]}"
        ) from exc

    if "transaction" not in data or "data" not in data.get("transaction", {}):
        raise RuntimeError(
            f"Routing API response missing transaction.data: {json.dumps(data)[:500]}"
        )

    return data


def execute_router_batch_swaps(
    w3: Web3,
    signer: Account,
    swap_recipient: str,
    intents: List[Dict],
    broadcast: bool,
) -> Dict:
    """
    Phase 4 (router-batch mode): approve tokens, get one multi-quote, send one
    executeSwaps tx to the Hydrex multi-router.

    Flow (matches boss's suggested approach):
      1. Build a single POST /quote/multi call for all eligible tokens.
         taker=swap_recipient so USDC is delivered directly to the cold wallet.
      2. Validate the returned router address against HYDREX_MULTI_ROUTER_ADDRESS.
      3. Approve each input token on the multi-router (skip if allowance sufficient).
      4. Send the single executeSwaps transaction.
      5. Verify USDC balance at swap_recipient increased (non-zero output check).

    Returns a result dict describing the outcome.
    """
    multi_router_addr = to_checksum_address(HYDREX_MULTI_ROUTER_ADDRESS)

    # Validate multi-router has code
    router_code = w3.eth.get_code(multi_router_addr)
    if len(router_code) == 0:
        raise RuntimeError(
            f"Hydrex multi-router has no bytecode on chain {CHAIN_ID}: {multi_router_addr}"
        )

    usdc_addr = to_checksum_address(USDC_ADDRESS)
    signer_addr = to_checksum_address(signer.address)
    recipient_addr = to_checksum_address(swap_recipient)

    recipient_is_signer = signer_addr.lower() == recipient_addr.lower()

    result: Dict = {
        "mode": "router-batch",
        "intents_count": len(intents),
        "status": "skipped",
    }

    if not intents:
        logger.info("Router-batch: no swap intents to execute; skipping routing API call")
        result.update(
            {
                "status": "skipped",
                "error": None,
                "legs": [],
                "approvals": [],
                "usdc_recipient": recipient_addr,
            }
        )
        return result

    if not broadcast:
        # Dry-run: still call the routing API to validate routes exist
        logger.info("DRY RUN router-batch: calling routing API to validate %d swap legs", len(intents))
        try:
            quote = get_multi_quote(
                intents,
                # taker must be the address that holds the tokens and sends the tx
                taker=signer_addr,
                slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
                source=HYDREX_ROUTING_SOURCE,
                origin=HYDREX_ROUTING_ORIGIN,
            )
            tx_to = quote["transaction"].get("to", "").lower()
            result.update(
                {
                    "status": "dry_run",
                    "routing_api_router": quote["transaction"].get("to"),
                    "legs": [
                        {
                            "from": s["fromTokenAddress"],
                            "to": s["toTokenAddress"],
                            "amountIn": s.get("amountIn"),
                            "amountOut": s.get("amountOut"),
                            "source": s.get("source"),
                        }
                        for s in quote.get("swaps", [])
                    ],
                    "total_usd": quote.get("totalAmountUsd"),
                }
            )
            logger.info(
                "DRY RUN multi-quote ok: %d legs totalUsd=%s routerTarget=%s",
                len(quote.get("swaps", [])),
                quote.get("totalAmountUsd"),
                quote["transaction"].get("to"),
            )
        except Exception as exc:
            result["status"] = "error"
            result["error"] = str(exc)
            logger.error("DRY RUN routing API call failed: %s", exc)
        return result

    # --- live broadcast path ---

    # USDC balance before (at signer; USDC lands here after executeSwaps)
    usdc_contract = w3.eth.contract(address=usdc_addr, abi=ERC20_ABI)
    usdc_before = usdc_contract.functions.balanceOf(signer_addr).call()

    # Build multi-quote (taker=signer: holds input tokens, approves router, sends tx)
    logger.info("Fetching multi-quote for %d swap legs via routing API…", len(intents))
    logger.info("taker (signer): %s  final USDC recipient: %s", signer_addr, recipient_addr)
    quote = get_multi_quote(
        intents,
        taker=signer_addr,
        slippage_bps=HYDREX_ROUTING_SLIPPAGE_BPS,
        source=HYDREX_ROUTING_SOURCE,
        origin=HYDREX_ROUTING_ORIGIN,
    )

    quoted_router = to_checksum_address(quote["transaction"]["to"])
    if quoted_router.lower() != multi_router_addr.lower():
        raise RuntimeError(
            f"Routing API returned unexpected router address: {quoted_router}. "
            f"Expected multi-router: {multi_router_addr}. Aborting for safety."
        )

    calldata_hex = quote["transaction"]["data"]

    # Step 1: Approve tokens on multi-router
    gas_price = w3.eth.gas_price
    nonce = w3.eth.get_transaction_count(signer_addr)

    approval_results = []
    for intent in intents:
        token_addr = to_checksum_address(intent["token"])
        amount_in = int(intent["balance_raw"])
        token_contract = w3.eth.contract(address=token_addr, abi=ERC20_ABI)
        current_allowance = token_contract.functions.allowance(signer_addr, multi_router_addr).call()
        if current_allowance >= amount_in:
            logger.info("Approve %s: already sufficient (%d)", intent["symbol"], current_allowance)
            approval_results.append({"token": token_addr, "symbol": intent["symbol"], "status": "already_approved"})
            continue

        logger.info("Approving %s (%s) on multi-router…", intent["symbol"], token_addr)
        approval_done = False
        transient_retries = 0
        while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
            try:
                wait_for_pending_nonce_drain(
                    w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
                )
                nonce = w3.eth.get_transaction_count(signer_addr, "pending")
                gas_price = w3.eth.gas_price
                approve_tx = token_contract.functions.approve(
                    multi_router_addr, amount_in
                ).build_transaction(
                    {
                        "from": signer_addr,
                        "chainId": CHAIN_ID,
                        "nonce": nonce,
                        "gas": 120000,
                        "gasPrice": gas_price,
                    }
                )
                approve_hash, approve_status = send_contract_transaction(w3, signer, approve_tx)
                nonce += 1
                if approve_status != 1:
                    raise RuntimeError(f"approve reverted for {intent['symbol']}")
                approval_results.append(
                    {"token": token_addr, "symbol": intent["symbol"], "status": "approved", "tx_hash": approve_hash}
                )
                logger.info("Approved %s tx=%s", intent["symbol"], approve_hash)
                approval_done = True
                break
            except Exception as exc:
                if is_delegated_inflight_error(exc) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                    transient_retries += 1
                    logger.warning(
                        "Delegated in-flight limit during approve for %s (retry %s/%s). Backing off %.1fs",
                        intent["symbol"],
                        transient_retries,
                        DELEGATED_INFLIGHT_MAX_RETRIES,
                        DELEGATED_INFLIGHT_RETRY_SECONDS,
                    )
                    time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                    continue
                if is_nonce_too_low_error(exc) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                    transient_retries += 1
                    logger.warning(
                        "Nonce sync race during approve for %s (retry %s/%s). Re-syncing nonce after %.1fs",
                        intent["symbol"],
                        transient_retries,
                        DELEGATED_INFLIGHT_MAX_RETRIES,
                        PENDING_NONCE_POLL_SECONDS,
                    )
                    time.sleep(PENDING_NONCE_POLL_SECONDS)
                    continue

                approval_results.append(
                    {"token": token_addr, "symbol": intent["symbol"], "status": "error", "error": str(exc)}
                )
                logger.error("Approve failed for %s: %s", intent["symbol"], exc)
                result["status"] = "error"
                result["error"] = f"approve failed for {intent['symbol']}: {exc}"
                result["approvals"] = approval_results
                return result

        if not approval_done:
            err = (
                f"approve failed for {intent['symbol']}: "
                "delegated-account retries exhausted"
            )
            approval_results.append(
                {"token": token_addr, "symbol": intent["symbol"], "status": "error", "error": err}
            )
            logger.error(err)
            result["status"] = "error"
            result["error"] = err
            result["approvals"] = approval_results
            return result

    # Step 2: Send the single executeSwaps transaction
    logger.info("Sending executeSwaps transaction to multi-router %s…", multi_router_addr)
    wait_for_pending_nonce_drain(
        w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
    )
    nonce = w3.eth.get_transaction_count(signer_addr, "pending")
    gas_price = w3.eth.gas_price

    swap_tx = {
        "from": signer_addr,
        "to": multi_router_addr,
        "data": calldata_hex,
        "chainId": CHAIN_ID,
        "nonce": nonce,
        "gas": 2_000_000,
        "gasPrice": gas_price,
        "value": 0,
    }

    receipt = None
    swap_tx_hash = None
    transient_retries = 0
    while transient_retries <= DELEGATED_INFLIGHT_MAX_RETRIES:
        try:
            wait_for_pending_nonce_drain(
                w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
            )
            nonce = w3.eth.get_transaction_count(signer_addr, "pending")
            gas_price = w3.eth.gas_price
            swap_tx.update({"nonce": nonce, "gasPrice": gas_price})

            signed = w3.eth.account.sign_transaction(swap_tx, signer.key)
            raw = signed_tx_raw_bytes(signed)
            swap_tx_hash_bytes = w3.eth.send_raw_transaction(raw)
            swap_tx_hash = swap_tx_hash_bytes.hex()
            nonce += 1
            receipt = wait_for_receipt(w3, swap_tx_hash_bytes)
            logger.info("executeSwaps tx mined: status=%d tx=%s", receipt.status, swap_tx_hash)
            break
        except Exception as exc:
            if is_delegated_inflight_error(exc) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                transient_retries += 1
                logger.warning(
                    "Delegated in-flight limit for executeSwaps (retry %s/%s). Backing off %.1fs",
                    transient_retries,
                    DELEGATED_INFLIGHT_MAX_RETRIES,
                    DELEGATED_INFLIGHT_RETRY_SECONDS,
                )
                time.sleep(DELEGATED_INFLIGHT_RETRY_SECONDS)
                continue
            if is_nonce_too_low_error(exc) and transient_retries < DELEGATED_INFLIGHT_MAX_RETRIES:
                transient_retries += 1
                logger.warning(
                    "Nonce sync race for executeSwaps (retry %s/%s). Re-syncing nonce after %.1fs",
                    transient_retries,
                    DELEGATED_INFLIGHT_MAX_RETRIES,
                    PENDING_NONCE_POLL_SECONDS,
                )
                time.sleep(PENDING_NONCE_POLL_SECONDS)
                continue

            result["status"] = "error"
            result["error"] = f"executeSwaps tx failed: {exc}"
            result["approvals"] = approval_results
            return result

    if receipt is None or swap_tx_hash is None:
        result["status"] = "error"
        result["error"] = "executeSwaps tx failed: delegated-account retries exhausted"
        result["approvals"] = approval_results
        return result

    if receipt.status != 1:
        result["status"] = "error"
        result["error"] = "executeSwaps tx reverted"
        result["tx_hash"] = swap_tx_hash
        result["approvals"] = approval_results
        return result

    if len(receipt.logs) == 0:
        result["status"] = "error"
        result["error"] = "executeSwaps mined with zero logs (probable no-op)"
        result["tx_hash"] = swap_tx_hash
        result["approvals"] = approval_results
        return result

    # Step 3: check USDC received at signer
    usdc_after = usdc_contract.functions.balanceOf(signer_addr).call()
    usdc_delta = usdc_after - usdc_before
    logger.info(
        "USDC balance delta at signer: %d raw (%s USDC)",
        usdc_delta,
        f"{usdc_delta / 1_000_000:.6f}",
    )

    result.update(
        {
            "status": "success",
            "tx_hash": swap_tx_hash,
            "usdc_recipient": str(recipient_addr),
            "usdc_received_raw": usdc_delta,
            "usdc_received": usdc_delta / 1_000_000,
            "approvals": approval_results,
            "legs": [
                {
                    "from": s["fromTokenAddress"],
                    "to": s["toTokenAddress"],
                    "amountIn": s.get("amountIn"),
                    "amountOut": s.get("amountOut"),
                    "source": s.get("source"),
                }
                for s in quote.get("swaps", [])
            ],
        }
    )

    # Step 4: forward USDC to recipient if it differs from signer
    if not recipient_is_signer and usdc_delta > 0:
        logger.info(
            "Forwarding %s USDC to recipient %s…",
            f"{usdc_delta / 1_000_000:.6f}",
            recipient_addr,
        )
        wait_for_pending_nonce_drain(
            w3, signer_addr, PENDING_NONCE_WAIT_SECONDS, PENDING_NONCE_POLL_SECONDS
        )
        nonce = w3.eth.get_transaction_count(signer_addr, "pending")
        gas_price = w3.eth.gas_price
        forward_tx = usdc_contract.functions.transfer(
            recipient_addr, usdc_delta
        ).build_transaction(
            {
                "from": signer_addr,
                "chainId": CHAIN_ID,
                "nonce": nonce,
                "gas": 120_000,
                "gasPrice": gas_price,
            }
        )
        try:
            fwd_hash, fwd_status = send_contract_transaction(w3, signer, forward_tx)
            if fwd_status != 1:
                raise RuntimeError("USDC forward transfer reverted")
            result["forward_tx_hash"] = fwd_hash
            logger.info("USDC forward tx=%s", fwd_hash)
        except Exception as exc:
            result["forward_error"] = str(exc)
            logger.error("USDC forward failed (swaps succeeded): %s", exc)
    elif recipient_is_signer:
        logger.info("Recipient is signer — no forward needed")

    return result


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
        raise ValueError("Could not resolve manual claim targets: " + "; ".join(message_parts))

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
            decimals = decimals if decimals is not None else int(token_contract.functions.decimals().call())
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
        "swap_results": swap_results or [],
    }
    
    with open(output_file, "w") as f:
        json.dump(artifact, f, indent=2, sort_keys=True)
    
    logger.info(f"Exported claim artifact to: {output_file}")


# ═══ Main Orchestration ═══
def main():
    """
    Phase 1-4 Orchestration:
    
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
        description="Claim and Swap Rewards: Phase 1-6 (Discovery, Claim, Swap, Persistence, Reporting)"
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
        help="Dry-run mode (default: true). No transactions are broadcast unless --broadcast is set.",
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
        "--claim-source",
        type=str,
        default="escrow",
        choices=["escrow", "voter", "distributor"],
        help="Claim source contract for Phase 3",
    )

    parser.add_argument(
        "--escrow-address",
        type=str,
        default=ESCROW_ADDRESS,
        help="Escrow contract address for claimRewards(...) (defaults to ESCROW_ADDRESS/MY_ESCROW_ADDRESS env)",
    )

    parser.add_argument(
        "--rewards-distributor-address",
        type=str,
        default=HYDREX_REWARDS_DISTRIBUTOR_ADDRESS,
        help="HydrexRewardsDistributor address (required when --claim-source distributor)",
    )

    parser.add_argument(
        "--distributor-token-id",
        type=int,
        default=None,
        help="ve tokenId to use with distributor claim(tokenId)",
    )

    parser.add_argument(
        "--skip-claims",
        action="store_true",
        help="Skip Phase 3 claim execution and proceed to Phase 4 swap planning/execution",
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
        "--gauge-addresses",
        type=str,
        default="",
        help="Optional comma/newline/space separated gauge allowlist for targeted claims",
    )

    parser.add_argument(
        "--pool-addresses",
        type=str,
        default="",
        help="Optional comma/newline/space separated pool-address allowlist for targeted claims",
    )

    parser.add_argument(
        "--enable-swaps",
        action="store_true",
        help="Enable Phase 4 swaps for tokens above dust threshold",
    )

    parser.add_argument(
        "--swap-recipient",
        type=str,
        default=None,
        help="Recipient for USDC output swaps (defaults to signer address)",
    )

    parser.add_argument(
        "--swap-mode",
        type=str,
        default=None,
        choices=["direct", "router-batch"],
        help=(
            "Swap execution mode override: "
            "'direct' = per-token exactInputSingle (legacy default); "
            "'router-batch' = multi-quote + single executeSwaps tx via Hydrex routing API. "
            "Defaults to HYDREX_SWAP_EXECUTION_MODE env var (default: direct)."
        ),
    )

    parser.add_argument(
        "--swap-max-intents",
        type=int,
        default=0,
        help=(
            "Limit Phase 4 swaps to top-N intents by USD value (0 = no limit). "
            "Useful for focused router-batch troubleshooting runs."
        ),
    )

    parser.add_argument(
        "--write-run-log",
        action="store_true",
        help="Persist Phase 3/4 run rows into claim_swap_execution_log table",
    )

    parser.add_argument(
        "--weekly-report",
        action="store_true",
        help="Generate Phase 6 weekly rollup report from claim_swap_execution_log",
    )

    parser.add_argument(
        "--report-only",
        action="store_true",
        help="Run Phase 6 report generation only (skip wallet/RPC/claim/swap steps)",
    )

    parser.add_argument(
        "--report-lookback-days",
        type=int,
        default=7,
        help="Lookback window for weekly report aggregation",
    )

    parser.add_argument(
        "--report-json-output",
        type=str,
        default="weekly_claim_swap_report.json",
        help="Phase 6 JSON rollup output path",
    )

    parser.add_argument(
        "--report-csv-output",
        type=str,
        default="weekly_claim_swap_report_swaps.csv",
        help="Phase 6 CSV swap rollup output path",
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
    
    logger.info("═══ Claim and Swap Rewards: Phase 1-6 ═══")
    dry_run = (not args.broadcast) or parse_bool(args.dry_run)
    if args.broadcast and parse_bool(args.dry_run):
        dry_run = False

    logger.info(f"Dry-run: {dry_run}")
    logger.info(f"Broadcast enabled: {args.broadcast}")

    if args.report_only:
        logger.info("Phase 6 report-only mode enabled")
        conn = sqlite3.connect(DATABASE_PATH)
        report = generate_weekly_rollup(conn, max(1, args.report_lookback_days))
        print_weekly_rollup(report)
        write_weekly_rollup_json(report, args.report_json_output)
        write_weekly_rollup_csv(report, args.report_csv_output)
        logger.info(
            f"Phase 6 report outputs written: {args.report_json_output}, {args.report_csv_output}"
        )
        conn.close()
        return
    
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
        
        # Phase 2: Gauge discovery / manual override
        if args.gauge_addresses or args.pool_addresses:
            logger.info("Phase 2: Resolving manual claim target override...")
            gauges = resolve_manual_claim_gauges(
                conn,
                gauge_addresses=args.gauge_addresses,
                pool_addresses=args.pool_addresses,
            )
        else:
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

        claim_results: List[Dict] = []
        if args.skip_claims:
            logger.info("Phase 3 skipped (--skip-claims enabled)")
        else:
            logger.info(f"Phase 3: Preparing claims via source={args.claim_source}")

            token_by_bribe = invert_reward_tokens_to_bribes(reward_tokens)
            fee_bribes: Dict[str, List[str]] = {}
            external_bribes: Dict[str, List[str]] = {}
            for _, (internal_bribe, external_bribe) in gauge_to_bribes.items():
                if internal_bribe and internal_bribe in token_by_bribe:
                    fee_bribes[internal_bribe] = token_by_bribe[internal_bribe]
                if external_bribe and external_bribe in token_by_bribe:
                    external_bribes[external_bribe] = token_by_bribe[external_bribe]

            if args.claim_source == "escrow":
                escrow_address = args.escrow_address
                if not escrow_address:
                    raise ValueError(
                        "--escrow-address is required when --claim-source escrow (or set ESCROW_ADDRESS/MY_ESCROW_ADDRESS in .env)"
                    )

                if not fee_bribes and not external_bribes:
                    logger.info("No fee/bribe addresses discovered for escrow claimRewards")
                    claim_results = []
                else:
                    escrow_contract = w3.eth.contract(
                        address=to_checksum_address(escrow_address),
                        abi=ESCROW_ABI,
                    )
                    claim_results = execute_escrow_claim_rewards(
                        w3=w3,
                        escrow_contract=escrow_contract,
                        signer=signer,
                        fee_bribes=fee_bribes,
                        external_bribes=external_bribes,
                        claim_mode=args.claim_mode,
                        broadcast=args.broadcast and not dry_run,
                    )

            elif args.claim_source == "distributor":
                if not args.rewards_distributor_address:
                    raise ValueError(
                        "--rewards-distributor-address is required when --claim-source distributor"
                    )
                if args.distributor_token_id is None:
                    raise ValueError(
                        "--distributor-token-id is required when --claim-source distributor"
                    )

                distributor_contract = w3.eth.contract(
                    address=to_checksum_address(args.rewards_distributor_address),
                    abi=DISTRIBUTOR_ABI,
                )
                # Distributor claims may return a token not present in bribe-derived token cache.
                try:
                    distributor_token_address = to_checksum_address(
                        distributor_contract.functions.token().call()
                    )
                    ensure_reward_token_metadata(
                        w3=w3,
                        conn=conn,
                        reward_tokens=reward_tokens,
                        token_address=distributor_token_address,
                    )
                except Exception as e:
                    logger.warning(
                        f"Could not resolve distributor token() metadata for Phase 4 swaps: {e}"
                    )

                preflight_distributor_claim_authorization(
                    distributor_contract=distributor_contract,
                    signer=signer,
                    token_id=args.distributor_token_id,
                )
                claim_results = execute_distributor_claim(
                    w3=w3,
                    distributor_contract=distributor_contract,
                    signer=signer,
                    distributor_token_id=args.distributor_token_id,
                    broadcast=args.broadcast and not dry_run,
                )
            else:
                claim_recipient = (
                    to_checksum_address(args.claim_recipient) if args.claim_recipient else signer_address
                )
                claim_for = to_checksum_address(args.claim_for) if args.claim_for else signer_address

                voter_contract = w3.eth.contract(address=to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)
                preflight_claim_authorization(
                    voter_contract=voter_contract,
                    signer=signer,
                    claim_for=claim_for,
                    recipient=claim_recipient,
                    fee_bribes=fee_bribes,
                    external_bribes=external_bribes,
                    claim_mode=args.claim_mode,
                )
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

        # Phase 4: Build and execute swaps
        swap_results: List[Dict] = []
        if args.enable_swaps:
            logger.info("Phase 4: Building swap intents...")
            swap_intents = build_swap_intents(
                w3=w3,
                conn=conn,
                signer_address=signer_address,
                reward_tokens=reward_tokens,
            )
            if args.swap_max_intents and args.swap_max_intents > 0:
                original_count = len(swap_intents)
                swap_intents = swap_intents[: args.swap_max_intents]
                logger.info(
                    "Phase 4: Limiting swap intents to top %d by USD value "
                    "(from %d total)",
                    len(swap_intents),
                    original_count,
                )
            logger.info(f"Phase 4: Generated {len(swap_intents)} swap intents")

            swap_recipient = (
                to_checksum_address(args.swap_recipient)
                if args.swap_recipient
                else signer_address
            )

            # Determine execution mode: CLI flag overrides env/config default
            swap_mode = (args.swap_mode or HYDREX_SWAP_EXECUTION_MODE).strip().lower()
            logger.info("Phase 4 swap execution mode: %s", swap_mode)

            if swap_mode == "router-batch":
                logger.info("Phase 4: Using router-batch mode (POST /quote/multi + single executeSwaps tx)")
                batch_result = execute_router_batch_swaps(
                    w3=w3,
                    signer=signer,
                    swap_recipient=swap_recipient,
                    intents=swap_intents,
                    broadcast=args.broadcast and not dry_run,
                )
                # Normalise into the same list shape used by the rest of the pipeline
                status = batch_result.get("status", "error")
                swap_results = [
                    {
                        "mode": "router-batch",
                        "symbol": "BATCH",
                        "token": "batch",
                        "status": status,
                        "tx_hash": batch_result.get("tx_hash"),
                        "usdc_recipient": batch_result.get("usdc_recipient"),
                        "usdc_received": batch_result.get("usdc_received"),
                        "usdc_received_raw": batch_result.get("usdc_received_raw"),
                        "error": batch_result.get("error"),
                        "legs": batch_result.get("legs", []),
                        "approvals": batch_result.get("approvals", []),
                        "intents_count": batch_result.get("intents_count"),
                    }
                ]
                # Rich summary table for batch mode
                batch_table = Table(title="Phase 4 Batch Swap Summary", header_style="bold cyan")
                batch_table.add_column("Field")
                batch_table.add_column("Value")
                batch_table.add_row("Mode", "router-batch")
                batch_table.add_row("Status", status)
                batch_table.add_row("Legs", str(len(batch_result.get("legs", []))))
                batch_table.add_row("USDC Received", f"{batch_result.get('usdc_received', 0):.6f}" if batch_result.get("usdc_received") else "-")
                batch_table.add_row("USDC Recipient", batch_result.get("usdc_recipient") or swap_recipient)
                batch_table.add_row("executeSwaps Tx", batch_result.get("tx_hash") or "-")
                if batch_result.get("error"):
                    batch_table.add_row("[red]Error[/red]", batch_result["error"])
                console.print(batch_table)
            else:
                swap_results = execute_swap_intents(
                    w3=w3,
                    signer=signer,
                    swap_recipient=swap_recipient,
                    intents=swap_intents,
                    broadcast=args.broadcast and not dry_run,
                    continue_on_error=True,
                )
                build_swap_execution_summary_table(swap_results)
        else:
            logger.info("Phase 4 swaps disabled (enable with --enable-swaps)")

        # Phase 5: Persistence for weekly review
        run_ts = int(time.time())
        if args.write_run_log:
            persist_phase_results(
                conn=conn,
                run_ts=run_ts,
                epoch=target_epoch,
                claim_results=claim_results,
                swap_results=swap_results,
            )
            logger.info("Phase 5: Persisted run rows to claim_swap_execution_log")
        else:
            logger.info("Phase 5 persistence disabled (enable with --write-run-log)")

        if args.weekly_report:
            report = generate_weekly_rollup(conn, max(1, args.report_lookback_days))
            print_weekly_rollup(report)
            write_weekly_rollup_json(report, args.report_json_output)
            write_weekly_rollup_csv(report, args.report_csv_output)
            logger.info(
                f"Phase 6 report outputs written: {args.report_json_output}, {args.report_csv_output}"
            )
        
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
            swap_results=swap_results,
        )
        
        # Final summary
        summary_text = f"""
    Phase 1-6 Complete: Discovery + Claim + Swap + Persistence + Reporting

Epoch: {target_epoch}
Signer: {signer_address}
Gauges: {len(gauges)}
Reward Tokens: {len(reward_tokens)}
Bribe Contracts: {len(all_bribes)}
Claim Batches: {len(claim_results)}
Swap Results: {len(swap_results)}

Next Phase: Phase 7+ (operational polish)
    - Expand runbook recovery commands
    - Add focused integration tests

Artifact: {args.output}
"""
        
        console.print(
            Panel(summary_text.strip(), title="✓ Phase 1-6 Complete", style="green")
        )
        
        logger.info("Phase 1-6 completed successfully")
        conn.close()
    
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        sys.exit(1)
    
    except Exception as e:
        logger.error(f"Error in Phase 1-6 flow: {e}", exc_info=True)
        console.print(
            Panel(
                f"[red]Error:[/red] {e}",
                title="Phase 1-6 Failed",
                style="red",
            )
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
