"""
Which account casts our vote, and how much voting power the Voter will count for it.

VoterV5 weighs a vote by `ve.getPastVotes(msg.sender, _epochTimestamp())` — the votes
delegated to the calling address at the START of the epoch. `balanceOfNFT` does not
reflect delegation, so it can show full power while the vote reverts with
InsufficientVotingPower(). That is what happened in epoch 1788998400: veNFT #19435's
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
"""

from typing import Optional, Tuple

from web3 import Web3

VOTE_FROM_ESCROW = "escrow"
VOTE_FROM_SIGNER = "signer"
VOTE_FROM_CHOICES = (VOTE_FROM_ESCROW, VOTE_FROM_SIGNER)


def resolve_voting_account(vote_from: str, escrow_address: str, signer_address: str) -> str:
    mode = str(vote_from or "").strip().lower()
    if mode not in VOTE_FROM_CHOICES:
        raise ValueError(f"VOTE_FROM must be one of {VOTE_FROM_CHOICES}, got {vote_from!r}")
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
        raise ValueError(f"VOTE_FROM must be one of {VOTE_FROM_CHOICES}, got {vote_from!r}")
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
        epoch_start = int.from_bytes(_call(w3, voter_address, "_epochTimestamp()"), "big")
        votes = int.from_bytes(
            _call(w3, ve, "getPastVotes(address,uint256)", _word_address(account) + _word_uint(epoch_start)),
            "big",
        )
        delegate: Optional[str] = None
        try:
            raw = _call(w3, ve, "delegates(address)", _word_address(account))
            delegate = _word_to_address(raw)
        except Exception:  # noqa: BLE001 - delegate is diagnostic only
            delegate = None
    except Exception as exc:  # noqa: BLE001 - surfaced to the operator, never fatal here
        return False, f"could not read voting power for {account} ({type(exc).__name__}: {exc})", 0

    whole = votes / 1e18
    if votes == 0:
        hint = f"; its votes are currently delegated to {delegate}" if delegate and delegate.lower() != account.lower() else ""
        return False, (
            f"{account} has 0 votes at epoch start {epoch_start} — the vote will revert "
            f"with InsufficientVotingPower(){hint}. A delegation change only counts from the next epoch start."
        ), 0
    detail = f"{account} has {whole:,.2f} votes at epoch start {epoch_start}"
    if expected_votes and whole < float(expected_votes):
        detail += (
            f" — below the configured {int(expected_votes):,}; the vote still uses the full "
            f"{whole:,.2f}, but expected-return figures are overstated"
        )
    return True, detail, votes
