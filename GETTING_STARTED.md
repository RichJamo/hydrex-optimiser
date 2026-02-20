# Getting Started with New Architecture

This guide walks you through using the refactored Hydrex Optimiser with the new fetch-once, analyze-many architecture.

## Quick Start (5 minutes)

### 1. Verify setup

```bash
cd /Users/richardjamieson/Documents/GitHub/hydrex-optimiser
source venv/bin/activate

# Check imports work
python -c "from config.settings import VOTER_ADDRESS; print('✅ Setup OK')"
```

### 2. Initialize database

```bash
# Creates all tables if they don't exist
python -c "
from src.database import Database
db = Database('data/db/data.db')
db.create_tables()
print('✅ Database initialized')
"
```

### 3. Fetch data for an epoch (one-time, ~1-2 min)

```bash
# Fetch votes at epoch
python -m data.fetchers.fetch_votes \
    --epoch 1771372800 \
    --vote-epoch 1770854400

# Fetch ve state (for reference)
python -m data.fetchers.fetch_ve_state \
    --epoch 1771372800 \
    --token-id 19435
```

### 4. Run analysis (instant, uses cached data)

```bash
python analysis/verify_historical_bribes.py
```

## Detailed Guide

### Understanding the New Structure

**Before (Old):**

```
verify_historical_bribes.py (root)
  ├─ Queries contracts on-demand
  ├─ Computes calculations inline
  ├─ Slow for repeated runs
  └─ Hard to reuse code
```

**After (New):**

```
data/fetchers/fetch_votes.py
  │  (fetch once, cache in DB)
  ├─ data/db/data.db
data/fetchers/fetch_bribes.py
  │  (fetch once, cache in DB)
  └─ data/db/data.db
        ↓
analysis/verify_historical_bribes.py
  │  (read from DB, instant)
  ├─ Uses: src/data_access.py
  ├─ Uses: src/contract_reward_calculator.py
  └─ Uses: config/settings.py
```

### Key Modules

#### `config/settings.py` - Configuration

Centralized settings. No more scattered `os.getenv()` calls.

```python
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"  # From .env or default
LEGACY_POOL_SHARES = {"HYDX/USDC": 0.085994, ...}  # Fallback estimates
```

#### `src/database.py` - ORM Models

Low-level database access (SQLAlchemy models).

```python
db = Database("data/db/data.db")
votes = db.get_votes_for_epoch(1771372800)
bribes = db.get_bribes_for_epoch(1771372800)
```

#### `src/data_access.py` - High-Level Queries

Convenient queries for analysis scripts.

```python
da = DataAccess(db)
bribes_with_metadata = da.get_bribes_for_epoch_detailed(1771372800)
# Returns: BribeDetails with token symbols, decimals, human amounts, etc.
```

#### `src/contract_reward_calculator.py` - Calculation Engine

Contract-based reward formula (shared by all scripts).

```python
from src.contract_reward_calculator import ContractRewardCalculator

calc = ContractRewardCalculator(w3, ve_contract)
reward = calc.calculate_reward(
    token_id=19435,
    calc_epoch=1770854400,
    bribe_contract=bribe_instance,
    token_address=token_addr,
    token_decimals=18,
)
```

### Common Tasks

#### Task: Run bribe verification

```bash
python analysis/verify_historical_bribes.py
```

**What it does:**

- Reads bribes from DB (cached earlier)
- Calculates expected reward using contract formula
- Compares against actual payouts
- Shows color-coded results

**Requires:**

- `.env` with YOUR_ADDRESS, YOUR_TOKEN_ID
- Data fetched earlier with fetch_votes.py

#### Task: Analyze votes for an epoch

```bash
python -c "
from src.database import Database
from src.data_access import DataAccess

db = Database('data/db/data.db')
da = DataAccess(db)

votes = db.get_votes_for_epoch(1771372800)
print(f'Total votes fetched: {len(votes)}')

for vote in votes[:5]:
    print(f'  Gauge {vote.gauge[:10]}...: {vote.total_votes} votes')
"
```

#### Task: Check bribe data

```bash
python -c "
from src.database import Database
from src.data_access import DataAccess

db = Database('data/db/data.db')
da = DataAccess(db)

summary = da.get_bribes_for_epoch_detailed(1771372800)
print(f'Epoch: {summary.epoch}')
print(f'Total bribes: {summary.bribe_count}')
print(f'Total amount (USD equivalent): \${summary.total_bribes_amount:,.2f}')
print(f'Unique tokens: {summary.unique_tokens}')

for bribe in summary.bribes[:5]:
    print(f'  {bribe.pool_name}/{bribe.bribe_type}: {bribe.amount_human} {bribe.token_symbol}')
"
```

#### Task: Query ve state

```bash
python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435
```

**Output:**

```
VE Delegation Snapshot
  Epoch: 1771372800 (calc_epoch: 1770854400)
  Token ID: 19435
  Delegatee: 0x768a675B8542F23C428C6672738E380176E7635C
  Power: 1183272000000000000000000
  Delegatee Past Votes: 1234567890000000000000000
  Weight (1e18): 958849...
```

### Environment Setup

Create/update `.env` with:

```env
# Contracts (usually don't need to change)
VOTER_ADDRESS=0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b
VE_ADDRESS=0x25B2ED7149fb8A05f6eF9407d9c8F878f59cd1e1

# Your info (set these)
YOUR_ADDRESS=0x768a675B8542F23C428C6672738E380176E7635C
YOUR_TOKEN_ID=19435

# RPC endpoint (required)
RPC_URL=https://base-mainnet.g.alchemy.com/v2/YOUR_KEY

# Data location (optional)
DATABASE_PATH=data/db/data.db
```

### Troubleshooting

**"No bribe data in database for this epoch"**

```bash
# Step 1: Check if bribes were fetched
sqlite3 data/db/data.db "SELECT COUNT(*) FROM bribes WHERE epoch=1771372800;"

# If 0, fetch them:
# python -m data.fetchers.fetch_bribes --epoch 1771372800
```

**"YOUR_ADDRESS owns N NFTs; set YOUR_TOKEN_ID in .env"**

```bash
# You own multiple ve NFTs. Edit .env and set:
YOUR_TOKEN_ID=19435
```

**Import errors after changes**

```bash
# Make sure venv is activated
source venv/bin/activate

# Clear cached imports
find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete

# Try again
python analysis/verify_historical_bribes.py
```

**RPC connection issues**

```bash
# Verify RPC_URL in .env
python -c "
from web3 import Web3
import os
RPC_URL = os.getenv('RPC_URL')
w3 = Web3(Web3.HTTPProvider(RPC_URL))
print(f'Connected: {w3.is_connected()}')
print(f'Block: {w3.eth.block_number}')
"
```

## Architecture Decisions

### Why Fetch Once?

- ✅ Queries are expensive (RPC rate limiting, slow historical queries)
- ✅ Analysis scripts often re-run with same data
- ✅ Enables offline analysis (plane mode!)
- ✅ Easy to share cached data

### Why Cached Calculation Module?

- ✅ Contract formula same for all scripts (DRY)
- ✅ Easy to verify against ground truth (payouts)
- ✅ Supports pre-flip estimates with fallback logic
- ✅ Flexible for different calculation scenarios

### Why DataAccess Layer?

- ✅ SQL queries hidden behind Python methods
- ✅ Automatic metadata enrichment (token symbols, etc.)
- ✅ Easy to add new query patterns
- ✅ Analysis scripts focus on logic, not queries

## Next Steps

1. **Run analyze_boundary_maximum_return with new calculator** (in progress)
   - More accurate than simple pro-rata
   - Requires YOUR_ADDRESS + YOUR_TOKEN_ID

2. **Create cron jobs** for automatic epoch-boundary fetching

   ```bash
   # At 00:00 UTC every Tuesday:
   0 0 * * 2 /path/to/hydrex/fetch_epoch_data.sh
   ```

3. **Add more fetchers** as needed
   - Event log indexing for accurate RewardAdded parsing
   - Gauge discovery/updates
   - Historical ve state snapshots

4. **Build custom analysis** using DataAccess
   - Find best pools by historical returns
   - Analyze gas costs vs bribes
   - Portfolio rebalancing suggestions

## References

- [ARCHITECTURE.md](../ARCHITECTURE.md) - System design
- [MIGRATION_GUIDE.md](../MIGRATION_GUIDE.md) - Integration details
- [data/fetchers/README.md](data/fetchers/README.md) - Fetcher documentation
