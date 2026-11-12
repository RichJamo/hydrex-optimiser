"""Find the block where a Hydrex epoch actually flips on-chain.

An epoch does not flip at 00:00 UTC on Thursday. It flips when a keeper calls the
Minter and ``Minter.active_period()`` advances to the new epoch. In the seven epochs
from 2026-10-01 to 2026-11-12 that happened 9-17 seconds after 00:00 (see the measured
table in docs/OPERATIONS_RUNBOOK.md section 4). That block, the mint block, is the
boundary block: the Voter's ``_epochTimestamp()`` and every bribe contract key their
accounting by ``active_period()``.

Why the mint block, not the first block stamped 00:00:
- Votes can land in that first second. ``VoterV5._voteDelay`` reverts with
  ``EpochStale`` only once ``block.timestamp > active_period + WEEK``, so a vote in a
  block stamped exactly 00:00:00 still counts for the closing epoch. Vote weights are
  final after the last block stamped 00:00:00 (about five blocks at 200 ms).
- Bribes can land in the whole gap. ``BribeV2.notifyRewardAmount`` keys a deposit by
  ``active_period()``, which still names the closing epoch until the mint, so a bribe
  paid during the gap is paid to the votes that just closed.
- The default review reads are keyed by the closed epoch: ``weightsAt(pool,
  vote_epoch)`` and ``rewardData(token, vote_epoch)``. The mint moves new votes and
  deposits to the next key, so state later in the mint block does not reach them.
  Three reads are not keyed, and none runs by default: ``isAlive`` in
  fetch_boundary_votes with ``--active-source onchain``, reward-token enumeration
  (which only grows; a new token reads zero for the old epoch), and bribe
  ``balanceOf`` for the diagnostic ``balances`` source.

Block numbers are found by binary search on chain state, never extrapolated from a
block rate: Base went from 2-second to 200-millisecond blocks on 2026-11-05 (the Denim
upgrade), and several blocks can now share one timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from web3 import Web3

WEEK_SECONDS = 604800

MINTER_ACTIVE_PERIOD_ABI = [
    {
        "name": "active_period",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    }
]
VOTER_MINTER_ABI = [
    {
        "name": "minter",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    }
]


class EpochNotFlippedError(RuntimeError):
    """The Minter has not yet advanced to the requested epoch at the latest block."""


class InconsistentChainStateError(RuntimeError):
    """Chain state contradicts an assumption the search depends on."""


@dataclass(frozen=True)
class EpochFlip:
    """Where one epoch flipped.

    Invariants: ``first_block_at_epoch_time <= mint_block``, and
    ``mint_lag_seconds == mint_block_timestamp - epoch >= 0``.
    """

    epoch: int
    minter_address: str
    first_block_at_epoch_time: int
    mint_block: int
    mint_block_timestamp: int

    @property
    def mint_lag_blocks(self) -> int:
        return self.mint_block - self.first_block_at_epoch_time

    @property
    def mint_lag_seconds(self) -> int:
        return self.mint_block_timestamp - self.epoch


def first_block_where(is_reached: Callable[[int], bool], low: int, high: int) -> int:
    """Return the lowest block in [low, high] for which ``is_reached`` is true.

    Precondition: ``is_reached`` is monotone over [low, high] (false then true) and
    ``is_reached(high)`` is true; raises ``InconsistentChainStateError`` otherwise.
    Makes about log2(high - low) calls.
    """
    if low > high:
        raise ValueError(f"empty search range [{low}, {high}]")
    if not is_reached(high):
        raise InconsistentChainStateError(f"condition is not met at the upper bound, block {high}")
    while low < high:
        middle = (low + high) // 2
        if is_reached(middle):
            high = middle
        else:
            low = middle + 1
    return low


def find_epoch_flip(w3: Web3, voter_address: str, epoch: int) -> EpochFlip:
    """Locate the first block stamped at or after ``epoch`` and the block where it minted.

    Preconditions: ``epoch`` is a multiple of WEEK_SECONDS (Thursday 00:00 UTC), the RPC
    serves historical state (archive reads), and the epoch has already flipped at the
    latest block. Postcondition: ``active_period()`` is exactly ``epoch`` at the mint
    block and earlier than ``epoch`` one block before it.
    Raises ``ValueError`` for an unaligned epoch, ``EpochNotFlippedError`` before the
    flip, and ``InconsistentChainStateError`` if the Minter shows the epoch before its
    start time or skipped it.
    """
    epoch = int(epoch)
    if epoch <= 0 or epoch % WEEK_SECONDS != 0:
        raise ValueError(f"epoch {epoch} is not a Thursday 00:00 UTC multiple of {WEEK_SECONDS}")

    latest_block = int(w3.eth.block_number)

    def timestamp_of(block: int) -> int:
        return int(w3.eth.get_block(block)["timestamp"])

    if timestamp_of(latest_block) < epoch:
        raise EpochNotFlippedError(f"epoch {epoch} has not started at the latest block {latest_block}")

    first_block_at_epoch_time = first_block_where(
        lambda block: timestamp_of(block) >= epoch, 0, latest_block
    )

    # Read the Minter's address as of the epoch, so a later setMinter cannot redirect the search.
    voter = w3.eth.contract(address=Web3.to_checksum_address(voter_address), abi=VOTER_MINTER_ABI)
    minter_address = Web3.to_checksum_address(
        voter.functions.minter().call(block_identifier=first_block_at_epoch_time)
    )
    minter = w3.eth.contract(address=minter_address, abi=MINTER_ACTIVE_PERIOD_ABI)

    def active_period_at(block: int) -> int:
        return int(minter.functions.active_period().call(block_identifier=block))

    if active_period_at(latest_block) < epoch:
        raise EpochNotFlippedError(
            f"Minter has not reached epoch {epoch} at the latest block {latest_block}"
        )
    if first_block_at_epoch_time > 0 and active_period_at(first_block_at_epoch_time - 1) >= epoch:
        raise InconsistentChainStateError(
            f"Minter shows epoch {epoch} before its start time, at block {first_block_at_epoch_time - 1}"
        )

    mint_block = first_block_where(
        lambda block: active_period_at(block) >= epoch, first_block_at_epoch_time, latest_block
    )
    period_at_mint = active_period_at(mint_block)
    if period_at_mint != epoch:
        raise InconsistentChainStateError(
            f"Minter skipped epoch {epoch}: active_period() went to {period_at_mint} at block {mint_block}"
        )
    return EpochFlip(
        epoch=epoch,
        minter_address=minter_address,
        first_block_at_epoch_time=first_block_at_epoch_time,
        mint_block=mint_block,
        mint_block_timestamp=timestamp_of(mint_block),
    )
