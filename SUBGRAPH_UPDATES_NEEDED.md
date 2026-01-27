# Subgraph & Code Updates - COMPLETED

## ✅ Subgraph Updates (v0.0.3)

**Fixed BribeV2 Event Tracking:**

- Changed from `NotifyReward(indexed address from, indexed address reward, uint256 amount)`
- To correct: `RewardAdded(indexed address rewardToken, uint256 amount, uint256 startTimestamp)`

**Updated Bribe Entity Schema:**

```graphql
type Bribe @entity {
  id: ID!
  epoch: BigInt! # NEW: Calculated from startTimestamp
  bribeContract: Bytes! # Internal or external bribe contract
  rewardToken: Bytes! # Token being offered as reward
  amount: BigInt! # Amount of reward tokens
  blockNumber: BigInt!
  blockTimestamp: BigInt!
  transactionHash: Bytes!
}
```

**What This Tracks:**

- Trading fees pushed to internal bribe contracts via `VoterV5.distributeFees()`
- External incentives added via `BribeV2.notifyRewardAmount()`
- All rewards claimable by veHYDX voters based on their voting power

## ✅ Python Code Updates

**Updated Files:**

1. **src/database.py**
   - Updated Bribe model: `bribe_contract`, `reward_token`, `amount`, `epoch`
   - Removed GaugeReward (was for LP emissions, not voter rewards)
   - Updated `save_bribe()` method with new parameters
   - Added `get_bribes_by_gauge()` to query both internal/external bribes

2. **src/subgraph_client.py**
   - Updated `fetch_bribes()` query to include `epoch` field
   - Added epoch filtering parameter
   - Returns epoch, bribeContract, rewardToken, amount

3. **config.py**
   - Updated BRIBE_ABI: `RewardAdded` event instead of `NotifyReward`

4. **main.py**
   - Updated backfill command to fetch bribes from subgraph
   - Converts amount from wei to token units
   - Saves to database with epoch timestamp

## Testing

Run after subgraph syncs:

```bash
# Test subgraph schema
python test_bribe_schema.py

# Backfill data
python main.py backfill --start-block 35273810 --epochs 12

# Analyze ROI once bribes exist
python main.py historical --epochs 5
```

## Current Status

**Waiting for:**

- Subgraph to fully sync with new schema
- `VoterV5.distributeFees()` to be called to populate internal bribe rewards
- External parties to add bribes via `BribeV2.notifyRewardAmount()`

**Once data exists:**

- Can calculate ROI per gauge: total rewards ÷ total votes
- Optimizer will recommend vote allocation to maximize returns
- Historical analysis will show opportunity cost of suboptimal voting
