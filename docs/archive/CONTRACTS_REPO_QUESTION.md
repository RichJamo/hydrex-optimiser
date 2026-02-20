# Question for Hydrex Contracts Repository

## Context

I'm building a vote optimizer for Hydrex on Base and need to understand the exact timing of when internal bribe rewards (trading fees) become available/visible to voters.

## Setup

- **Voter Contract (VoterV5):** `0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b`
- **Epoch System:** Weekly epochs starting Wednesday 00:00 UTC
- **Internal Bribes:** Each gauge has an internal bribe contract that receives trading fees from the pool

## The Critical Question

**When are trading fees transferred from the pool to the internal bribe contract?**

### Scenario A: Continuous/Real-time

```
Pool generates fees → Fees immediately sent to internal bribe contract
                    → Throughout the epoch, balance accumulates
                    → Voters can query internal bribe balance BEFORE voting
```

✅ This allows optimization based on actual fee data

### Scenario B: Epoch Flip Only

```
Pool generates fees → Fees stay in pool during epoch
                    → Only at epoch boundary (Wednesday 00:00 UTC)
                    → Fees are transferred to internal bribe
                    → `distribute()` is called during the flip
```

❌ This means voters must vote BEFORE knowing fee amounts

## What I've Observed

Looking at my last vote (epoch N → N+1 boundary):

- Voted for 4 pools with internal bribe contracts
- Received rewards from internal bribes after claiming

Example internal bribe contract: `0xdbd3DA2c3183a4db0d6a1E648a06B14b593dB7B5`

- Paid out: WETH + cbBTC (trading fees)
- Total value: $246.75

## Specific Questions

1. **When do pools transfer fees to internal bribe contracts?**
   - Continuously during the epoch?
   - Only at the epoch flip via `distribute()`?
   - Or some other mechanism?

2. **Can we query expected internal bribe amounts before voting?**
   - Is there a view function to check pending/accumulated fees?
   - Or do we need to estimate based on historical data?

3. **What happens during the epoch flip?**
   - Does `VoterV5.distribute()` trigger fee collection from pools?
   - When exactly does `notifyRewardAmount()` get called on internal bribes?

4. **Best practice for optimization:**
   - Should we query internal bribe balances before voting?
   - Or is it impossible to know fee amounts until after the flip?

## Why This Matters

For vote optimization, I need to know:

- **External bribes:** Can query anytime (deposited manually)
- **Internal bribes (fees):** Timing determines if we can use this data

If fees are only visible AFTER the flip, I'll need to:

- Use historical fee data to estimate
- Focus more on external bribes for real-time optimization

## Example Code (what I'm trying to do)

```python
# Before voting (Saturday-Tuesday), query all gauges:
for gauge in all_gauges:
    external_bribe = voter.external_bribes(gauge)
    internal_bribe = voter.internal_bribes(gauge)

    # External bribes - can query anytime ✓
    external_balance = get_bribe_rewards(external_bribe)

    # Internal bribes - when is this accurate?
    internal_balance = get_bribe_rewards(internal_bribe)  # ❓

    total_expected = external_balance + internal_balance

# Optimize vote allocation based on expected returns
optimal_allocation = optimize(all_gauges, voting_power)
```

## Additional Context

- **My voting power:** 1,183,272
- **Last vote return:** $954.53 across 4 pools
- **Breakdown:** $718.82 from internal bribes, $235.00 from external bribes
- **Goal:** Maximize ROI by voting late in the epoch with full information

## New Question: Pre-Flip Visibility of balanceOfOwnerAt

While debugging historical epochs, we can query:

- `rewardData(token, epoch)` pre-flip (often non-zero)
- `totalSupplyAt(epoch)` pre-flip (moves significantly before boundary)

But for our delegatee we repeatedly see:

- `balanceOfOwnerAt(delegatee, epoch) == 0` at T-5 and T-1
- then non-zero at/after the epoch boundary

This makes contract-share estimation impossible before the flip.

Could you clarify:

1. Is `balanceOfOwnerAt(owner, epoch)` expected to remain zero until a specific boundary action?
2. What contract/event updates that owner balance checkpoint?
3. Is there any **pre-flip** view we should use as an estimate/proxy for owner share (e.g. pending owner balance, raw gauge vote balance, or another checkpoint source)?
4. If no pre-flip source exists, what is the intended way for frontends/optimizers to estimate owner share before epoch close?

Example addresses from our tests are available if helpful; happy to provide exact tx/block traces.

Thanks for any insight into the fee distribution mechanism!

---

## Contracts AI Response (19 Feb 2026)

Summary received:

1. `balanceOfOwnerAt(owner, epoch)` can remain `0` for an epoch until that owner/delegatee vote path writes it (typically via `vote()` / `poke()` in or after that epoch).
2. The owner checkpoint write is in `BribeV2.deposit/withdraw`, triggered from `VoterV5._vote/_reset`.
3. Relevant observability/events:
   - Bribe: `Staked`, `Withdrawn`
   - Voter: `Voted`, `Abstained`
4. There is no canonical on-chain “pending owner bribe balance” before that write.
5. Best pre-write proxy is current vote state (e.g. `voter.votes(delegatee, pool)` + delegatee power from `ve.getPastVotes`) if already cast; otherwise owner share is unknown and must be estimated off-chain.
6. Intended optimizer/frontend behavior:
   - treat owner share as unknown/zero until vote tx is mined for that epoch,
   - then recompute from live state,
   - use off-chain forecasting (last known allocations/assumptions) for pre-close projections.

## Practical Implications for This Repo

- The observed `balanceOfOwnerAt(delegatee, epoch) == 0` at T-5/T-1 is expected protocol behavior, not an RPC/data bug.
- Our fallback path is required pre-flip when owner checkpoint is not written.
- “T-5 == T-1” in current runs is consistent with both snapshots being pre-write for owner checkpoint.
- Pre-close model quality should come from off-chain vote-share forecasting, not from assuming checkpointed owner share exists.

## Optional Next Validation (if needed)

Provide one `vote()/poke()` transaction hash + block number to map exact write/read timing and confirm the precise epoch key used by the checkpoint.
