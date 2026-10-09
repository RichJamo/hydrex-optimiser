"""The already-claimed guard, transfer detection, and claim-source resolution.

Moved out of scripts/claim_and_swap_rewards.py (issue #8) without behaviour changes.
"""

import logging
import re
import time
from typing import Dict, List, Optional, Tuple

from eth_utils import keccak
from eth_utils import to_checksum_address
from web3 import Web3
from web3.exceptions import TransactionNotFound

from config import VOTER_ABI

logger = logging.getLogger(__name__)

# Bribe contracts book an epoch's entitlement against the account that held the votes,
# which for a delegated veNFT is still its owner — voting by delegation does NOT move
# accrual to the delegate. So the claim source follows veNFT ownership, resolved on-chain
# at runtime by resolve_claim_source(), not VOTE_FROM. See CLAIM_SOURCE_AUTO.
CLAIM_SOURCE_AUTO = "auto"
CLAIM_SOURCES = ("escrow", "voter", "distributor")


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


# keccak256("Transfer(address,address,uint256)")
ERC20_TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)


def _topic_to_address(topic) -> str:
    """Take the low 20 bytes of an indexed 32-byte topic as a lowercase hex address."""
    raw = topic.hex() if hasattr(topic, "hex") else str(topic)
    return "0x" + raw[-40:].lower()


def count_reward_transfers(receipt, recipient: str) -> int:
    """Count ERC-20 Transfer logs in `receipt` that credit `recipient`.

    A claim that transfers nothing still returns status=1, so the receipt status alone
    cannot tell a working claim from a no-op one. Epoch 1789603200: two Voter self-claims
    burned ~510k gas each, emitted zero logs, and were reported as successful while the
    week's rewards stayed unclaimed. Counting credits to the recipient is what
    distinguishes the two.

    Preconditions: `receipt` exposes `logs` (web3 AttributeDict or plain dict); each log
    exposes `topics`, whose entries may be HexBytes or hex strings.
    Postcondition: returns a count >= 0; a receipt with no readable logs counts as 0.
    """
    recipient_l = (recipient or "").lower()
    if not recipient_l:
        return 0

    transfers = 0
    for log in getattr(receipt, "logs", None) or []:
        topics = (
            getattr(log, "topics", None)
            or (log.get("topics") if isinstance(log, dict) else None)
            or []
        )
        if len(topics) < 3:
            continue
        signature = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        if not signature.lower().startswith("0x"):
            signature = "0x" + signature
        if signature.lower() != ERC20_TRANSFER_TOPIC:
            continue
        if _topic_to_address(topics[2]) == recipient_l:
            transfers += 1
    return transfers


def summarize_claim_transfers(claim_results: List[Dict]) -> Tuple[int, int]:
    """Return (broadcast_successes, total_transfers_in) across Phase 3 claim results."""
    successes = 0
    transfers = 0
    for result in claim_results:
        if result.get("status") != "success":
            continue
        successes += 1
        transfers += int(result.get("transfers_in") or 0)
    return successes, transfers


class ClaimMovedNothingError(RuntimeError):
    """Every broadcast claim succeeded, yet no tokens reached the recipient."""


def assert_claims_moved_tokens(
    claim_results: List[Dict], recipient: str, claim_source: str
) -> None:
    """Fail loudly when successful claims transferred nothing.

    This is the guard that would have caught the epoch-1789603200 silent no-op. It is
    deliberately an exception rather than a warning: with nothing claimed there is
    nothing for Phase 4 to swap, so continuing only buries the problem further.

    Precondition: `claim_results` are Phase 3 results; dry-run rows carry no transfers.
    Postcondition: returns None, or raises ClaimMovedNothingError naming the likely cause.
    """
    successes, transfers = summarize_claim_transfers(claim_results)
    if successes == 0 or transfers > 0:
        return
    raise ClaimMovedNothingError(
        f"{successes} claim transaction(s) succeeded via --claim-source {claim_source} "
        f"but transferred no tokens to {recipient}. A claim that moves nothing still "
        "returns status=1, so this is reported as success everywhere else.\n"
        "  Most likely the entitlement belongs to the veNFT rather than the signer — "
        "bribes book rewards against the account that held the votes, and delegation "
        "does not change that. Try --claim-source escrow.\n"
        "  If the epoch was genuinely already claimed, re-run with --force to downgrade "
        "this to a warning."
    )


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
        raise AttributeError(
            "Signed transaction has neither raw_transaction nor rawTransaction"
        )
    return raw


def decode_voter_revert(exc: Exception) -> Optional[str]:
    """Decode 4-byte revert selector from exception text when available."""
    text = str(exc)
    match = re.search(r"0x([0-9a-fA-F]{8})", text)
    if not match:
        return None
    selector = match.group(1).lower()
    return VOTER_ERROR_SELECTORS.get(selector)


ESCROW_TOKEN_ID_ABI = [
    {
        "name": "tokenId",
        "inputs": [],
        "outputs": [{"type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    }
]
VOTING_ESCROW_OWNERSHIP_ABI = [
    {
        "name": "ownerOf",
        "inputs": [{"type": "uint256"}],
        "outputs": [{"type": "address"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "name": "isApprovedOrOwner",
        "inputs": [{"type": "address"}, {"type": "uint256"}],
        "outputs": [{"type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
]


RECLAIM_GUARD_PROCEED = "proceed"
RECLAIM_GUARD_NOT_APPLICABLE = "not_applicable"
RECLAIM_GUARD_FORCED = "forced"
RECLAIM_GUARD_BLOCK = "block"


def evaluate_reclaim_guard(
    prior_success_count: int,
    skip_claims: bool,
    force: bool,
) -> Tuple[str, str]:
    """Decide whether the already-claimed guard stops this run, and say why.

    The guard stops Phase 3 claiming one epoch twice. Its four outcomes are mutually
    exclusive, and reporting the wrong one has a cost of its own: a run told that
    `--force` bypassed a guard teaches its operator to reach for `--force` by habit.
    That is not hypothetical. A `--skip-claims` run has been exempt since f6134b8, but
    the exemption and the force warning were separate `if` statements over the same
    state, so a swap-only run carrying a redundant `--force` logged both "guard not
    applicable" and "--force passed: bypassing already-claimed guard". The second line
    is what the epoch-1789603200 and 1790208000 notes recorded, and it is why both
    epochs were swapped with a flag they did not need.

    Pre:  prior_success_count >= 0.
    Post: returns (decision, reason), where decision is exactly one of
            "proceed"        - no prior Phase 3 success rows; the guard has nothing to say.
            "not_applicable" - prior rows exist but this run skips Phase 3 entirely, so
                               there is no claim path to protect. Takes precedence over
                               "forced": `--force` bypasses nothing here and must never
                               be reported as having done so.
            "forced"         - prior rows exist, Phase 3 would run, `--force` overrode it.
            "block"          - prior rows exist, Phase 3 would run, no `--force`. Caller
                               must abort.
          Never raises; a negative count is treated as zero rather than trusted.
    """
    if prior_success_count <= 0:
        return RECLAIM_GUARD_PROCEED, "no prior Phase 3 claim rows for this epoch"
    if skip_claims:
        return (
            RECLAIM_GUARD_NOT_APPLICABLE,
            f"{prior_success_count} prior Phase 3 claim rows, but --skip-claims runs no "
            "claim path, so the guard does not apply (--force is not needed here)",
        )
    if force:
        return (
            RECLAIM_GUARD_FORCED,
            f"--force passed: bypassing the already-claimed guard despite "
            f"{prior_success_count} prior Phase 3 success rows",
        )
    return (
        RECLAIM_GUARD_BLOCK,
        f"{prior_success_count} prior Phase 3 claim success rows already exist",
    )


def choose_claim_source(
    explicit: Optional[str],
    signer_address: str,
    escrow_address: str,
    venft_owner: Optional[str],
    signer_is_approved_or_owner: Optional[bool],
) -> Tuple[str, str]:
    """Decide which contract Phase 3 claims through, and say why.

    Bribe contracts book an epoch's entitlement against the account that held the votes.
    For a delegated veNFT that is still its *owner*: voting by delegation does not move
    accrual to the delegate. Epoch 1789603200 proved it — the signer voted by delegation,
    `earned(tokenId)` held the full entitlement, `earnedOwner(signer)` was zero, and a
    Voter self-claim moved nothing while reporting status=1. So ownership decides the
    claim source, never VOTE_FROM.

    Preconditions:
      - `explicit` is None (resolve automatically) or one of CLAIM_SOURCES.
      - `venft_owner` is None when ownership could not be read on-chain.
      - `signer_is_approved_or_owner` is None when the check could not be made.

    Postconditions:
      - returns (source, reason); `source` is always one of CLAIM_SOURCES.
      - an explicit choice is returned unchanged, so the operator can always override.

    Invariant: when an escrow is configured, "voter" is only chosen if the signer owns
    the veNFT or is approved for it — so an escrow-owned veNFT can never be routed to a
    Voter self-claim. That is the case that silently claimed nothing.

    With NO escrow configured the invariant does not hold: "voter" is returned as the
    only remaining on-chain option even when ownership is unreadable (the normal state
    here, since the tokenId is read from the escrow) or belongs to someone else. That is
    correct for a signer that owns its veNFT directly; when it is wrong,
    assert_claims_moved_tokens aborts the run rather than letting it pass as success.
    """
    if explicit:
        return explicit, f"explicit --claim-source {explicit}"

    signer_l = (signer_address or "").lower()
    escrow_l = (escrow_address or "").lower()
    owner_l = (venft_owner or "").lower()

    if owner_l:
        if owner_l == signer_l:
            return "voter", "signer owns the veNFT, so the Voter pays it directly"
        if escrow_l and owner_l == escrow_l:
            return (
                "escrow",
                f"veNFT is owned by the escrow {escrow_address}, which books the entitlement",
            )
        if signer_is_approved_or_owner:
            return "voter", "signer is approved for the veNFT, so it can self-claim"
        if escrow_l:
            return (
                "escrow",
                f"veNFT owner {venft_owner} is not the signer and not approved to it; "
                f"claiming through the configured escrow {escrow_address}",
            )
        return (
            "voter",
            f"veNFT owner {venft_owner} is not the signer and no escrow is configured",
        )

    if escrow_l:
        return (
            "escrow",
            f"veNFT ownership unreadable; falling back to configured escrow {escrow_address}",
        )
    return "voter", "veNFT ownership unreadable and no escrow configured"


def resolve_claim_source(
    w3: Web3,
    signer_address: str,
    explicit: Optional[str],
    escrow_address: str,
    ve_address: str,
) -> Tuple[str, str]:
    """Read veNFT ownership on-chain, then delegate the decision to choose_claim_source.

    The managed tokenId is read from the escrow's own `tokenId()` getter, so no extra
    configuration is needed. Every read is best-effort: an unreachable contract degrades
    to the documented fallbacks rather than aborting the run.
    """
    if explicit:
        return choose_claim_source(explicit, signer_address, escrow_address, None, None)

    venft_owner: Optional[str] = None
    signer_is_approved: Optional[bool] = None
    token_id: Optional[int] = None

    if escrow_address:
        try:
            escrow = w3.eth.contract(
                address=to_checksum_address(escrow_address), abi=ESCROW_TOKEN_ID_ABI
            )
            token_id = int(escrow.functions.tokenId().call())
        except Exception as e:
            logger.warning(
                f"Could not read escrow tokenId() for claim-source resolution: {e}"
            )

    if token_id is not None and ve_address:
        try:
            ve = w3.eth.contract(
                address=to_checksum_address(ve_address), abi=VOTING_ESCROW_OWNERSHIP_ABI
            )
            venft_owner = ve.functions.ownerOf(token_id).call()
            signer_is_approved = bool(
                ve.functions.isApprovedOrOwner(
                    to_checksum_address(signer_address), token_id
                ).call()
            )
        except Exception as e:
            logger.warning(
                f"Could not read veNFT ownership for claim-source resolution: {e}"
            )

    source, reason = choose_claim_source(
        explicit=None,
        signer_address=signer_address,
        escrow_address=escrow_address,
        venft_owner=venft_owner,
        signer_is_approved_or_owner=signer_is_approved,
    )
    if token_id is not None:
        reason = f"{reason} (veNFT #{token_id})"
    return source, reason
