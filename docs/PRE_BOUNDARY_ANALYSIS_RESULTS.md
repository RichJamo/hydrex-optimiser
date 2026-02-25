# Pre-Boundary Optimization Analysis Results

**Date:** 2026-02-25  
**Analysis:** Comparing decision quality at 20 blocks before, 1 block before, and at boundary

## Executive Summary

We analyzed 5 recent epochs to assess how early we can make optimal voting decisions before the epoch boundary. The results show **remarkably stable predictions**:

### Key Findings

1. **Perfect Pool Selection at 20 Blocks Before Boundary**
   - Top 5 pool rankings were identical or near-identical at 20 blocks before vs actual boundary
   - Expected returns: **100% of optimal** in all tested epochs
   - Vote changes occurred in 29-55 gauges (out of 291 total), but not enough to affect rankings

2. **Data Stability**
   - **Rewards:** Locked in well before boundary (no changes observed 20+ blocks before)
   - **Votes:** Minor changes occur, but top pools remain stable
   - **Rankings:** ROI rankings highly stable despite vote fluctuations

3. **Prediction Window**
   - ✅ **20 blocks before boundary:** Fully reliable for decision-making
   - ✅ **1 block before boundary:** Identical to boundary (as expected)

## Detailed Results by Epoch

### Epoch 1771459200

- Total gauges: 291 tracked, 119 with votes and rewards
- Gauges with vote changes (20 blocks → boundary): 31
- Top 5 selection: **Identical** across all time points
- Expected returns: **100%** of optimal

### Epoch 1770854400

- Total gauges: 291 tracked, 118 with votes and rewards
- Gauges with vote changes: 29
- Notable: One pool (0x0a2918e8) moved from rank 2 at 20 blocks before to rank 3 at boundary, but still in top 5
- Expected returns: **100%** of optimal

### Epoch 1770249600

- Total gauges: 291 tracked, 114 with votes and rewards
- Gauges with vote changes: 39
- Top 5 selection: **Identical** across all time points
- Expected returns: **100%** of optimal

### Epoch 1769644800

- Gauges with vote changes: 55 (most volatile of analyzed epochs)
- Top 5 selection: **Identical** across all time points
- Expected returns: **100%** of optimal

### Epoch 1769040000

- Gauges with vote changes: 50
- Top 5 selection: **Identical** across all time points
- Expected returns: **100%** of optimal

## Implications for Strategy

### Current State

- **No prediction improvement needed** for basic pool selection
- Early decision-making (20 blocks before) is as good as waiting for boundary
- Simple ROI ranking (rewards/votes) is stable and reliable

### Next Steps for Optimization

Since prediction accuracy is already 100% at 20 blocks before, further optimization should focus on:

1. **Dynamic Allocation**
   - Instead of equal 20% splits, optimize allocation proportions
   - Test if weighted allocation based on ROI differences improves returns
2. **Risk Management**
   - Analyze volatility of rewards and votes across epochs
   - Identify reliable vs unreliable gauges
   - Build confidence scores for each pool

3. **Expanded Pool Set**
   - Current analysis uses top 5, test top 10 or top 3
   - Analyze trade-offs between concentration and diversification
4. **Gas Cost Optimization**
   - With early prediction capability, we can analyze gas costs at different times
   - Optimize for gas efficiency without sacrificing returns

5. **Multi-Epoch Strategies**
   - Since single-epoch prediction is solved, look at multi-epoch patterns
   - Identify persistently high-ROI pools
   - Build strategies that consider pool lifetime and sustainability

## Technical Details

### Data Sources

- Boundary data: `boundary_gauge_values` (votes) + `boundary_reward_snapshots` (rewards)
- Pre-boundary data: `boundary_vote_samples` + `boundary_reward_samples`
- Offsets tested: 1 block, 20 blocks before boundary

### Methodology

1. For each time point (boundary, -1 block, -20 blocks):
   - Calculate ROI = total_rewards / total_votes for each gauge
   - Rewards are normalized using token decimals from `token_metadata` table
   - Votes are in raw units (no normalization needed)
   - Select top 5 gauges by ROI
   - Calculate expected returns assuming equal 20% vote allocation

2. Compare selected pools and expected returns across time points

### Limitations

- USD price data unavailable in some tables (analyzed in normalized token units)
- Analysis assumes equal allocation across 5 pools
- Does not account for gas costs or transaction timing
- Minimum vote threshold of 1.0 applied to avoid division-by-zero edge cases

## Code Output Sample

```
═══ Analyzing Epoch 1771459200 ═══
Boundary: 119 gauges with votes
1 block before: 119 gauges with votes
20 blocks before: 119 gauges with votes

Epoch 1771459200 - Optimization Comparison (Normalized Token Units)
┌────────────────────────┬───────────────────────┬─────────────────┬──────────────────┐
│ Metric                 │ At Boundary (Optimal) │  1 Block Before │ 20 Blocks Before │
├────────────────────────┼───────────────────────┼─────────────────┼──────────────────┤
│ Total Expected Returns │       97,516,723.9045 │ 97,516,723.9045 │  97,516,723.9045 │
│ vs Optimal             │               100.00% │         100.00% │          100.00% │
└────────────────────────┴───────────────────────┴─────────────────┴──────────────────┘

Pool Selections:

Boundary (Optimal):
  1. 0x4665cf9c... ROI: 28923.80 (293,220.18 / 10)
  2. 0x08923820... ROI: 270.97 (328,783,321.40 / 1,213,362)
  3. 0x89ef3f3e... ROI: 192.39 (12,866,781.45 / 66,879)
  4. 0x19dbf0c8... ROI: 154.46 (402.01 / 3)
  5. 0x69d66e75... ROI: 153.61 (431,963,018.32 / 2,812,023)

20 Blocks Before: IDENTICAL top 5 pools (minor ROI changes due to vote fluctuations)
```

Note: ROI values represent normalized tokens earned per vote. The top pool has an exceptional ROI of ~29,000 due to having very high rewards (293k) with minimal competition (only 10 votes).

## Conclusion

The Hydrex voting system demonstrates **excellent predictability** for pool selection. Decision-makers can confidently choose optimal pools **20 blocks before the boundary** with **zero loss** in expected returns compared to waiting until the boundary.

This finding simplifies the optimization problem significantly: the challenge is no longer _prediction_, but rather _allocation optimization_ and _risk management_ within the predicted top pools.
