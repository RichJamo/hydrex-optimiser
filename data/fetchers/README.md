# Data Fetchers

Scripts to populate the database with on-chain data. Run these once per epoch to cache data, then analysis scripts can read from the cache.

## Overview

```
┌─────────────────┐
│ On-chain        │
│ Contracts       │
└────────┬────────┘
         │
    ┌────▼─────────────────────┐
    │ data/fetchers/*.py        │ ← Run once per epoch
    ├──────────────────────────┤
    │ • fetch_votes.py          │
    │ • fetch_bribes.py         │
    │ • fetch_ve_state.py       │
    └────┬──────────────────────┘
         │
    ┌────▼──────────┐
    │ data/data.db  │ ← Cache
    └────┬──────────┘
         │
    ┌────▼──────────────────┐
    │ analysis/*.py          │ ← Read-only
    └───────────────────────┘
```

## Scripts

### `fetch_votes.py`

Fetch gauge vote distribution at a specific epoch.

**What it does:**

- Queries VoterV5.weightsAt() for each gauge
- Stores votes in database
- Captures final vote distribution at epoch

**Usage:**

```bash
python -m data.fetchers.fetch_votes \
    --epoch 1771372800 \
    --vote-epoch 1770854400 \
    --block 42291740
```

**Parameters:**

- `--epoch` (required): Epoch timestamp when snapshot taken
- `--vote-epoch` (required): Vote epoch to query (usually earlier epoch)
- `--block` (optional): Block number to query at (auto-detected if not provided)
- `--database` (optional): Database path (default: data/data.db)

**Output:**

- Stores vote records in `votes` table
- Indexed by (epoch, gauge)

### `fetch_bribes.py`

Fetch bribe/reward data from bribe contracts.

**What it does:**

- Queries bribe contracts for reward data
- Queries event logs for RewardAdded events (recommended)
- Stores rewards in database
- Captures total reward pool per contract

**Usage:**

```bash
python -m data.fetchers.fetch_bribes \
    --epoch 1771372800 \
    --repair-token-metadata
```

**Parameters:**

- `--epoch` (required): Epoch timestamp
- `--bribe-contracts` (optional): Comma-separated addresses to query
- `--database` (optional): Database path
- `--repair-token-metadata` (optional): Runs `scripts/repair_token_metadata.py` after fetch

**Output:**

- Stores bribe records in `bribes` table
- Indexed by (epoch, bribe_contract, reward_token)

**Note:** This script has a limitation - it cannot enumerate all reward tokens from a bribe contract directly on-chain. For production use, you should:

1. Index RewardAdded events off-chain (use a subgraph or event listener)
2. Store known reward tokens in a config
3. Use `process_rewards.py` to parse event logs

### `fetch_ve_state.py`

Fetch ve NFT delegation state snapshot.

**What it does:**

- Queries ve.delegates() - who you delegated to
- Queries ve.balanceOfNFTAt() - your voting power
- Queries ve.getPastVotes() - delegatee's total votes
- Calculates delegation weight (power / delegatee_votes)
- Displays snapshot for verification

**Usage:**

```bash
python -m data.fetchers.fetch_ve_state \
    --epoch 1771372800 \
    --token-id 19435 \
    --block 42291740
```

**Parameters:**

- `--epoch` (required): Epoch timestamp
- `--token-id` (required): ve NFT token ID to query
- `--block` (optional): Block number to query at
- `--database` (optional): Database path

**Output:**

- Prints ve state snapshot to console
- Future: Will store snapshots in database for historical analysis

## Typical Workflow

### Scenario: Analyze rewards at epoch boundary

#### Step 1: Fetch data (run at/after epoch flip, takes a few minutes)

```bash
# Fetch vote distribution
python -m data.fetchers.fetch_votes \
    --epoch 1771372800 \
    --vote-epoch 1770854400

# Record your ve state for reference
python -m data.fetchers.fetch_ve_state \
    --epoch 1771372800 \
    --token-id 19435

# Fetch reward data (requires off-chain indexed bribes or known addresses)
python -m data.fetchers.fetch_bribes \
    --epoch 1771372800 \
    --repair-token-metadata
```

#### Step 2: Analyze (instant, uses cached data)

```bash
# Now analysis scripts can run instantly
python analysis/verify_historical_bribes.py
python analysis/analyze_boundary_maximum_return.py --epoch 1771372800 --k 5
```

### Scenario: Multi-epoch historical analysis

```bash
# Fetch data for multiple epochs
for epoch in 1769659200 1770264000 1770868800 1771372800; do
    python -m data.fetchers.fetch_votes --epoch $epoch --vote-epoch $((epoch - 604800))
done

# Then analyze all at once (instant)
python analysis/my_analysis_script.py --epochs 1769659200,1770264000,1770868800,1771372800
```

## Database Schema

**Fetchers populate these tables:**

### `votes` table

```sql
CREATE TABLE votes (
    id INTEGER PRIMARY KEY,
    epoch INTEGER,              -- When snapshot taken
    gauge TEXT,                 -- Gauge address
    total_votes FLOAT,          -- Total votes for gauge
    indexed_at INTEGER          -- Unix timestamp when indexed
);
CREATE INDEX idx_votes_epoch_gauge ON votes(epoch, gauge);
```

### `bribes` table

```sql
CREATE TABLE bribes (
    id INTEGER PRIMARY KEY,
    epoch INTEGER,              -- When snapshot taken
    bribe_contract TEXT,        -- Bribe contract address
    reward_token TEXT,          -- Reward token address
    amount FLOAT,               -- Human-readable amount
    amount_wei TEXT,            -- Raw amount in wei
    timestamp INTEGER,          -- Event timestamp or fetch timestamp
    indexed_at INTEGER          -- When added to DB
);
CREATE INDEX idx_bribes_epoch_contract_token
ON bribes(epoch, bribe_contract, reward_token);
```

## Configuration

Fetchers read from `.env`:

```env
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY
DATABASE_PATH=data/data.db
VOTER_ADDRESS=0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b
```

## Troubleshooting

**"No gauges in database"**

- Run a gauge fetcher first (not provided yet, can use existing scripts)
- Or manually populate gauges table from known pool data

**"Could not fetch decimals/symbol"**

- Token might not have standard ERC20 interface
- Fetcher will fall back to default (18 decimals, short address)

**"Failed to find block at timestamp"**

- Block might be too far in past (blockchain pruning)
- Try providing `--block` explicitly if you know it

## Next Steps

1. **Processor scripts** for parsing event logs (currently fetchers query snapshot state)
2. **Gauge fetcher** to populate initial gauge data
3. **Historical storage** for ve state snapshots
4. **Cron jobs** to auto-fetch at epoch boundaries
