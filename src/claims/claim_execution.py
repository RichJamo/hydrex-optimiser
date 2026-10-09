"""Phase 3 of claim_and_swap_rewards.py: building and sending claim transactions."""

import logging
import time
from typing import Dict, List, Set, Tuple

from eth_account import Account
from eth_utils import to_checksum_address
from rich.console import Console
from rich.table import Table
from web3 import Web3

from src.claims.wallet import CHAIN_ID
from src.claims.reclaim_guard import (
    count_reward_transfers,
    decode_voter_revert,
    signed_tx_raw_bytes,
    wait_for_receipt,
)

logger = logging.getLogger(__name__)

console = Console()

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
