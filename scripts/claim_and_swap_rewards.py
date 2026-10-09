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
import json
import logging
import os
import sqlite3
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import VOTER_ABI
from src.claims.wallet import (
    CHAIN_ID,
    ONE_E18,
    load_wallet,
    load_wallet_from_1password,
    load_wallet_from_file_or_env,
    preflight_checks,
)
from src.claims.reclaim_guard import (
    CLAIM_SOURCE_AUTO,
    CLAIM_SOURCES,
    ERC20_TRANSFER_TOPIC,
    RECLAIM_GUARD_BLOCK,
    RECLAIM_GUARD_FORCED,
    RECLAIM_GUARD_NOT_APPLICABLE,
    RECLAIM_GUARD_PROCEED,
    ClaimMovedNothingError,
    assert_claims_moved_tokens,
    choose_claim_source,
    count_reward_transfers,
    decode_voter_revert,
    evaluate_reclaim_guard,
    is_delegated_inflight_error,
    is_nonce_too_low_error,
    resolve_claim_source,
    signed_tx_raw_bytes,
    summarize_claim_transfers,
    wait_for_pending_nonce_drain,
    wait_for_receipt,
)
from src.claims.reporting import (
    generate_weekly_rollup,
    persist_phase_results,
    print_weekly_rollup,
    write_weekly_rollup_csv,
    write_weekly_rollup_json,
)
from src.claims.swaps import (
    ERC20_ABI,
    build_swap_execution_summary_table,
    build_swap_intents,
    execute_router_batch_swaps,
    execute_swap_intents,
)
from src.claims.discovery import (
    build_claim_summary,
    discover_voted_gauges,
    enumerate_reward_tokens_from_bribes,
    ensure_reward_token_metadata,
    export_claim_artifact,
    map_gauges_to_bribes,
    resolve_manual_claim_gauges,
    resolve_target_epoch,
)
from config.settings import (
    DATABASE_PATH,
    DUST_THRESHOLD_USD,
    ESCROW_ADDRESS,
    HYDREX_FACTORY_ADDRESS,
    HYDREX_REWARDS_DISTRIBUTOR_ADDRESS,
    HYDREX_ROUTER_ADDRESS,
    HYDREX_SWAP_EXECUTION_MODE,
    RPC_URL,
    SLIPPAGE_START_PCT,
    SWAP_DEADLINE_SECONDS,
    SWAP_RETRY_COUNT,
    USDC_ADDRESS,
    VE_ADDRESS,
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


def parse_bool(value: str) -> bool:
    """Parse common truthy/falsey CLI values."""
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


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
            {
                "internalType": "address[]",
                "name": "bribeAddresses",
                "type": "address[]",
            },
            {"internalType": "address[]", "name": "claimTokens", "type": "address[]"},
        ],
        "name": "claimRewards",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]


VOTER_SELF_CLAIM_SIGNATURES = {
    "fees": "claimFees(address[],address[][])",
    "bribes": "claimBribes(address[],address[][])",
}
VOTER_RECIPIENT_CLAIM_SIGNATURES = {
    "fees": "claimFeesToRecipientByAddress(address[],address[][],address,address)",
    "bribes": "claimBribesToRecipientByAddress(address[],address[][],address,address)",
}


def voter_claim_call_spec(signer_address: str, claim_for: str, recipient: str):
    """Pick the Voter claim functions for a claim context.

    Returns ({"fees": signature, "bribes": signature}, build_args(bribes, tokens) -> tuple).

    When the signer claims its own rewards to itself, use claimFees/claimBribes
    (address[],address[][]): the Voter calls Bribe.getRewardForAddress(msg.sender, tokens)
    (traced 2026-09-15). The *ToRecipientByAddress variants revert NotApprovedOrOwner()
    for a plain wallet even when claiming for itself, so they are kept only for claiming
    on behalf of another address or to a different recipient.
    """
    is_self_claim = (
        signer_address.lower() == str(claim_for).lower() == str(recipient).lower()
    )
    if is_self_claim:
        return VOTER_SELF_CLAIM_SIGNATURES, lambda bribes, tokens: (bribes, tokens)
    return VOTER_RECIPIENT_CLAIM_SIGNATURES, lambda bribes, tokens: (
        bribes,
        tokens,
        claim_for,
        recipient,
    )


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
    signatures, build_args = voter_claim_call_spec(signer.address, claim_for, recipient)
    checks: List[Tuple[str, str, Dict[str, List[str]]]] = []
    if claim_mode in {"all", "fees"} and fee_bribes:
        checks.append(("fees", signatures["fees"], fee_bribes))
    if claim_mode in {"all", "bribes"} and external_bribes:
        checks.append(("bribes", signatures["bribes"], external_bribes))

    for action_type, signature, mapping in checks:
        bribe, tokens = next(((b, t) for b, t in mapping.items() if t), (None, None))
        if not bribe or not tokens:
            continue

        try:
            fn = voter_contract.get_function_by_signature(signature)
            fn(*build_args([bribe], [tokens])).call({"from": signer.address})
            logger.info(
                f"Phase 3 preflight authorization passed for {action_type} claims"
            )
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
        logger.info(
            f"Phase 3 distributor preflight: tokenId={token_id} claimable={claimable_amt}"
        )
    except Exception as e:
        logger.warning(f"Could not read claimable({token_id}) on distributor: {e}")

    try:
        distributor_contract.functions.claim(int(token_id)).estimate_gas(
            {"from": signer.address}
        )
        logger.info("Phase 3 distributor authorization preflight passed")
    except Exception as e:
        raise PermissionError(
            "Distributor claim authorization failed. "
            f"signer={signer.address} token_id={token_id} error={e}"
        ) from e


def invert_reward_tokens_to_bribes(
    reward_tokens: Dict[str, Dict]
) -> Dict[str, List[str]]:
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
    bribe_chunks = (
        chunk_claim_inputs(external_bribes, claim_batch_size) if external_bribes else []
    )

    signatures, build_args = voter_claim_call_spec(signer.address, claim_for, recipient)
    actions: List[Tuple[str, List[Tuple[List[str], List[List[str]]]], str]] = []
    if claim_mode in {"all", "fees"}:
        actions.append(("fees", fee_chunks, signatures["fees"]))
    if claim_mode in {"all", "bribes"}:
        actions.append(("bribes", bribe_chunks, signatures["bribes"]))

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
            call = fn(*build_args(bribes, tokens))
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
                result.update(
                    {
                        "status": "dry_run",
                        "nonce": nonce,
                        "gas": tx["gas"],
                        "gas_price_wei": gas_price,
                    }
                )
                results.append(result)
                nonce += 1
                continue

            try:
                signed = w3.eth.account.sign_transaction(tx, signer.key)
                tx_hash = w3.eth.send_raw_transaction(signed_tx_raw_bytes(signed))
                receipt = wait_for_receipt(w3, tx_hash)

                transfers_in = count_reward_transfers(receipt, recipient)
                result.update(
                    {
                        "status": "success" if receipt.status == 1 else "reverted",
                        "nonce": nonce,
                        "gas": tx["gas"],
                        "gas_price_wei": gas_price,
                        "tx_hash": tx_hash.hex(),
                        "block_number": receipt.blockNumber,
                        "transfers_in": transfers_in,
                    }
                )
                logger.info(
                    f"Broadcast {action_type} batch {batch_index}/{len(chunks)} "
                    f"status={result['status']} transfers_in={transfers_in} tx={tx_hash.hex()}"
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
                logger.error(
                    f"Claim tx failed for {action_type} batch {batch_index}: {e}"
                )

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
                "transfers_in": count_reward_transfers(receipt, signer.address),
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
        call = escrow_contract.functions.claimRewards(
            fee_addresses, bribe_addresses, claim_tokens
        )

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

        # Retry loop: handles "in-flight transaction limit" and "gapped-nonce tx".
        # Base runs reth, whose max_inflight_delegated_slot_limit defaults to 1, so an
        # EIP-7702 delegated account may hold only one pooled tx at a time.  This is a
        # node policy, not an RPC-provider quota.  We already wait for a receipt before
        # sending the next tx, but Base Flashblocks return that receipt in ~200ms while
        # the pool frees the delegated slot only when the full 2s block seals -- so the
        # next send can still collide.  One sealed block is therefore all the backoff
        # that is needed; the tail entry covers genuine congestion.  Back off and
        # re-sync nonce on each rate-limit rejection so subsequent batches don't gap.
        _RATE_LIMIT_SIGNALS = ("in-flight transaction limit", "gapped-nonce tx")
        _backoffs = [0, 3, 6, 20]  # seconds to wait before each attempt
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
                        "transfers_in": count_reward_transfers(receipt, signer.address),
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
            result.update(
                {
                    "status": "error",
                    "error": "delegated-account rate limit: all retries exhausted",
                }
            )
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
        default="runs/claim_and_swap_artifact.json",
        help=(
            "Output artifact file (JSON). The default sits in the git-ignored runs/ "
            "folder and is overwritten each run; pass a path to keep a copy"
        ),
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
        default=CLAIM_SOURCE_AUTO,
        choices=[CLAIM_SOURCE_AUTO, *CLAIM_SOURCES],
        help=(
            "Claim source contract for Phase 3. Default 'auto' resolves from veNFT "
            "ownership on-chain: escrow-owned -> escrow, signer-owned or approved -> voter. "
            "VOTE_FROM does not decide this — delegation does not move reward accrual."
        ),
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

    parser.add_argument(
        "--force",
        action="store_true",
        help="Skip the already-claimed guard and allow re-running Phase 3 for an epoch that has prior success rows",
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

        # F2: Pre-claim epoch guard — abort if Phase 3 claims already completed
        # for this epoch to prevent accidental double-claims.
        _prior_claims = conn.execute(
            """
            SELECT COUNT(*), MAX(run_ts)
            FROM claim_swap_execution_log
            WHERE epoch = ? AND phase = 'phase3_claim' AND status = 'success'
            """,
            (target_epoch,),
        ).fetchone()
        _prior_count = int(_prior_claims[0] or 0)
        _guard_decision, _guard_reason = evaluate_reclaim_guard(
            prior_success_count=_prior_count,
            skip_claims=args.skip_claims,
            force=args.force,
        )
        if _guard_decision == RECLAIM_GUARD_NOT_APPLICABLE:
            logger.info("Epoch %s: %s", target_epoch, _guard_reason)
        elif _guard_decision == RECLAIM_GUARD_FORCED:
            logger.warning("Epoch %s: %s", target_epoch, _guard_reason)
        elif _guard_decision == RECLAIM_GUARD_BLOCK:
            _prior_ts = _prior_claims[1]
            import datetime as _dt

            _prior_dt = _dt.datetime.utcfromtimestamp(_prior_ts).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            console.print(
                f"[bold red]✗ Epoch {target_epoch} already has {_prior_count} Phase 3 claim success "
                f"rows (last run {_prior_dt}). Aborting to prevent double-claim.[/bold red]\n"
                "  Pass [bold]--force[/bold] to override this guard."
            )
            conn.close()
            return

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
            logger.info(
                "Reward token cache refresh requested: forcing on-chain enumeration"
            )
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
            explicit_claim_source = (
                None if args.claim_source == CLAIM_SOURCE_AUTO else args.claim_source
            )
            claim_source, claim_source_reason = resolve_claim_source(
                w3=w3,
                signer_address=signer_address,
                explicit=explicit_claim_source,
                escrow_address=args.escrow_address,
                ve_address=VE_ADDRESS,
            )
            logger.info(
                f"Phase 3: Preparing claims via source={claim_source} ({claim_source_reason})"
            )

            token_by_bribe = invert_reward_tokens_to_bribes(reward_tokens)
            fee_bribes: Dict[str, List[str]] = {}
            external_bribes: Dict[str, List[str]] = {}
            for _, (internal_bribe, external_bribe) in gauge_to_bribes.items():
                if internal_bribe and internal_bribe in token_by_bribe:
                    fee_bribes[internal_bribe] = token_by_bribe[internal_bribe]
                if external_bribe and external_bribe in token_by_bribe:
                    external_bribes[external_bribe] = token_by_bribe[external_bribe]

            if claim_source == "escrow":
                escrow_address = args.escrow_address
                if not escrow_address:
                    raise ValueError(
                        "--escrow-address is required when --claim-source escrow (or set ESCROW_ADDRESS/MY_ESCROW_ADDRESS in .env)"
                    )

                if not fee_bribes and not external_bribes:
                    logger.info(
                        "No fee/bribe addresses discovered for escrow claimRewards"
                    )
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

            elif claim_source == "distributor":
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
                    to_checksum_address(args.claim_recipient)
                    if args.claim_recipient
                    else signer_address
                )
                claim_for = (
                    to_checksum_address(args.claim_for)
                    if args.claim_for
                    else signer_address
                )

                voter_contract = w3.eth.contract(
                    address=to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI
                )
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

            # A claim that transfers nothing still returns status=1. Without this check
            # a wrong --claim-source is indistinguishable from a working claim.
            try:
                # Name the address the transfers were actually counted against: the
                # Voter path pays --claim-recipient, the escrow/distributor paths pay
                # the signer.
                counted_recipient = (
                    to_checksum_address(args.claim_recipient)
                    if claim_source == "voter" and args.claim_recipient
                    else signer_address
                )
                assert_claims_moved_tokens(
                    claim_results, counted_recipient, claim_source
                )
            except ClaimMovedNothingError as e:
                if args.force:
                    logger.warning("--force passed: %s", e)
                else:
                    console.print(f"[bold red]✗ {e}[/bold red]")
                    conn.close()
                    raise

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
                logger.info(
                    "Phase 4: Using router-batch mode (POST /quote/multi + single executeSwaps tx)"
                )
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
                batch_table = Table(
                    title="Phase 4 Batch Swap Summary", header_style="bold cyan"
                )
                batch_table.add_column("Field")
                batch_table.add_column("Value")
                batch_table.add_row("Mode", "router-batch")
                batch_table.add_row("Status", status)
                batch_table.add_row("Legs", str(len(batch_result.get("legs", []))))
                batch_table.add_row(
                    "USDC Received",
                    (
                        f"{batch_result.get('usdc_received', 0):.6f}"
                        if batch_result.get("usdc_received")
                        else "-"
                    ),
                )
                batch_table.add_row(
                    "USDC Recipient",
                    batch_result.get("usdc_recipient") or swap_recipient,
                )
                batch_table.add_row(
                    "executeSwaps Tx", batch_result.get("tx_hash") or "-"
                )
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
