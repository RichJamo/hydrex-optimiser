# Calculated vs Actual Comparison Table

## Jan 29 Reward Epoch (Voting on Jan 22)

**Calculated Total:** $178.47  
**Actual Received:** $1,315.12  
**Gap:** $1,136.65 (87% of actual amount missing from calculation)

## Token-by-Token Breakdown

| Token      | Type  |  **ACTUAL Amount** | **CALCULATED Amount** | Ratio | Notes                              |
| ---------- | ----- | -----------------: | --------------------: | ----: | ---------------------------------- |
| **HYDX**   | Bribe |          **65.48** |                     0 |     - | ❌ Not in bribes database          |
| **BETR**   | Bribe |                  0 |           5,566,121.3 |     - | ⚠️ We show bribe, you got fee only |
| **BETR**   | Fee   |     **248,865.85** |             161,122.2 |  1.5x | ⚠️ We calculated ~64% of actual    |
| **oHYDX**  | Bribe |       **3,658.67** |                333.97 | 0.09x | ⚠️ We calculated ~9% of actual     |
| **FACY**   | Bribe |      **51,286.02** |                     0 |     - | ❌ Not in bribes database          |
| **FUEGO**  | Bribe |         **190.94** |                     0 |     - | ❌ Not in bribes database          |
| **OTTO**   | Bribe |      **78,769.96** |                     0 |     - | ❌ Not in bribes database          |
| **PIGGY**  | Bribe |       **51,172.9** |                     0 |     - | ❌ Not in bribes database          |
| **BAES**   | Bribe |   **2,047,444.14** |                     0 |     - | ❌ Not in bribes database          |
| **BAES**   | Bribe | **115,405,834.45** |                     0 |     - | ❌ Not in bribes database          |
| **chubal** | Bribe |   **1,653,224.28** |             1,954,032 |  1.2x | ⚠️ Close match, off by 18%         |
| **REGEN**  | Bribe |          **99.33** |                     0 |     - | ❌ Not in bribes database          |
| **USDC**   | Bribe |          **34.40** |                     0 |     - | ❌ Not in bribes database          |
| **WETH**   | Bribe |         **0.2052** |                     0 |     - | ❌ Not in bribes database          |

## Summary of Issues

### Tokens in Database (4 found):

- **BETR** (fee + bribe calculated, but you received as fee)
- **oHYDX** (bribe - calculated ~9% of actual)
- **chubal** (bribe - calculated ~120% of actual)
- **0x4200...0006, 0x8335...2913, others** (fee tokens, minimal amounts)

### Tokens NOT in Database (10 missing):

- **HYDX, FACY, FUEGO, OTTO, PIGGY, BAES, REGEN, USDC, WETH** - All classified as bribes but completely absent from subgraph bribe data
- These account for **$1,136.65 (87% of your actual return)**

## Root Cause Analysis

The subgraph's `Bribe` entity (which captures `RewardAdded` events) is missing these 10 token types entirely. Either:

1. These tokens were added to internal bribe contracts but never forwarded via `notifyRewardAmount()`
2. These tokens came from a different reward mechanism not tracked by the subgraph
3. The subgraph indexing is incomplete for these specific tokens
