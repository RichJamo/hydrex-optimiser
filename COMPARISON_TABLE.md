# Calculated vs Actual Comparison Table

# For Jan 22 Voting Epoch → Jan 29 Reward Epoch

## Summary

- **Our Total Calculation:** $5,931.85
- **Your Actual Total:** ~$1,315.12
- **Discrepancy:** 4.5x higher than actual

## Token-by-Token Breakdown

| Token           | Type  |     **ACTUAL Amount** | **CALCULATED Amount** | Ratio | Issue                          |
| --------------- | ----- | --------------------: | --------------------: | ----: | ------------------------------ |
| **HYDX**        | Bribe |             **65.48** |                     0 |     - | ❌ Missing from database       |
| **WETH**        | Fee   |            **0.2052** |                0.0043 | 0.02x | ⚠️ We calculated 47x LESS      |
| **WETH**        | Bribe |            **0.0032** |                     0 |     - | ❌ Missing                     |
| **FACY**        | Fee   |         **51,286.02** |                     0 |     - | ❌ Missing from database       |
| **BETR**        | Fee   |        **248,865.85** |             7,687,538 |   31x | 🔴 We calculated 31x MORE      |
| **BETR**        | Bribe |            **(none)** |           265,573,361 |     - | 🔴 We show bribe, you got none |
| **USDC**        | Fee   |             **34.40** |                     0 |     - | ❌ Missing from database       |
| **chubal**      | Fee   |        **262,130.57** |                     0 |     - | ❌ Missing from database       |
| **chubal**      | Bribe |      **1,653,224.28** |             1,954,032 |  1.2x | ⚠️ Close, but off by 18%       |
| **metacademax** | Fee   | **5,813** (2×2,906.5) |                     0 |     - | ❌ Missing from database       |
| **OTTO**        | Fee   |         **78,769.96** |                     0 |     - | ❌ Missing from database       |
| **PIGGY**       | Fee   |          **51,172.9** |            63,156,442 | 1234x | 🔴 We calculated 1234x MORE    |
| **BAES**        | Fee   |      **2,047,444.14** |                     0 |     - | ❌ Missing from database       |
| **BAES**        | Bribe |    **115,405,834.45** |                     0 |     - | ❌ Missing from database       |
| **oHYDX**       | Bribe |          **3,658.67** |                15,934 |  4.4x | 🔴 We calculated 4.4x MORE     |
| **REGEN**       | Fee   |             **99.33** |                     0 |     - | ❌ Missing from database       |
| **FUEGO**       | Fee   |            **190.94** |                     0 |     - | ❌ Missing from database       |

## Critical Issues Found

### 1. Voting Share Calculation is BROKEN 🔴

Our output shows impossible percentages:

- Gauge 0x0a2918e8...: **14,661%** share (should be ~0.01-10%)
- Gauge 0x1df220b4...: **2,455%** share
- Gauge 0x6321d730...: **367%** share

**This means we're dividing by the wrong total votes.** We need to sum ALL votes for a gauge (not just bribes), but we're only summing votes from bribes in our database.

### 2. Missing 11 out of 17 Token Types ❌

Tokens completely missing from our calculations:

- HYDX, FACY, USDC, OTTO, BAES, REGEN, FUEGO (fees)
- HYDX, BAES (bribes)

This suggests our database doesn't have complete bribes data for this epoch.

### 3. Massive Overestimation for Tokens We Do Have 🔴

- **PIGGY:** 1234x too high
- **BETR:** 31x too high
- **oHYDX:** 4.4x too high

### 4. Fee vs Bribe Mismatch

- We calculated 265M BETR bribe, you received **ZERO** BETR bribe
- You received BETR as fee only

## Root Cause

**The "DB Total" in our calculation is WRONG.**

We're calculating:

```
Your Share = Your Votes / DB Total Votes
```

But "DB Total Votes" is being calculated as the **sum of votes from users who submitted bribes**, not the **total votes for that gauge from all users**.

This makes "Your Share" artificially inflated (often >100%), which multiplies your token amounts by 10x, 100x, or even 1000x.

## Next Steps

1. Fix voting share calculation - need to get actual total votes per gauge
2. Investigate why 11 token types are missing from database
3. Re-sync bribes data for Jan 22 epoch to ensure completeness
