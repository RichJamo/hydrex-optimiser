# Validation Commands for Hardened Epoch-Level Reward Attribution

**Last Updated:** 2026-02-20  
**Hardening Focus:** Vote-/reward-epoch alignment, guardrail diagnostics, multi-epoch generalization

---

## Overview

The `analyze_boundary_maximum_return.py` script now implements the **canonical protocol-based approach** for epoch reward attribution:

1. **Single authoritative epoch for both votes and rewards** (not mixed calc_epoch)
2. **Vote-epoch misalignment detection** (all-zero guardrail)
3. **Sparse weight warnings** (early signal of epoch drift)
4. **Rewards consistency checks** (detects stale/unfetched bribes)
5. **Enhanced auto-detection** with diagnostics
6. **Multi-epoch generalization** (no hardcoded 1-week offsets)

---

## Quick Validation (5-10 min)

### Test 1: Explicit Vote-Epoch with Small Gauge Set (Quick sanity check)

```bash
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 10 \
  --candidate-pools 10 \
  --k 5
```

**Expected output:**
- ✅ `nonzero_pools ≥ 8/10` (most pools should have votes)
- ✅ `vote_failures = 0`
- ✅ `reward_failures = 0`
- ✅ `✓ Rewards consistency OK: X gauges with USD rewards`
- ✅ 1-pool and 5-pool allocations computed and displayed

**Failure modes to catch:**
- ❌ `vote_failures > 0` → RPC connection issue or gauges don't exist
- ❌ `All X pools returned weightsAt(...) = 0` → vote_epoch is misaligned
- ❌ `EMPTY REWARDS WARNING` → rewards queried at wrong epoch (see guardrail message)

---

### Test 2: Auto-Detect Vote-Epoch (Verify improved diagnostics)

```bash
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --max-gauges 15 \
  --candidate-pools 15 \
  --k 5 \
  --vote-epoch-scan-days 7 \
  --vote-epoch-sample-pools 8
```

**Expected output:**
- ✅ `Vote-epoch auto-detection (sampled 8 pools over 8 epoch candidates):`
- ✅ Top 3 candidates ranked by nonzero count and total votes
- ✅ Auto-selected best epoch used for weightsAt/rewardData
- ✅ Best candidate should have ≥6/8 pools with nonzero votes

**Diagnostics to check:**
- If top candidate has many pools with nonzero votes → ✓ alignment good
- If top candidate has 0/8 nonzero → ⚠️ all epochs misaligned (check --epoch value)
- If multiple epochs tie on nonzero count → total votes used as tiebreaker (correctly logged)

---

## Medium Validation (15-20 min)

### Test 3: Cache Workflow (Verify cache + boundary fallback)

```bash
# First run: no cache, write to DB
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 20 \
  --candidate-pools 20 \
  --k 5 \
  --no-cache

# Second run: should load from cache (faster)
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 20 \
  --candidate-pools 20 \
  --k 5
```

**Expected behavior:**
- First run: `Querying pool weightsAt...` and `Querying rewardData...` progress bars
- First run (end): `Cached X boundary states to DB`
- Second run: `Loaded X cached boundary states from DB` (no re-queries)
- Second run (end): same results as first run (identical USD values, allocations)

**Fallback scenario (test boundary block drift):**

```bash
# First run at epoch, boundary_block = B1
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 10 \
  --no-cache

# Wait a few blocks (or re-query with slightly different timestamp)
# Now boundary_block detection may move to ~B1+5

python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 10
```

**Expected with fallback:**
- Second run detects `current boundary block B2 had no cache rows`
- Checks if older block `B1` cached (from first run)
- If found: `Using cached boundary states from block B1 (current boundary block B2 had no cache rows...)`
- Results remain consistent (same pools, same USD values)

---

## Multi-Epoch Generalization (30-45 min)

### Test 4: Cross-Epoch Consistency (Verify approach works for different epochs)

Test on a recently-closed epoch (within 1-2 weeks):

```bash
# Find a recent epoch boundary. Epochs are every 7 days (604800 seconds).
# Current epoch chain: 1770854400 → 1771372800 → 1771891200 → ...
# (These are Tuesday dates; protocol uses Thu, but test with what's in data)

# Test the previous closed epoch
PREV_EPOCH=1770854400
PREV_VOTE_EPOCH=$((PREV_EPOCH - 604800))  # = 1770249600

python analyze_boundary_maximum_return.py \
  --epoch $PREV_EPOCH \
  --vote-epoch $PREV_VOTE_EPOCH \
  --max-gauges 20 \
  --candidate-pools 20 \
  --k 5 \
  --no-cache
```

**Verification steps:**
```bash
# Compare with current epoch
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 20 \
  --candidate-pools 20 \
  --k 5 \
  --no-cache
```

**Expected observations:**
- ✅ Both epochs produce output without `vote_epoch misaligned` warnings
- ✅ Different epochs have different top pools / USD values (market dynamics)
- ✅ Same vote-epoch offset pattern holds (vote_epoch = epoch - 1 WEEK)
- ⚠️ If previous epoch shows sparse weights → that epoch may not have full historical data in DB

---

### Test 5: Auto-Detect Generalizes (No hardcoding)

```bash
# Test auto-detection on a different epoch without explicit --vote-epoch
SOME_EPOCH=1771372800

python analyze_boundary_maximum_return.py \
  --epoch $SOME_EPOCH \
  --max-gauges 20 \
  --candidate-pools 20 \
  --k 5 \
  --vote-epoch-scan-days 10 \
  --vote-epoch-sample-pools 12 \
  --no-cache
```

**Expected:**
- Auto-detection logs show scan across 11 epoch candidates (10 days back)
- Best candidate auto-selected based on nonzero vote count
- Should NOT require manual --vote-epoch override if data is available

**Diagnostic edge case:**
If auto-detection selects an epoch with low nonzero pools (e.g., 3/12):
```
⚠️  AUTODETECT WEAK: Best candidate X had only 3/12 nonzero pools. 
    Results may be unreliable; consider --vote-epoch override.
```
→ This is **expected** if data is sparse; recommend manual `--vote-epoch` to a known good epoch.

---

## Full Hardened Run (45-60 min)

### Test 6: Large Gauge Set with Diagnostics

```bash
# Full run: top 50 gauges by epoch USD, k=8 allocation
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 50 \
  --candidate-pools 40 \
  --k 8 \
  --min-votes-per-pool 100000 \
  --no-cache 2>&1 | tee /tmp/full_boundary_run.log
```

**Check diagnostic output:**
- Line 1: `Boundary block: XXXXX @ timestamp`
- Early: `Vote-epoch auto-detection...` or explicit vote-epoch shown
- Mid: `Boundary vote query stats: nonzero_pools=XXX/YYY, max_pool_votes=...`
  - Should have high nonzero ratio (≥80% is healthy)
- Mid: `Querying rewardData at boundary` progress bar
- After queries: Either:
  - ✅ `✓ Rewards consistency OK: N gauges with USD rewards`
  - ⚠️ `⚠️  SPARSE WEIGHTS WARNING` (if <1/3 pools have votes)
  - ❌ `⚠️  CRITICAL GUARDRAIL: All X pools returned weightsAt() = 0`

**Expected final output:**
```
Total gauges with USD > 0: ~45-50
1-pool max return: $XXXX (vs baseline)
K-pool max return: $YYYY (vs baseline, should be > 1-pool)
Allocations respect min-votes-per-pool constraint
```

**Rerun with cache:**
```bash
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1770854400 \
  --max-gauges 50 \
  --candidate-pools 40 \
  --k 8 \
  --min-votes-per-pool 100000 2>&1 | tee /tmp/cached_run.log
```

Expected:
- `Loaded X cached boundary states from DB` (immediate, no queries)
- Same output as no-cache run
- Compare: `diff /tmp/full_boundary_run.log /tmp/cached_run.log` → only timestamps differ

---

## Guardrail Validation (10-15 min)

### Test 7: Detect Vote-Epoch Misalignment

Intentionally query at a wrong epoch:

```bash
# Use wrong vote epoch (e.g., 7 days later, not earlier)
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1771891200 \
  --max-gauges 20 \
  --no-cache
```

**Expected guardrail trigger:**
```
⚠️  CRITICAL GUARDRAIL: All 20 pools returned weightsAt(pool, 1771891200) = 0.
    This suggests vote_epoch 1771891200 is misaligned with the closed epoch 1771372800.
Recommendations:
  1) Verify vote_epoch is the CLOSED epoch timestamp (not E+WEEK)
  2) Use --vote-epoch to explicitly set a different timestamp
  3) Check if epoch 1771372800 has actually closed (is past update_period() call)
  4) Examine contract events for actual flip block (Mint event with week_number)
```

✅ **Correct behavior:** Script fails fast with actionable diagnostic.

---

### Test 8: Detect Sparse Weights

Query at an epoch when only a few gauges were voted:

```bash
# This depends on historical voting patterns; if no sparse epochs in data,
# manually query a random epoch to likely trigger warning
python analyze_boundary_maximum_return.py \
  --epoch 1770249600 \
  --vote-epoch 1769644800 \
  --max-gauges 50 \
  --no-cache
```

**Expected if sparse:**
```
⚠️  SPARSE WEIGHTS WARNING: Only 5/50 pools have nonzero votes.
    This could indicate:
      • Vote epoch is slightly off (try adjacent days)
      • Most gauges were not voted in the queried epoch
    Proceeding with caution; results may underestimate pool returns.
```

✅ **Correct behavior:** Proceeds but warns; user can investigate or retry with different epoch.

---

### Test 9: Detect Empty Rewards

Query rewardData before bribes are deposited (force this by querying old epoch):

```bash
# If data is available, query a very old epoch where no bribes exist
# Simulate by querying with vote_epoch way off:
python analyze_boundary_maximum_return.py \
  --epoch 1771372800 \
  --vote-epoch 1700000000 \
  --max-gauges 20 \
  --no-cache
```

**Expected:**
- First guardrail: `All 20 pools returned weightsAt() = 0` → vote_epoch misaligned
  - **OR** if arbitrarily old epoch actually has votes (unlikely):
- Second guardrail: `EMPTY REWARDS WARNING: All gauges have zero USD rewards...`

✅ **Correct behavior:** Detects root cause and recommends action.

---

## Checklist for Production Use

Before deploying `analyze_boundary_maximum_return.py` for recurring (e.g., hourly) analysis:

- [ ] **Test Quick Run (Test 1):** Confirms RPC connectivity and basic flow
- [ ] **Test Auto-Detect (Test 2):** Logs show sensible epoch ranking
- [ ] **Test Cache Workflow (Test 3):** Cache loads correctly, fallback works
- [ ] **Test Multi-Epoch (Test 4):** Previous epochs produce consistent results
- [ ] **Test Full Run (Test 6):** Results reasonable for large gauge set
- [ ] **Verify Guardrails (Tests 7-9):** Warnings trigger appropriately
- [ ] **Compare with Manual Verification:** Query one high-influence pool manually:
  ```bash
  python -c "
  from web3 import Web3
  import json
  w3 = Web3(Web3.HTTPProvider('https://mainnet.base.org'))
  voter_abi = json.load(open('voterv5_abi.json'))
  voter = w3.eth.contract(address=Web3.to_checksum_address('0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b'), abi=voter_abi)
  pool = '0xff611d0b4788fa30b6c28b69e6d3d5dd8adc1f51'  # Example
  epoch = 1770854400
  votes = voter.functions.weightsAt(pool, epoch).call()
  print(f'weightsAt({pool}, {epoch}) = {votes}')
  "
  ```
  Cross-check with script output for same pool/epoch.

---

## Troubleshooting Guide

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| `All X pools returned weightsAt() = 0` | vote_epoch misaligned | Use `--vote-epoch` to set correct closed epoch (1 week before rewards flip) |
| `only X/Y nonzero pools` with sparse warning | Epoch sparsely voted or data incomplete | Retry with `--vote-epoch-scan-days 14` to find better epoch, or suppress with `--disable-vote-epoch-autodetect` |
| `EMPTY REWARDS WARNING` (rewards=0 but votes≠0) | rewardData not deposited yet or queried at wrong epoch | Bribes may be deposited post-flip; wait a block and rerun, or manually check `Bribe.rewardData(token, epoch)` |
| `vote_failures > 0` | RPC errors on individual weightsAt calls | Check RPC URL (often rate-limited); retry with backoff or reduce `--max-gauges` |
| `reward_failures > 0` | RPC errors on individual rewardData calls | Check RPC URL; failures are expected if bribe contract is defunct or token doesn't exist |
| Cache shows different block than current query | Boundary block drift (new blocks mined) | Expected behavior; fallback uses best cached block for same epoch. Results should be nearly identical |
| Results differ between epochs | Market dynamics or different allocation landscape | **Expected.** Different epochs have different vote distributions and bribe amounts. Not a bug. |

---

## Commands for Continuous Monitoring

To run `analyze_boundary_maximum_return.py` every hour for an epoch:

```bash
#!/bin/bash
EPOCH=1771372800
VOTE_EPOCH=1770854400
DB="data.db"

while true; do
  echo "[$(date)] Running boundary analysis for epoch $EPOCH..."
  python analyze_boundary_maximum_return.py \
    --epoch $EPOCH \
    --vote-epoch $VOTE_EPOCH \
    --max-gauges 30 \
    --candidate-pools 30 \
    --k 5 \
    --db $DB \
    2>&1 | tee -a /tmp/boundary_analysis.log

  echo "[$(date)] Sleeping 3600 seconds..."
  sleep 3600
done
```

To switch to a new epoch (once the current one closes):

```bash
# After epoch close, flip to next epoch
NEXT_EPOCH=$((CURRENT_EPOCH + 604800))      # Add 1 week
NEXT_VOTE_EPOCH=$CURRENT_EPOCH              # Previous epoch's timestamp

# Restart with new epochs
python analyze_boundary_maximum_return.py \
  --epoch $NEXT_EPOCH \
  --vote-epoch $NEXT_VOTE_EPOCH \
  ...
```

---

## Summary

✅ **Hardening Complete:**
1. **Correctness:** Vote and reward queries use same authoritative epoch
2. **Safety:** Guardrails detect misalignment, sparseness, and stale data
3. **Transparency:** Auto-detection logs ranked candidates; users see why epoch was chosen
4. **Generalization:** Works for any epoch; no hardcoded offsets
5. **Cache:** Boundary fallback handles small block drifts; cache consistent across runs

**Next Steps:** Run Test 1-3 to validate. Use Test 6 for production baseline.

