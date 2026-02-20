# Hydrex Optimiser - Repository Structure

This repo is organized into distinct phases:

```
hydrex-optimiser/
├── data/                      # Data pipeline (fetch, process, cache)
│   ├── fetchers/              # One-time scripts to fetch on-chain data
│   │   ├── __init__.py
│   │   ├── fetch_bribes.py    # Fetch bribe contract data
│   │   ├── fetch_votes.py     # Fetch voting data
│   │   └── fetch_ve_state.py  # Fetch ve NFT delegation state
│   ├── processors/            # Process and enrich fetched data
│   │   └── __init__.py
│   ├── __init__.py
│   ├── db/                   # SQLite databases (git-ignored)
│   │   └── data.db
│   └── .gitignore
│
├── analysis/                  # Analysis & optimization scripts
│   ├── __init__.py
│   ├── verify_historical_bribes.py
│   ├── analyze_boundary_maximum_return.py
│   └── analyze_last_vote_complete.py
│
├── src/                       # Shared libraries & utilities
│   ├── __init__.py
│   ├── contract_reward_calculator.py  # Contract-based reward calculation formulas
│   ├── database.py            # SQLAlchemy ORM models & low-level DB ops
│   ├── data_access.py         # High-level data access layer (queries for analysis)
│   ├── subgraph_client.py     # Subgraph queries
│   ├── price_feed.py          # Token price fetching
│   ├── token_utils.py         # Token symbol/decimal lookups
│   └── utils.py               # General utilities
│
├── config/                    # Configuration
│   ├── __init__.py
│   └── settings.py            # Centralized config (addresses, RPC, constants)
│
├── scripts/                   # Ad-hoc utilities
│   ├── __init__.py
│   └── query_recent_epoch.py  # Example utility script
│
├── .env                       # Environment variables (git-ignored)
├── .gitignore
├── README.md
└── requirements.txt
```

## Data Flow

### Phase 1: Data Fetching (One-time or periodic)

```
On-chain Contracts → data/fetchers/*.py → data/db/data.db
```

**Fetchers populate the database from on-chain sources:**

- `fetch_bribes.py` - Queries bribe contracts, caches reward totals
- `fetch_votes.py` - Queries VoterV5, stores vote distributions per epoch
- `fetch_ve_state.py` - Queries ve contract state, delegation snapshots

**Usage:**

```bash
python -m data.fetchers.fetch_bribes --epoch 1771372800
python -m data.fetchers.fetch_votes --epoch 1771372800
python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435
```

### Phase 2: Data Analysis (Use pre-fetched data)

```
data/db/data.db → analysis/*.py → Insights & Recommendations
```

**Analysis scripts read from the database and produce results:**

- `verify_historical_bribes.py` - Reconcile contract rewards vs actual payouts
- `analyze_boundary_maximum_return.py` - Find optimal vote allocation at epoch boundary
- `analyze_last_vote_complete.py` - Analyze past vote performance

**Usage:**

```bash
python analysis/verify_historical_bribes.py
python analysis/analyze_boundary_maximum_return.py --epoch 1771372800 --vote-epoch 1770854400
python analysis/analyze_last_vote_complete.py
```

## Key Modules

### `config/settings.py`

Centralized configuration:

- Contract addresses (VOTER_ADDRESS, VE_ADDRESS)
- RPC endpoints
- Database path
- Constants (ONE_E18, SCALE_32, WEEK)
- Known pool mappings
- Fallback pool share estimates

### `src/contract_reward_calculator.py`

Implements the contract-based reward formula shared by all analysis scripts:

- `VeDelegationSnapshot` - ve delegation state at epoch
- `BribeContractState` - Bribe contract snapshot at epoch
- `calculate_expected_reward()` - Core formula with fallback logic
- `ContractRewardCalculator` - Cached calculator for batch operations

**Formula:**

```
reward_per_token = (rewardsPerEpoch × 10^32) / totalSupply
reward = (reward_per_token × delegateeBalance) / 10^32
reward = (reward × weight) / 10^18

where weight = (your_power / delegatee_past_votes) × 10^18
```

### `src/database.py`

SQLAlchemy ORM models for all tables:

- `Epoch` - Epoch metadata
- `Gauge` - Gauge info (pool, bribe contracts)
- `Vote` - Votes per epoch/gauge
- `Bribe` - Rewards (RewardAdded events)
- `TokenMetadata` - Token info (symbol, decimals cache)
- `TokenPrice` - Price cache
- `HistoricalAnalysis` - Analysis results

**Database class:**

- Low-level operations: `save_bribe()`, `get_votes_for_epoch()`, `get_bribes_for_epoch()`

### `src/data_access.py`

High-level query layer for analysis scripts (built on top of `database.py`):

- `DataAccess` class wraps Database and provides convenient analysis methods
- `get_bribes_for_epoch_detailed()` - Get bribes with full token metadata
- `get_bribes_by_pool_and_type()` - Bribes grouped by pool and type (internal/external)
- `get_all_pools_in_epoch()` - List all pools with activity
- `save_bribe_with_metadata()` - Save bribe + token metadata atomically

**Usage in analysis scripts:**

```python
from src.database import Database
from src.data_access import DataAccess

db = Database("data/db/data.db")
da = DataAccess(db)

summary = da.get_bribes_for_epoch_detailed(epoch=1771372800)
for bribe in summary.bribes:
    print(f"{bribe.pool_name}: {bribe.amount_human} {bribe.token_symbol}")
```

## Usage Examples

### Scenario: Analyze rewards for next epoch

1. **Fetch data (once at epoch boundary):**

   ```bash
   python -m data.fetchers.fetch_bribes --epoch 1771372800
   python -m data.fetchers.fetch_votes --epoch 1771372800
   python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435
   ```

2. **Analyze (multiple times, instantly):**
   ```bash
   python analysis/verify_historical_bribes.py
   python analysis/analyze_boundary_maximum_return.py --epoch 1771372800 --vote-epoch 1770854400 --k 5
   ```

### Scenario: Debug bribe mismatch

```python
from src.database import Database
from src.data_access import DataAccess

db = Database("data/db/data.db")
da = DataAccess(db)

# Get all bribes for epoch
summary = da.get_bribes_for_epoch_detailed(1771372800)
print(f"Total bribes: {summary.total_bribes_amount} USD")

# Get bribes for specific pool
bribes_by_type = da.get_bribes_by_pool_and_type(1771372800, "HYDX/USDC")
for bribe in bribes_by_type["internal"]:
    print(f"  Internal: {bribe.amount_human} {bribe.token_symbol}")
```

## Environment Variables (.env)

```env
# Contracts
VOTER_ADDRESS=0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b
VE_ADDRESS=0x25B2ED7149fb8A05f6eF9407d9c8F878f59cd1e1

# User
YOUR_ADDRESS=0x768a675B8542F23C428C6672738E380176E7635C
YOUR_TOKEN_ID=19435

# RPC
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY

# Database
DATABASE_PATH=data/db/data.db
```

## Next Steps

1. **Migrate existing fetchers** to `data/fetchers/`
2. **Update analysis scripts** to use `DataAccess` instead of raw SQL
3. **Create fetcher scripts** for any missing data sources
4. **Add more `DataAccess` methods** as needed for new analysis tasks
