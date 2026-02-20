# Question for Hydrex Contract Team

## Quick Confirmation Request: Internal Bribe (Fee) Accumulation Timing

Hi team,

I'm building a vote optimizer and need to confirm the timing of internal bribe rewards (trading fees).

## What I Observed

Looking at BaseScan for an internal bribe contract from my last vote:

- **Contract:** `0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5` (internal bribe for WETH/cbBTC pool)
- **Observation:** Appears to receive fee tokens with nearly every swap transaction on the pool

Example: https://basescan.org/address/0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5

## My Understanding (Please Confirm)

**Trading fees accumulate in the internal bribe contract continuously throughout the epoch:**

```
User swaps on pool → Pool collects fee (e.g., 0.3%)
                   → Fee tokens immediately sent to internal bribe contract
                   → This happens on EVERY swap
                   → Balance accumulates throughout the week
```

**At the epoch flip (Wednesday 00:00 UTC):**

```
VoterV5.distribute() is called → notifyRewardAmount() called on internal bribe
                               → Makes the accumulated fees "official" for claiming
                               → Voters who voted in epoch N can now claim
```

## The Critical Question

✅ **Can I query the internal bribe contract balance BEFORE voting to see accumulated fees?**

In other words:

- Saturday-Tuesday (before voting deadline)
- Query `internal_bribe.balanceOf(reward_token)` for each reward token
- Use those balances to estimate expected rewards
- Make optimized vote allocation based on real fee data

Is this approach valid, or is there a gotcha I'm missing?

## Why This Matters

If fees accumulate continuously:

- ✅ I can optimize votes based on **actual fee data** (past 6 days of the epoch)
- ✅ External bribes + internal fees = total expected return
- ✅ Make data-driven vote allocation on Tuesday night

If fees only appear at the flip:

- ❌ Must estimate internal rewards from historical data
- ❌ Can only optimize based on external bribes
- ❌ Less accurate optimization

## Example Optimization Logic

```python
# Late in epoch N (e.g., Tuesday evening before Wednesday flip)

for gauge in all_291_gauges:
    external_bribe = voter.external_bribes(gauge)
    internal_bribe = voter.internal_bribes(gauge)

    # Query deposited external bribes
    external_rewards = sum_external_bribe_balances(external_bribe)

    # Query accumulated internal fees (THIS IS THE KEY QUESTION)
    internal_fees = sum_internal_bribe_balances(internal_bribe)

    # Calculate expected return
    current_votes = voter.weights(pool)
    my_votes = optimal_allocation[gauge]
    expected_share = my_votes / (current_votes + my_votes)
    expected_return = (external_rewards + internal_fees) * expected_share

# Vote Tuesday night with full information
voter.vote([pools], [weights])
```

## Specific Confirmation Needed

1. **Do internal bribe contracts receive fee tokens continuously during the epoch?**
   - YES / NO

2. **Can we query internal bribe token balances before voting to estimate rewards?**
   - YES / NO
3. **Are there any gotchas or edge cases?**
   - (e.g., fees locked, different accounting, etc.)

## My Setup

- **Voter:** VoterV5 at `0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b`
- **Network:** Base mainnet
- **Voting Power:** 1,183,272
- **Last Vote Result:** $718.82 from internal bribes, $235.00 from external bribes

Thanks for clarifying this mechanism—it's the key to building an effective optimizer!

---

**TL;DR:** Can I query internal bribe contract balances (accumulated fees) before voting to optimize my allocation, or are fees only visible after the epoch flip?
