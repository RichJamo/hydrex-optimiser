# Migration Guide: Integrating Contract Reward Calculator

## Current State

The existing `analyze_boundary_maximum_return.py` script works by:

1. Querying on-chain contracts directly (weightsAt, rewardData)
2. Computing rewards as simple pro-rata: `reward = total_usd * (your_votes / (boundary_votes + your_votes))`
3. Running this each time (slow for repeated analysis)

## New Architecture

Three tiers of data abstraction:

```
Tier 1: On-chain contracts (web3.py)
   ↓
Tier 2: src/contract_reward_calculator.py ← Contract formula implementation
   ↓
Tier 3: src/data_access.py ← High-level queries with caching
   ↓
Tier 4: analysis/*.py ← Analysis scripts (read-only)
```

## Migration Strategy

### Phase 1: ✅ Complete (Current)

- Created `src/contract_reward_calculator.py` with contract formula
- Created `src/data_access.py` with high-level queries
- Refactored `analysis/verify_historical_bribes.py` to use new modules
- Created `config/settings.py` for centralized configuration

### Phase 2: Pending (Data Fetching)

Create `data/fetchers/*.py` scripts to populate database:

- `fetch_bribes.py` → Run once per epoch, stores bribe snapshot in DB
- `fetch_votes.py` → Run once per epoch, stores vote snapshot in DB
- `fetch_ve_state.py` → Run optionally, caches ve delegation state

### Phase 3: Pending (Refactor analyze_boundary_maximum_return.py)

The current script uses pro-rata calculation:

```python
def expected_return(total_usd, base_votes, your_votes):
    return total_usd * (your_votes / (base_votes + your_votes))
```

The contract-based method requires:

```python
calculator = ContractRewardCalculator(w3, ve_contract)
reward = calculator.calculate_reward(
    token_id=token_id,
    calc_epoch=calc_epoch,
    bribe_contract=bribe_contract,
    token_address=token_address,
    token_decimals=token_decimals,
)
```

**Key difference:** Contract method accounts for:

- ✅ Delegation weight (power / delegatee_votes)
- ✅ Delegatee's pool balance share
- ✅ Actual bribe snapshot (not just votes)

**Challenge:** The refactor requires:

- User must provide: YOUR_ADDRESS, YOUR_TOKEN_ID (for ve lookup)
- Must query ve contract state for each run
- More complex but more accurate

### How to Integrate Contract Calculator

For any analysis script that calculates expected rewards:

```python
from src.contract_reward_calculator import ContractRewardCalculator
from config.settings import LEGACY_POOL_SHARES

# Setup
calculator = ContractRewardCalculator(w3, ve_contract)

# For each bribe contract + token
reward = calculator.calculate_reward(
    token_id=token_id,
    calc_epoch=calc_epoch,  # WEEK-aligned epoch
    bribe_contract=bribe_contract_instance,
    token_address=reward_token_address,
    token_decimals=token_decimals,
    fallback_db_amount=total_amount_from_db,  # If rewardData is zero
    legacy_pool_share=LEGACY_POOL_SHARES.get(pool_name),  # Fallback if contract data sparse
    block_identifier=block_number,  # Optional: for pre-flip estimates
)

# If need USD value, multiply by price
reward_usd = reward * price_per_token
```

## Next Steps

1. **Create fetcher scripts** in `data/fetchers/`:

   ```bash
   python -m data.fetchers.fetch_bribes --epoch 1771372800
   python -m data.fetchers.fetch_votes --epoch 1771372800
   python -m data.fetchers.fetch_ve_state --epoch 1771372800 --token-id 19435
   ```

2. **Verify database is populated**:

   ```bash
   sqlite3 data/db/data.db "SELECT COUNT(*) FROM bribes WHERE epoch=1771372800;"
   ```

3. **Then analysis scripts use**:

   ```python
   from src.data_access import DataAccess
   da = DataAccess(db)
   bribes = da.get_bribes_for_epoch_detailed(1771372800)
   ```

4. **For reward calculations, use**:
   ```python
   from src.contract_reward_calculator import ContractRewardCalculator
   calc = ContractRewardCalculator(w3, ve_contract)
   reward = calc.calculate_reward(...)
   ```

## File Organization

**Old style (query on-demand, compute each time):**

- `verify_historical_bribes.py` (root)
- `analyze_boundary_maximum_return.py` (root)

**New style (fetch once, analyze many times):**

- `data/fetchers/fetch_bribes.py` ← Run once at epoch
- `data/fetchers/fetch_votes.py` ← Run once at epoch
- `analysis/verify_historical_bribes.py` ← Refactored to use modules
- `analysis/analyze_boundary_maximum_return.py` ← Pending refactor
- `data/db/data.db` ← Cached data (git-ignored)

## Example: Full Flow

```bash
# 1. Fetch data once (at epoch boundary)
python -m data.fetchers.fetch_bribes --epoch 1771372800 --current-block 42291740
python -m data.fetchers.fetch_votes --epoch 1771372800 --current-block 42291740

# 2. Analyze whenever needed (instant, uses cached data)
python analysis/verify_historical_bribes.py
python analysis/analyze_boundary_maximum_return.py --epoch 1771372800 --k 5

# 3. If you need to re-fetch (data changed)
python -m data.fetchers.fetch_bribes --epoch 1771372800 --no-cache  # Bypass cache
```
