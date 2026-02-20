"""
Contract-based bribe reward calculator.

Uses on-chain contract snapshots to calculate expected rewards:
  1. Query VoterV5 delegation state (delegates, balanceOfNFTAt, getPastVotes) at epoch
  2. Query Bribe contract state (rewardData, totalSupplyAt, balanceOfOwnerAt) at epoch
  3. Apply formula: reward = (rewardsPerEpoch / totalSupply) * delegateeBalance * weight

This module consolidates the calculation logic shared between:
  - verify_historical_bribes.py (final expected + pre-flip estimates)
  - analyze_boundary_maximum_return.py (expected per pool)
"""

from typing import Any, Dict, Tuple, Optional
from web3 import Web3

ONE_E18 = 10**18
SCALE_32 = 10**32


class VeDelegationSnapshot:
    """Delegation and voting power at a specific epoch/block."""
    def __init__(
        self,
        delegatee: str,
        power: int,
        delegatee_past_votes: int,
    ):
        self.delegatee = delegatee
        self.power = power
        self.delegatee_past_votes = delegatee_past_votes
        self.weight_raw_1e18 = self._compute_weight()

    def _compute_weight(self) -> int:
        """Weight = power / delegatee_past_votes, scaled to 1e18."""
        if self.delegatee_past_votes <= 0:
            return 0
        return (self.power * ONE_E18) // self.delegatee_past_votes

    def is_valid(self) -> bool:
        """Check if delegation is in a usable state."""
        return (
            self.delegatee != "0x0000000000000000000000000000000000000000"
            and self.power > 0
            and self.delegatee_past_votes > 0
            and self.weight_raw_1e18 > 0
        )


class BribeContractState:
    """Reward and pool state from a bribe contract at a specific epoch."""
    def __init__(
        self,
        rewards_per_epoch_raw: int,
        total_supply_at_epoch: int,
        delegatee_pool_balance: int,
    ):
        self.rewards_per_epoch_raw = rewards_per_epoch_raw
        self.total_supply_at_epoch = total_supply_at_epoch
        self.delegatee_pool_balance = delegatee_pool_balance

    def is_complete(self) -> bool:
        """Check if all contract inputs are available."""
        return (
            self.rewards_per_epoch_raw > 0
            and self.total_supply_at_epoch > 0
            and self.delegatee_pool_balance > 0
        )


def query_ve_delegation_snapshot(
    w3: Web3,
    ve_contract: Any,
    token_id: int,
    calc_epoch: int,
    block_identifier: Optional[int] = None,
) -> VeDelegationSnapshot:
    """
    Query ve delegation state at a specific epoch/block.
    
    Args:
        w3: Web3 instance
        ve_contract: ve contract instance
        token_id: ve NFT token ID
        calc_epoch: WEEK-aligned epoch timestamp
        block_identifier: Optional block number/tag; if None, queries latest state
    
    Returns:
        VeDelegationSnapshot with delegation info
    """
    kwargs = {"block_identifier": block_identifier} if block_identifier else {}

    delegatee = ve_contract.functions.delegates(token_id, calc_epoch).call(**kwargs)
    power = ve_contract.functions.balanceOfNFTAt(token_id, calc_epoch).call(**kwargs)

    delegatee_past_votes = 0
    if delegatee != "0x0000000000000000000000000000000000000000":
        delegatee_past_votes = ve_contract.functions.getPastVotes(delegatee, calc_epoch).call(**kwargs)

    return VeDelegationSnapshot(delegatee, power, delegatee_past_votes)


def query_bribe_contract_state(
    w3: Web3,
    bribe_contract: Any,
    token_address: str,
    delegatee: str,
    calc_epoch: int,
    block_identifier: Optional[int] = None,
) -> BribeContractState:
    """
    Query bribe contract state at a specific epoch/block.
    
    Args:
        w3: Web3 instance
        bribe_contract: bribe contract instance
        token_address: reward token address
        delegatee: delegatee address
        calc_epoch: WEEK-aligned epoch timestamp
        block_identifier: Optional block number/tag; if None, queries latest state
    
    Returns:
        BribeContractState with contract snapshot
    """
    kwargs = {"block_identifier": block_identifier} if block_identifier else {}

    reward_data = bribe_contract.functions.rewardData(
        Web3.to_checksum_address(token_address), calc_epoch
    ).call(**kwargs)
    rewards_per_epoch_raw = reward_data[1]

    total_supply_at_epoch = bribe_contract.functions.totalSupplyAt(calc_epoch).call(**kwargs)

    delegatee_pool_balance = bribe_contract.functions.balanceOfOwnerAt(
        Web3.to_checksum_address(delegatee), calc_epoch
    ).call(**kwargs)

    return BribeContractState(rewards_per_epoch_raw, total_supply_at_epoch, delegatee_pool_balance)


def calculate_expected_reward(
    ve_snapshot: VeDelegationSnapshot,
    bribe_state: BribeContractState,
    token_decimals: int,
    fallback_db_amount: Optional[float] = None,
    legacy_pool_share: Optional[float] = None,
) -> float:
    """
    Calculate expected reward using contract formula with optional fallback.
    
    Contract formula (when all inputs available):
        reward_per_token = (rewardsPerEpoch * SCALE_32) / totalSupply
        reward = (reward_per_token * delegateeBalance) / SCALE_32
        reward = (reward * weight) / ONE_E18
    
    Fallback strategies (in order):
        1. If rewards zero: use fallback_db_amount (if provided)
        2. If pool balance or supply zero: use legacy_pool_share * fallback_db_amount
        3. Otherwise: use full contract formula
    
    Args:
        ve_snapshot: VeDelegationSnapshot from query_ve_delegation_snapshot()
        bribe_state: BribeContractState from query_bribe_contract_state()
        token_decimals: Token decimal places
        fallback_db_amount: DB amount to use if rewardData is zero (optional)
        legacy_pool_share: Legacy vote-share estimate for fallback (optional)
    
    Returns:
        Expected reward amount in human-readable units (scaled by token_decimals)
    """
    if not ve_snapshot.is_valid():
        return 0.0

    # Hybrid fallback logic:
    # - If rewardData is zero, revert to DB amount (pre-flip scenario)
    # - If no contract share available, use legacy pool share estimate
    
    rewards_baseline_raw = bribe_state.rewards_per_epoch_raw
    if rewards_baseline_raw == 0 and fallback_db_amount is not None:
        rewards_baseline_raw = int(float(fallback_db_amount) * (10 ** token_decimals))

    contract_share_available = (
        bribe_state.total_supply_at_epoch > 0
        and bribe_state.delegatee_pool_balance > 0
        and ve_snapshot.weight_raw_1e18 > 0
    )

    if contract_share_available:
        # Full contract formula
        if bribe_state.total_supply_at_epoch == 0:
            reward_per_token = rewards_baseline_raw * SCALE_32
        else:
            reward_per_token = (rewards_baseline_raw * SCALE_32) // bribe_state.total_supply_at_epoch

        reward_raw = (reward_per_token * bribe_state.delegatee_pool_balance) // SCALE_32
        reward_raw = (reward_raw * ve_snapshot.weight_raw_1e18) // ONE_E18
        return reward_raw / (10 ** token_decimals)
    else:
        # Fallback to legacy pool share estimate
        if legacy_pool_share is not None and fallback_db_amount is not None:
            return float(fallback_db_amount) * legacy_pool_share
        return 0.0


class ContractRewardCalculator:
    """
    Cached calculator for expected rewards using contract snapshots.
    
    Maintains caches to avoid redundant contract queries when computing
    multiple per-token expectations for the same epoch.
    """

    def __init__(self, w3: Web3, ve_contract: Any):
        self.w3 = w3
        self.ve_contract = ve_contract
        
        # Caches for ve delegation snapshots
        self._ve_cache: Dict[Tuple[int, int, Optional[int]], VeDelegationSnapshot] = {}
        
        # Caches for bribe contract state
        self._bribe_cache: Dict[Tuple[str, str, int, str, Optional[int]], BribeContractState] = {}

    def get_ve_snapshot(
        self,
        token_id: int,
        calc_epoch: int,
        block_identifier: Optional[int] = None,
    ) -> VeDelegationSnapshot:
        """Get or query ve delegation snapshot (cached)."""
        cache_key = (token_id, calc_epoch, block_identifier)
        if cache_key not in self._ve_cache:
            self._ve_cache[cache_key] = query_ve_delegation_snapshot(
                self.w3, self.ve_contract, token_id, calc_epoch, block_identifier
            )
        return self._ve_cache[cache_key]

    def get_bribe_state(
        self,
        bribe_contract: Any,
        token_address: str,
        delegatee: str,
        calc_epoch: int,
        block_identifier: Optional[int] = None,
    ) -> BribeContractState:
        """Get or query bribe contract state (cached)."""
        cache_key = (
            bribe_contract.address.lower(),
            token_address.lower(),
            calc_epoch,
            delegatee.lower(),
            block_identifier,
        )
        if cache_key not in self._bribe_cache:
            self._bribe_cache[cache_key] = query_bribe_contract_state(
                self.w3, bribe_contract, token_address, delegatee, calc_epoch, block_identifier
            )
        return self._bribe_cache[cache_key]

    def calculate_reward(
        self,
        token_id: int,
        calc_epoch: int,
        bribe_contract: Any,
        token_address: str,
        token_decimals: int,
        fallback_db_amount: Optional[float] = None,
        legacy_pool_share: Optional[float] = None,
        block_identifier: Optional[int] = None,
    ) -> float:
        """
        Calculate expected reward at a specific epoch/block (using cache).
        
        Handles both final calculations (block_identifier=None for latest state)
        and pre-flip estimates (block_identifier=specific past block).
        
        Args:
            token_id: ve NFT token ID
            calc_epoch: WEEK-aligned epoch timestamp
            bribe_contract: bribe contract instance
            token_address: reward token address
            token_decimals: Token decimal places
            fallback_db_amount: DB amount for fallback if rewardData zero
            legacy_pool_share: Legacy vote-share for fallback if contract data sparse
            block_identifier: Optional past block for pre-flip estimates
        
        Returns:
            Expected reward in human-readable units
        """
        ve_snapshot = self.get_ve_snapshot(token_id, calc_epoch, block_identifier)
        bribe_state = self.get_bribe_state(
            bribe_contract, token_address, ve_snapshot.delegatee, calc_epoch, block_identifier
        )
        return calculate_expected_reward(
            ve_snapshot, bribe_state, token_decimals, fallback_db_amount, legacy_pool_share
        )

    def clear_cache(self):
        """Clear all caches."""
        self._ve_cache.clear()
        self._bribe_cache.clear()
