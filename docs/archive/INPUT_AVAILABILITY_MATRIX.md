# Input Availability Matrix - Precise Breakdown by Column & Boundary

## Overview

This document explains which inputs are used by each estimator column (Expected, T-5 Est., T-1 Est., Actual %) and clarifies which inputs are pre-boundary (may change) vs post-boundary (final).

---

## Column Input Breakdown

### **Expected (Final Contract State at Epoch Flip)**

**Block Timestamp:** CLOSED_EPOCH (Feb 19, 2026 00:00:00 UTC)  
**Status:** ✓ Post-boundary (final, locked at epoch flip)

#### Inputs Used:

| Input                                     | Source                                                    | Availability | Notes                                          |
| ----------------------------------------- | --------------------------------------------------------- | ------------ | ---------------------------------------------- |
| `rewards_per_epoch_raw`                   | `rewardData(token, calc_epoch)` @ epoch_block             | ✓ Finalized  | Distribution completed, immutable              |
| `totalSupplyAt(calc_epoch)`               | Bribe contract checkpoint @ epoch_block                   | ✓ Finalized  | Voting power snapshot at exact epoch timestamp |
| `balanceOfOwnerAt(delegatee, calc_epoch)` | Bribe contract @ epoch_block                              | ✓ Finalized  | Your pool share locked at epoch                |
| `delegatee`                               | `ve.delegates(tokenId, aligned_epoch)` @ epoch_block      | ✓ Finalized  | Vote delegation state at epoch                 |
| `power`                                   | `ve.balanceOfNFTAt(tokenId, aligned_epoch)` @ epoch_block | ✓ Finalized  | Your voting power at epoch                     |
| `delegatee_past_votes`                    | `ve.getPastVotes(delegatee, aligned_epoch)` @ epoch_block | ✓ Finalized  | Total delegatee voting power at epoch          |
| `weight`                                  | Calculated as `(power × 1e18) / delegatee_past_votes`     | ✓ Finalized  | Weight factor derived from above               |

#### Calculation:

```
reward_per_token = (rewards_per_epoch_raw × SCALE_32) / totalSupplyAt
manual_epoch_reward = (reward_per_token × balanceOfOwnerAt) / SCALE_32
manually_epoch_reward = (manual_epoch_reward × weight) / 1e18
expected_reward = manual_epoch_reward / 10^decimals
```

#### Result:

✓ **This is the _true, authoritative_ reward at the exact moment of epoch flip.**

---

### **T-5 Est. (5 Seconds Before Epoch - Hybrid Pre-Flip)**

**Block Timestamp:** CLOSED_EPOCH - 300 seconds  
**Status:** ✗ Pre-boundary (may change over next 5 seconds)

#### Inputs Used (with fallback):

| Input                                        | Source                                                 | Availability            | Boundary Behavior                        |
| -------------------------------------------- | ------------------------------------------------------ | ----------------------- | ---------------------------------------- |
| `rewards_per_epoch_raw`                      | `rewardData(token, calc_epoch)` @ t5_block             | ✗ May be zero           | If 0: **fallback to DB `total_amount`**  |
| `totalSupplyAt(calc_epoch)`                  | Bribe contract @ t5_block                              | ✗ May not reflect final | Used only if > 0                         |
| `balanceOfOwnerAt(delegatee_t5, calc_epoch)` | Bribe contract @ t5_block                              | ✗ May be zero           | If zero or missing: **trigger fallback** |
| `delegatee`                                  | `ve.delegates(tokenId, aligned_epoch)` @ t5_block      | ✗ May change in next 5s | Could be different from final            |
| `power`                                      | `ve.balanceOfNFTAt(tokenId, aligned_epoch)` @ t5_block | ✗ May change in next 5s | Could be different from final            |
| `delegatee_past_votes`                       | `ve.getPastVotes(delegatee, aligned_epoch)` @ t5_block | ✗ May change            | Could be different from final            |
| `weight`                                     | Calculated as above                                    | ✗ May change            | Depends on voting state changes          |

#### T-5 Logic (Hybrid with Fallback):

```python
rewards_baseline_raw = rewards_per_epoch_raw_t5
if rewards_baseline_raw == 0:
    rewards_baseline_raw = DB.total_amount × 10^decimals  # FALLBACK 1
    estimator_source = "Fallback (DB)"
else:
    estimator_source = "Contract"

contract_share_available = (
    totalSupplyAt_t5 > 0 AND
    balanceOfOwnerAt_t5 > 0 AND
    weight_t5 > 0
)

if contract_share_available:
    preflip_estimated = (rewards_baseline × balanceOfOwnerAt_t5) / totalSupplyAt_t5
    preflip_estimated = (preflip_estimated × weight_t5) / 1e18
else:
    preflip_estimated = DB.total_amount × LEGACY_POOL_SHARES[pool]  # FALLBACK 2
    estimator_source = "Fallback (legacy share)"
```

#### Result:

⚠️ **Best guess 5 seconds before, may diverge up to reward flip if:**

- More claims arrive
- Voting power changes
- Delegations change
- Contract snapshots update

---

### **T-1 Est. (1 Second Before Epoch - Post-Flip Stabilization Check)**

**Block Timestamp:** CLOSED_EPOCH - 60 seconds  
**Status:** ✗ Pre-boundary (just 60s before, near-final)

#### Same Logic as T-5 Est., but at t1_block timestamp

| Aspect              | T-5   | T-1    | Expected   |
| ------------------- | ----- | ------ | ---------- |
| Seconds to boundary | 300   | 60     | 0 (locked) |
| Contract stability  | Low   | High   | 100%       |
| Likely identical?   | Maybe | Likely | -          |

#### Key Question:

**If T-1 ≈ Expected (≥99%), then:**

- ✓ Contract state was stable for at least 60 seconds
- ✓ No major reward updates or voting changes in final minute
- ✓ Our estimate was _already accurate_ 60 seconds before

**If T-1 ≠ Expected (< 99%), then:**

- ⚠️ Contract state or voting power changed in the final 60 seconds
- ⚠️ Something significant happened at flip time (new bribes, delegations, etc.)

---

### **Actual % (Actual / Expected × 100)**

**Source:** Your reported received amount from ACTUAL_RECEIVED dict  
**Compares Against:** Expected reward

| Range       | Color     | Meaning                                      |
| ----------- | --------- | -------------------------------------------- |
| 99.5–100.5% | 🟢 Green  | Perfect match (dust-safe)                    |
| > 100.5%    | 🔵 Cyan   | Over-distributed (rare)                      |
| 90–99.5%    | 🟡 Yellow | Minor shortfall (possible dust)              |
| < 90%       | 🔴 Red    | Major shortfall (bug or missed distribution) |

---

## Why T-5 Est. ≡ T-1 Est.? (Identical)

### **Case 1: Contract Inputs Unchanged**

```
If rewards_per_epoch_raw_t5 == rewards_per_epoch_raw_t1
   AND totalSupplyAt_t5 == totalSupplyAt_t1
   AND balanceOfOwnerAt_t5 == balanceOfOwnerAt_t1
   AND weight_t5 == weight_t1

→ Mathematically: T-5 = T-1 (identical inputs, identical formula)
```

**Why this happens:**

- Bribe reward distribution completed hours before epoch
- Voting power snapshot never changed between T-5 and T-1
- No delegations or power changes in that window
- Contract state is already "settled"

### **Case 2: Both Using Fallback (Legacy Share)**

```
If rewardData = 0 at both T-5 and T-1:
   T-5 est = total_amount × LEGACY_POOL_SHARES[pool]
   T-1 est = total_amount × LEGACY_POOL_SHARES[pool]

→ By definition: T-5 = T-1 (same formula, same constant)
```

**Why this happens:**

- No contract state pre-epoch (this token/pool had no prior claims)
- System falls back to constant legacy distribution ratio
- Both times compute identical result

### **Case 3: Both Using Contract, But Different Inputs Compensate**

```
Rare: T-5 might have partial data that T-1 "fills in",
but the weighted result ends up equal.
```

---

## Input Availability Lifecycle (Key Timeline)

### **T-5 (300s before epoch flip)**

```
Pre-Boundary Phase:
├─ rewards_per_epoch_raw: ✗ May be 0 if no prior claims
├─ totalSupplyAt: ✗ Incomplete (votes still arriving)
├─ balanceOfOwnerAt: ✗ May be 0 if not yet allocated
├─ voting_power: ✗ May differ from final (last-minute delegations)
└─ Result: Unreliable → Use fallback (legacy pool share)
```

### **T-1 (60s before epoch flip)**

```
Pre-Boundary, Near-Stable Phase:
├─ rewards_per_epoch_raw: ~ Likely stable by now
├─ totalSupplyAt: ~ Mostly settled
├─ balanceOfOwnerAt: ~ Your share likely allocated
├─ voting_power: ~ Unlikely to change this late, but possible
└─ Result: Better estimate (if not using fallback)
```

### **Final / Epoch Block (0s - locked)**

```
✓ Post-Boundary, Everything Locked:
├─ rewards_per_epoch_raw: ✓ FINAL
├─ totalSupplyAt: ✓ SNAPSHOT @ epoch timestamp
├─ balanceOfOwnerAt: ✓ YOUR SHARE (locked)
├─ voting_power: ✓ FINAL voting power
└─ Result: Authoritative (no further changes possible)
```

---

## New Columns in Output

### **Est. Source (T-5/T-1)**

Shows where each estimator's value came from:

```
T5: Contract rewards     <- Means: rewards_per_epoch_raw_t5 > 0, contract path used
T1: Fallback (legacy)    <- Means: couldn't use contract, fell back to pool share ratio

OR

T5: Fallback (DB)        <- Means: rewards were 0, used DB total_amount as baseline
T1: Contract rewards     <- Different source between T-5 and T-1!
```

**Observation:**

- If both say "Contract ...": inputs were available, likely identical at T-5 and T-1
- If both say "Fallback ...": no contract state, estimates locked at legacy ratio
- If different: data became available between T-5 and T-1 (shows stabilization)

---

### **T-1 vs Expected %**

Quantifies how close T-1 was to the final Expected reward:

```
= (T-1 Estimate / Expected) × 100

99–100%  (🟢 Green)   → T-1 was almost right; good pre-epoch estimate
80–99%   (🟡 Yellow)  → T-1 was off; changes happened in final 60s
< 80%    (🔴 Red)     → Major divergence; significant shifts at epoch
```

**Interpretation:**

- **99–100%:** Contract state was stable. T-1 snapshot was highly predictive.
- **< 99%:** Something changed in the final minute. Voting or claims updated.

---

## Debug Output Per Row

For each row, you now see:

```
1. HYDX/USDC / HYDX (internal)
  Estimator Source:
    T-5: Contract rewards_per_epoch_raw > 0, fallback to DB if zero
    T-1: Contract rewards_per_epoch_raw > 0, fallback to DB if zero

  Input Availability Matrix:
    Metric                   T-5                  T-1                  Final (Epoch)
    ---------                ---                  ---                  ----
    Rewards/Epoch            670,880,000,000      670,880,000,000      670,880,000,000       ← Unchanged!
    Total Supply             1,234,567,890,000    1,234,567,890,000    1,234,567,890,000     ← Unchanged!
    Balance Owner            123,456,789,000      123,456,789,000      123,456,789,000       ← Unchanged!
    Weight (1e18)            100,000,000,000      100,000,000,000      100,000,000,000       ← Unchanged!
    Delegatee Votes          1,234,567,000,000    1,234,567,000,000    1,234,567,000,000     ← Unchanged!
    Contract Available       True                 True                 N/A (final given)

  Calculated Rewards:
    Expected (Final):        670.88
    T-5 Estimate:            670.88
    T-1 Estimate:            670.88
    T-5 vs Final:            +0.00%
    T-1 vs Final:            +0.00%

✓ T-5 ≡ T-1 (same source or inputs unchanged)
```

---

## Summary Table Reference

| Column       | Pre/Post | Boundary    | Notes                                           |
| ------------ | -------- | ----------- | ----------------------------------------------- |
| Expected     | Post     | ✓ Locked    | Final, authoritative, frozen at epoch block     |
| T-5 Est.     | Pre      | 300s before | Unreliable, likely fallback, may change         |
| T-1 Est.     | Pre      | 60s before  | Better estimate, stabilized, still not final    |
| Est. Source  | —        | —           | Shows "Contract" vs "Fallback (legacy/DB)" path |
| T-1 vs Exp % | —        | —           | Quantifies pre→post divergence                  |
| Actual %     | —        | —           | What you actually received vs Expected          |

---

## Diagnosis Guide

### **All Estimates Identical (T-5 = T-1 = Expected)**

✓ **Stable Pool:** Contract state did not change across epoch boundary.  
→ This is usually the case for mature bribes.

### **T-1 Close to Expected, but T-5 Far**

⚠️ **Late Stabilization:** Voting power or rewards updated between T-5 and T-1.  
→ Good news: you can estimate within 60s of epoch using T-1 estimate.

### **T-1 Source = "Fallback", Expected Source = "Contract"**

⏰ **Just-in-time Update:** Contract state filled in within the final 60 seconds.  
→ This token/pool became available for distribution at the last moment.

### **Expected ≠ Actual %**

💥 **Distribution Mismatch:** You received less than the contract calculated you should.  
→ See "Contract Balance Analysis" section—likely dust threshold or claiming issue.

---

## Key Takeaway

The **input availability matrix** shows you:

1. **What inputs are pre-boundary** (may change until flip): T-5 and T-1 use fallback if unavailable
2. **What inputs are post-boundary** (final): Expected uses locked contract state
3. **Why T-5 ≡ T-1**: Usually because contract inputs are already stable, occasionally because both fell back to legacy ratio
4. **How to predict**: T-1's closeness to Expected tells you if the pre-epoch estimate will hold

Use **T-1 vs Expected %** to assess **how much contract state might still shift in the final minute** before epochs lock.
