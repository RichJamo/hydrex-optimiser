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
    Post: our votes already counted in weightsAt(pool, vote_epoch) at query_block, per
          standing_votes_for_epoch. Raises on a failed read; the caller chooses the
          fallback.
"""

from typing import Dict, Iterable, Optional, Tuple

from multicall import Call, Multicall
from web3 import Web3

from config.settings import ONE_E18, WEEK

VOTE_FROM_ESCROW = "escrow"
VOTE_FROM_SIGNER = "signer"
VOTE_FROM_CHOICES = (VOTE_FROM_ESCROW, VOTE_FROM_SIGNER)


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
) -> Dict[str, float]:
    """Read lastVoted and every pool's votes in one Multicall3 round trip."""
    voter = Web3.to_checksum_address(voter_address)
    account = Web3.to_checksum_address(account)
    unique_pools = sorted({str(p).lower() for p in pools if p})
    calls = [
        Call(voter, ["lastVoted(address)(uint256)", account], [("last_voted", None)])
    ]
    calls += [
        Call(
            voter,
            [
                "votes(address,address)(uint256)",
                account,
                Web3.to_checksum_address(pool),
            ],
            [(pool, None)],
        )
        for pool in unique_pools
    ]
    results = Multicall(
        calls, _w3=w3, block_id=int(query_block), require_success=True
    )()
    votes_by_pool = {pool: int(results[pool]) / ONE_E18 for pool in unique_pools}
    return standing_votes_for_epoch(
        int(results["last_voted"]), votes_by_pool, vote_epoch
    )
