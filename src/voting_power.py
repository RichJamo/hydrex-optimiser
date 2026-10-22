"""
Which account casts our vote, and how much voting power the Voter will count for it.

VoterV5 weighs a vote by `ve.getPastVotes(msg.sender, _epochTimestamp())` — the votes
delegated to the calling address at the START of the epoch. `balanceOfNFT` does not
reflect delegation, so it can show full power while the vote reverts with
InsufficientVotingPower(). That is what happened in epoch 1788998400: our veNFT's
votes were delegated from the PartnerEscrow to the signer on 2026-09-09, and every
escrow vote after the next epoch start reverted.

Contract:
  resolve_voting_account(vote_from, escrow_address, signer_address)
    Pre:  vote_from in {"escrow", "signer"}.
    Post: returns the checksummed address that sends Voter.vote; raises ValueError for
          an unknown mode or a missing address, so misconfiguration fails at startup.
  check_epoch_voting_power(w3, voter_address, account, expected_votes)
    Post: returns (ok, detail, votes). ok is False when the account has zero votes at
          the epoch start (the vote would revert) or the read failed. A shortfall
          against expected_votes is reported in detail but stays ok, because the Voter
          spreads whatever power exists across the submitted weights.
    Never raises: callers decide whether a failed check blocks a run.
  standing_votes_for_epoch(last_voted, votes_by_pool, vote_epoch)
    Post: returns {pool (lower): votes} with zero entries dropped when last_voted falls
          inside [vote_epoch, vote_epoch + WEEK), else {}. Voter.votes(account, pool)
          keeps the previous epoch's votes after the flip while weightsAt(pool, epoch)
          starts again from zero, so votes cast in another epoch are not in the
          totals and must not be subtracted from them.
  fetch_standing_votes(w3, voter_address, account, vote_epoch, pools, query_block)
    Post: StandingVotes(last_voted, by_pool): the account's lastVoted at query_block,
          and our votes already counted in weightsAt(pool, vote_epoch) there, per
          standing_votes_for_epoch. Raises on a failed read (Multicall3 aggregate
          reverts as a whole); the caller chooses the fallback.
"""

from typing import Dict, Iterable, NamedTuple, Optional, Tuple

from web3 import Web3

from config.settings import ONE_E18, WEEK

VOTE_FROM_ESCROW = "escrow"
VOTE_FROM_SIGNER = "signer"
VOTE_FROM_CHOICES = (VOTE_FROM_ESCROW, VOTE_FROM_SIGNER)

# Canonical Multicall3 deployment, at the same address on Base and every other EVM chain.
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_AGGREGATE_ABI = [
    {
        "name": "aggregate",
        "type": "function",
        "stateMutability": "payable",
        "inputs": [
            {
                "name": "calls",
                "type": "tuple[]",
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "callData", "type": "bytes"},
                ],
            }
        ],
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData", "type": "bytes[]"},
        ],
    }
]


class StandingVotes(NamedTuple):
    last_voted: int
    by_pool: Dict[str, float]


def resolve_voting_account(
    vote_from: str, escrow_address: str, signer_address: str
) -> str:
    mode = str(vote_from or "").strip().lower()
    if mode not in VOTE_FROM_CHOICES:
        raise ValueError(
            f"VOTE_FROM must be one of {VOTE_FROM_CHOICES}, got {vote_from!r}"
        )
    address = escrow_address if mode == VOTE_FROM_ESCROW else signer_address
    if not address:
        which = "MY_ESCROW_ADDRESS" if mode == VOTE_FROM_ESCROW else "the signer wallet"
        raise ValueError(f"VOTE_FROM={mode} requires {which} to be configured")
    return Web3.to_checksum_address(address)


def resolve_vote_target(vote_from: str, escrow_address: str, voter_address: str) -> str:
    """Contract the vote transaction is sent to: the PartnerEscrow, or the Voter directly.

    Both expose vote(address[],uint256[]); the escrow forwards to the Voter as msg.sender.
    """
    mode = str(vote_from or "").strip().lower()
    if mode not in VOTE_FROM_CHOICES:
        raise ValueError(
            f"VOTE_FROM must be one of {VOTE_FROM_CHOICES}, got {vote_from!r}"
        )
    address = escrow_address if mode == VOTE_FROM_ESCROW else voter_address
    if not address:
        which = "MY_ESCROW_ADDRESS" if mode == VOTE_FROM_ESCROW else "VOTER_ADDRESS"
        raise ValueError(f"VOTE_FROM={mode} requires {which} to be configured")
    return Web3.to_checksum_address(address)


def _call(w3, to: str, signature: str, args: bytes = b"") -> bytes:
    data = Web3.keccak(text=signature)[:4] + args
    return w3.eth.call({"to": Web3.to_checksum_address(to), "data": data})


def _word_address(address: str) -> bytes:
    return bytes.fromhex(Web3.to_checksum_address(address)[2:].lower().rjust(64, "0"))


def _word_to_address(word: bytes) -> str:
    # eth_call returns HexBytes, whose .hex() carries a 0x prefix in some versions; slice bytes.
    return Web3.to_checksum_address(bytes(word)[-20:])


def _word_uint(value: int) -> bytes:
    return int(value).to_bytes(32, "big")


def check_epoch_voting_power(
    w3, voter_address: str, account: str, expected_votes: int
) -> Tuple[bool, str, int]:
    try:
        ve = _word_to_address(_call(w3, voter_address, "_ve()"))
        epoch_start = int.from_bytes(
            _call(w3, voter_address, "_epochTimestamp()"), "big"
        )
        votes = int.from_bytes(
            _call(
                w3,
                ve,
                "getPastVotes(address,uint256)",
                _word_address(account) + _word_uint(epoch_start),
            ),
            "big",
        )
        delegate: Optional[str] = None
        try:
            raw = _call(w3, ve, "delegates(address)", _word_address(account))
            delegate = _word_to_address(raw)
        except Exception:  # noqa: BLE001 - delegate is diagnostic only
            delegate = None
    except (
        Exception
    ) as exc:  # noqa: BLE001 - surfaced to the operator, never fatal here
        return (
            False,
            f"could not read voting power for {account} ({type(exc).__name__}: {exc})",
            0,
        )

    whole = votes / 1e18
    if votes == 0:
        hint = (
            f"; its votes are currently delegated to {delegate}"
            if delegate and delegate.lower() != account.lower()
            else ""
        )
        return (
            False,
            (
                f"{account} has 0 votes at epoch start {epoch_start} — the vote will revert "
                f"with InsufficientVotingPower(){hint}. A delegation change only counts from the next epoch start."
            ),
            0,
        )
    detail = f"{account} has {whole:,.2f} votes at epoch start {epoch_start}"
    if expected_votes and whole < float(expected_votes):
        detail += (
            f" — below the configured {int(expected_votes):,}; the vote still uses the full "
            f"{whole:,.2f}, but expected-return figures are overstated"
        )
    return True, detail, votes


def standing_votes_for_epoch(
    last_voted: int, votes_by_pool: Dict[str, float], vote_epoch: int
) -> Dict[str, float]:
    if not (int(vote_epoch) <= int(last_voted) < int(vote_epoch) + int(WEEK)):
        return {}
    return {
        str(pool).lower(): float(votes)
        for pool, votes in votes_by_pool.items()
        if float(votes) > 0
    }


def fetch_standing_votes(
    w3,
    voter_address: str,
    account: str,
    vote_epoch: int,
    pools: Iterable[str],
    query_block: int,
) -> StandingVotes:
    """Read lastVoted and every pool's votes in one Multicall3 round trip.

    Uses web3's synchronous client rather than the multicall package: that package runs
    an asyncio loop per call and reuses an aiohttp session bound to another thread's
    loop, which fails ("Timeout context manager should be used inside a task") when the
    read runs on a deadline thread, as auto_voter runs it.
    """
    voter = Web3.to_checksum_address(voter_address)
    account_word = _word_address(account)
    unique_pools = sorted({str(p).lower() for p in pools if p})
    calls = [(voter, Web3.keccak(text="lastVoted(address)")[:4] + account_word)]
    votes_selector = Web3.keccak(text="votes(address,address)")[:4]
    calls += [
        (voter, votes_selector + account_word + _word_address(pool))
        for pool in unique_pools
    ]
    multicall = w3.eth.contract(
        address=Web3.to_checksum_address(MULTICALL3_ADDRESS),
        abi=MULTICALL3_AGGREGATE_ABI,
    )
    _block, return_data = multicall.functions.aggregate(calls).call(
        block_identifier=int(query_block)
    )
    if len(return_data) != len(calls) or any(len(r) != 32 for r in return_data):
        raise ValueError(
            f"Multicall3 returned {len(return_data)} results for {len(calls)} calls"
        )
    words = [int.from_bytes(bytes(r), "big") for r in return_data]
    last_voted = words[0]
    votes_by_pool = {
        pool: word / ONE_E18 for pool, word in zip(unique_pools, words[1:])
    }
    return StandingVotes(
        last_voted, standing_votes_for_epoch(last_voted, votes_by_pool, vote_epoch)
    )
