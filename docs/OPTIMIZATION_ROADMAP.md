# Hydrex Optimization Roadmap

**Created:** 2026-02-25  
**Updated:** 2026-02-25 (URGENT: Added Phase 0 for immediate voting needs)  
**Status:** URGENT - Need voting capability TODAY  
**Current Milestone:** Pre-boundary analysis complete, moving to production optimization

---

## ⚠️ Phase 0: URGENT - Today's Voting (CRITICAL)

### 0.1 Immediate Voting Recommendation 🚨

**Priority:** CRITICAL  
**Estimated Time:** 1-2 hours  
**Status:** Not Started  
**Deadline:** TODAY (before voting closes)

**Objective:** Generate voting recommendations for today's vote using best available data.

**Tasks:**

- [ ] Determine current epoch and voting deadline
- [ ] Fetch latest data (rewards, votes) from current state
- [ ] Run optimization analysis with current best method:
  - Calculate ROI for all gauges (with marginal ROI if time permits)
  - Select top 5 pools
  - Recommend allocation (equal 20% for now, optimize if time)
- [ ] Generate voting instructions:
  - Gauge addresses
  - Vote amounts (from YOUR_VOTING_POWER)
  - Summary of expected returns
  - Risk assessment (if pools have history)
- [ ] Create script: `scripts/generate_voting_recommendation.py`
- [ ] Validate output format matches voting interface requirements
- [ ] **Execute vote before deadline**

**Output Format Needed:**

```
Gauge Address                               | Votes     | % Allocation
0x7d1bb380a7275a47603dab3b6521d5a8712dfba5 | 236,654   | 20%
0xdc470dc0b3247058ea4605dba6e48a9b2a083971 | 236,654   | 20%
...
```

**Success Criteria:**

- Working recommendation script
- Vote submitted successfully
- Documentation of what was voted and why

---

### 0.2 Automated Boundary Re-Voting System 🤖

**Priority:** CRITICAL  
**Estimated Time:** 4-6 hours  
**Status:** Not Started  
**Deadline:** TODAY (setup before sleep)

**Objective:** Automated system to re-vote at optimal time (20 blocks before boundary) without manual intervention.

**Architecture Components:**

1. **Boundary Monitor Service**
   - Continuously check current block number
   - Calculate blocks until epoch boundary
   - Trigger action at 20 blocks before boundary
   - Alerting/logging system

2. **Data Fetcher**
   - Fetch fresh reward data at trigger time
   - Fetch fresh vote data at trigger time
   - Calculate ROI for all gauges
   - Select optimal pools

3. **Vote Executor**
   - Generate transaction for voting
   - Sign transaction with wallet
   - Submit to blockchain
   - Verify transaction success
   - Log results

4. **Safety & Monitoring**
   - Dry-run mode for testing
   - Transaction simulation before sending
   - Gas price limits
   - Fallback to manual if issues detected
   - Email/notification on success/failure

**Tasks:**

**Part A: Monitoring (1-2 hours)**

- [ ] Create `scripts/boundary_monitor.py`
- [ ] Implement block monitoring loop
  - Query current block every 30 seconds
  - Calculate blocks until boundary (from `epoch_boundaries` table)
  - Trigger callback at configured threshold (default: 20 blocks)
- [ ] Add configuration:
  - RPC endpoint
  - Trigger threshold (blocks before boundary)
  - Retry logic if RPC fails
- [ ] Test: Simulate boundary approach with mock data

**Part B: Voting Logic (1-2 hours)**

- [ ] Create `scripts/auto_voter.py`
- [ ] Implement vote calculation:
  - Fetch current state (rewards + votes)
  - Run optimization (reuse from Phase 0.1)
  - Generate vote allocation
- [ ] Implement transaction creation:
  - Interface with Voter contract
  - Generate `vote()` or `batchVote()` call
  - Encode transaction data
- [ ] Add wallet integration:
  - Load private key from secure location (env var or keyfile)
  - Sign transaction
  - Gas estimation and limit setting

**Part C: Safety & Execution (1-2 hours)**

- [ ] Implement safety checks:
  - Dry-run mode flag
  - Transaction simulation (eth_call before send)
  - Sanity checks (vote amounts sum to YOUR_VOTING_POWER)
  - Gas price limit (don't execute if gas too high)
- [ ] Add logging and monitoring:
  - Log file with full execution trace
  - Success/failure notification (email, Slack, stdout)
  - Record transaction hash
  - Verify transaction inclusion in block
- [ ] Test full flow:
  - Test in dry-run mode
  - Test with small test account (if available)
  - Verify no errors, proper gas estimation

**Part D: Deployment & Activation (0.5-1 hour)**

- [ ] Create systemd service or cron job to run monitor
- [ ] Document startup procedure
- [ ] Create kill switch / manual override
- [ ] Set up monitoring (check it's still running)
- [ ] **Activate for next epoch boundary**

**Configuration File Needed (.env or config):**

```bash
# Automated Voting Settings
AUTO_VOTE_ENABLED=true
AUTO_VOTE_DRY_RUN=false
AUTO_VOTE_TRIGGER_BLOCKS_BEFORE=20
AUTO_VOTE_MAX_GAS_PRICE_GWEI=10
AUTO_VOTE_WALLET_KEYFILE=./secrets/voting_wallet.key
AUTO_VOTE_NOTIFICATION_EMAIL=your@email.com
```

**Security Considerations:**
⚠️ **IMPORTANT:** Automated voting requires access to wallet private key

- Store key in encrypted file or secure env var
- Use dedicated voting wallet with only necessary funds
- Implement transaction value limits
- Log all actions for audit trail
- Consider using hardware wallet or multi-sig for large amounts

**Success Criteria:**

- Monitor successfully detects boundary approach
- Fresh data fetched at correct time
- Optimal vote calculated correctly
- Transaction submitted successfully
- System runs unattended until boundary
- Full logging/alerting in place

**Rollback Plan:**

- Manual override capability at any time
- Dry-run mode for testing
- Detailed logs to debug issues
- Fallback to Phase 0.1 manual voting if automation fails

---

## Phase 1: Foundation & Cleanup

### 1.1 Database & Repository Cleanup 🧹

**Priority:** High  
**Estimated Time:** 2-3 hours  
**Status:** Not Started

**Objective:** Remove legacy tables and unused code to reduce confusion and improve maintainability.

**Tasks:**

- [ ] Audit all database tables and identify:
  - Active tables used in current fetches/analysis
  - Legacy tables with zero/obsolete data
  - Redundant tables that duplicate data
- [ ] Document schema for tables we're keeping
- [ ] Create backup before cleanup
- [ ] Drop unused tables from `data.db`
- [ ] Remove or archive old scripts in `scripts/archive/`
- [ ] Update documentation to reflect current state
- [ ] Clean up any temp databases (e.g., `preboundary_rewarddata_5.db`, `preboundary_balances_5.db`)

**Success Criteria:**

- Clear understanding of which tables store what data
- No confusion about zero-value vs actual-zero data
- Documentation updated to match current schema

---

## Phase 2: Analysis Improvements

### 2.1 Fix Zero-Vote Pool Edge Case 🔧

**Priority:** High  
**Estimated Time:** 1-2 hours  
**Status:** Not Started

**Objective:** Properly evaluate pools with zero or near-zero votes by accounting for the impact of our votes.

**Current Issue:**

- Pools with 0-1 votes are filtered out or create artificially high ROI
- We're not considering that OUR votes will change the ROI calculation
- Missing opportunity to find under-voted high-reward pools

**Solution Approach:**

1. Modify ROI calculation to include hypothetical vote contribution:
   ```
   adjusted_roi = total_rewards / (existing_votes + our_allocated_votes)
   ```
2. Create a "marginal ROI" metric that shows ROI after adding our votes
3. Compare current ROI vs marginal ROI for decision making
4. Test on pools with various vote levels (0, 1, 10, 100, etc.)

**Tasks:**

- [ ] Implement `calculate_marginal_roi()` function
- [ ] Update `calculate_roi_and_select_top5()` to use marginal ROI
- [ ] Add parameter for vote allocation amount (e.g., YOUR_VOTING_POWER / 5)
- [ ] Test with edge cases (0 votes, 1 vote, high votes)
- [ ] Compare results: current method vs marginal method
- [ ] Update output to show both current and marginal ROI

**Success Criteria:**

- Zero-vote pools can be properly evaluated
- No division by zero errors
- Clear understanding of how our votes affect pool ROI

---

### 2.2 Extend Analysis to All 23 Epochs 📊

**Priority:** High  
**Estimated Time:** 1 hour  
**Status:** Not Started

**Objective:** Validate pre-boundary prediction accuracy across the full historical dataset.

**Tasks:**

- [ ] Modify `preboundary_returns_analysis.py` to process all epochs (not just 5)
- [ ] Add summary statistics across all epochs:
  - Average prediction accuracy
  - Variance in pool selections
  - Consistency of top performers
- [ ] Generate comprehensive report with:
  - Per-epoch results
  - Aggregate statistics
  - Visualization-ready CSV export
- [ ] Identify any epochs with anomalies or outliers

**Success Criteria:**

- Analysis runs successfully on all 23 epochs
- Clear picture of prediction stability over time
- CSV output for further analysis/visualization

---

## Phase 3: Data Enhancement

### 3.1 Token Price Collection ⚠️

**Priority:** Medium  
**Estimated Time:** 4-6 hours (cautious approach)  
**Status:** Not Started

**Objective:** Collect USD prices for reward tokens across all epochs to enable USD-denominated analysis.

**Challenges:**

- Time-consuming API calls
- Rate limiting on CoinGecko/price APIs
- Historical price data may not be available for all tokens
- Need to handle missing prices gracefully

**Approach:**

1. **Phase 3.1a: Price Source Assessment**
   - [ ] Identify which tokens are missing prices
   - [ ] Check what price sources are available (CoinGecko, Analytics Subgraph, DEX data)
   - [ ] Prioritize tokens by frequency/importance (most used in top pools)
   - [ ] Document which tokens have no price source available

2. **Phase 3.1b: Incremental Price Fetching**
   - [ ] Start with most recent epoch, work backwards
   - [ ] Implement rate limiting and retry logic
   - [ ] Cache prices to avoid refetching
   - [ ] Create script: `scripts/backfill_token_prices.py`
   - [ ] Run in batches (e.g., 5 epochs at a time)
   - [ ] Monitor for failures and handle gracefully

3. **Phase 3.1c: Price Data Integration**
   - [ ] Update analysis to use USD prices where available
   - [ ] Add fallback to token-normalized values when price missing
   - [ ] Show both USD and token-normalized results in reports

**Success Criteria:**

- Price coverage for top 20 most-used reward tokens
- No analysis failures due to missing prices
- Clear indication in reports when prices are/aren't available

---

## Phase 4: Advanced Optimization

### 4.1 Dynamic Vote Allocation 🎯

**Priority:** Medium  
**Estimated Time:** 3-4 hours  
**Status:** Not Started

**Objective:** Optimize vote distribution across selected pools instead of equal 20% splits.

**Context:** Current analysis assumes equal allocation. But if one pool has ROI of 29,000 and another has ROI of 150, should we really allocate equally?

**Approaches to Test:**

1. **Proportional to ROI:** Allocate more to higher ROI pools
2. **Kelly Criterion:** Risk-adjusted allocation based on confidence
3. **Constrained Optimization:** Maximize returns subject to diversification constraints
4. **Marginal Returns:** Allocate to equalize marginal return across pools

**Tasks:**

- [ ] Implement allocation strategy functions
- [ ] Create `analysis/allocation_optimizer.py`
- [ ] Test each strategy against historical data
- [ ] Compare returns: equal split vs optimized allocation
- [ ] Account for gas costs (more complexity = more transactions)
- [ ] Generate report showing improvement over equal allocation

**Success Criteria:**

- Quantified improvement over equal allocation
- Clear recommendation for allocation strategy
- Consideration of practical constraints (gas, complexity)

---

### 4.2 Risk Management & Confidence Scoring 📈

**Priority:** Medium  
**Estimated Time:** 3-4 hours  
**Status:** Not Started

**Objective:** Identify reliable vs volatile pools and build confidence scores.

**Metrics to Calculate:**

1. **Volatility Measures:**
   - ROI variance across epochs
   - Reward amount volatility
   - Vote count volatility
2. **Reliability Scores:**
   - Presence in top-N across multiple epochs
   - Consistency of ROI ranking
   - Reward predictability
3. **Risk Indicators:**
   - Pool/gauge age (new = higher risk?)
   - Bribe diversity (single token vs multiple)
   - Historical disappearance rate

**Tasks:**

- [ ] Create `analysis/risk_metrics.py`
- [ ] Calculate per-pool statistics across all epochs:
  - Mean ROI, std dev, coefficient of variation
  - Appearance frequency in top 5/10
  - Reward consistency score
- [ ] Build risk scoring model
- [ ] Generate pool "report cards" with risk/return profiles
- [ ] Integrate risk scores into pool selection logic

**Success Criteria:**

- Risk score for each frequently-appearing pool
- Clear identification of "safe bets" vs "high risk/high reward"
- Actionable recommendations considering risk tolerance

---

### 4.3 Pool Set Optimization (3 vs 5 vs 10 pools) 🔢

**Priority:** Low  
**Estimated Time:** 2-3 hours  
**Status:** Not Started

**Objective:** Determine optimal number of pools to split votes across.

**Trade-offs:**

- **Fewer pools (3):** Concentrated bets, higher gas efficiency, higher risk
- **More pools (10):** Diversification, lower risk, higher gas costs, diluted impact

**Tasks:**

- [ ] Modify analysis to test N=3, 5, 7, 10 pools
- [ ] Calculate for each N:
  - Expected returns
  - Risk (variance)
  - Sharpe-like ratio (return/risk)
  - Estimated gas costs
- [ ] Analyze diminishing returns: does pool #10 add meaningful value?
- [ ] Generate recommendation based on risk tolerance

**Success Criteria:**

- Clear understanding of returns vs risk trade-off
- Recommendation for optimal N
- Consideration of practical constraints

---

### 4.4 Multi-Epoch Pattern Analysis 🔍

**Priority:** Medium  
**Estimated Time:** 3-4 hours  
**Status:** Not Started

**Objective:** Identify pools that consistently perform well across multiple epochs.

**Questions to Answer:**

1. Which pools appear in top 5 most frequently?
2. Are there "persistent high performers"?
3. Do certain pool types (token pairs, gauge types) perform better?
4. Are there seasonal or cyclical patterns?
5. Can we predict future high performers based on past patterns?

**Tasks:**

- [ ] Create `analysis/multi_epoch_patterns.py`
- [ ] Calculate pool persistence metrics:
  - Top-5 appearance frequency
  - Average ROI when present
  - Longevity (how many consecutive epochs in top-N)
- [ ] Identify pool characteristics correlated with performance:
  - Token types
  - Gauge addresses
  - Bribe contract patterns
- [ ] Build predictive features for next-epoch selection
- [ ] Validate: would "persistent pool strategy" outperform single-epoch optimization?

**Success Criteria:**

- List of "reliable high performers"
- Understanding of what makes a pool consistently good
- Potential strategy: "always vote for these + optimize remaining"

---

## Phase 5: Production Readiness

### 5.1 Automated Decision Pipeline 🤖

**Priority:** Low  
**Estimated Time:** 4-5 hours  
**Status:** Not Started

**Objective:** Create end-to-end pipeline from data fetch to voting recommendation.

**Components:**

1. Data freshness check
2. Automated pre-boundary snapshot (20 blocks before)
3. Pool selection with chosen strategy
4. Vote allocation optimization
5. Output format compatible with voting interface
6. Confidence/risk assessment

**Tasks:**

- [ ] Create `analysis/voting_recommender.py`
- [ ] Integrate all analysis components
- [ ] Add command-line interface for different strategies
- [ ] Generate voting instructions (gauge addresses + vote amounts)
- [ ] Add validation checks before recommendations
- [ ] Create monitoring/alerting for data issues

---

### 5.2 Documentation & Handoff 📚

**Priority:** Medium  
**Estimated Time:** 2-3 hours  
**Status:** Not Started

**Tasks:**

- [ ] Update README with current architecture
- [ ] Document all analysis scripts and their purpose
- [ ] Create user guide for running analysis
- [ ] Document database schema
- [ ] Add examples of common workflows
- [ ] Create troubleshooting guide

---

## Execution Order Recommendation

**🚨 TODAY (Phase 0 - URGENT):**

1. **Phase 0.1:** Generate voting recommendation (1-2 hours) - MUST DO FIRST
2. **Execute manual vote based on recommendation**
3. **Phase 0.2:** Build automated re-voting system (4-6 hours) - DO TODAY
4. **Test and deploy automation before sleep**

**Week 1: Foundation (After automation is working)** 5. Phase 1.1: Database cleanup (high priority, clears confusion) 6. Phase 2.1: Fix zero-vote edge case (blocks other work) 7. Phase 2.2: Extend to 23 epochs (validate at scale)

**Week 2: Enhancement** 8. Phase 3.1a-b: Token price collection (start cautiously) 9. Phase 4.2: Risk management (useful context for Phase 4.1) 10. Phase 4.1: Dynamic allocation (benefits from risk scores)

**Week 3: Advanced Analysis** 11. Phase 4.4: Multi-epoch patterns (needs full epoch data) 12. Phase 3.1c: Price integration into analysis 13. Phase 4.3: Pool set optimization (least critical)

**Week 4: Production** 14. Phase 5.1: Automated pipeline (enhance Phase 0.2) 15. Phase 5.2: Documentation

---

## Dependencies & Blockers

**CRITICAL PATH:**

- Phase 0.1 must complete TODAY before voting deadline
- Phase 0.2 should complete TODAY for unattended operation
- All other phases can wait until Phase 0 is complete

**Original Dependencies:**

- Phase 2.2 requires Phase 2.1 (fix edge case first)
- Phase 4.1 benefits from but doesn't require Phase 4.2
- Phase 4.4 needs Phase 2.2 (all epochs analyzed)
- Phase 5.1 can enhance Phase 0.2 automation

---

## Success Metrics

**URGENT (Phase 0):**

- Vote successfully submitted today
- Automated system running and monitoring boundary
- Transaction successfully submitted at optimal time (20 blocks before)
- Full audit trail of automated actions

**Technical:**

- All analysis runs without errors on all 23 epochs
- No data confusion (clear schema, no ghost tables)
- Reproducible results with clear data provenance

**Analysis:**

- Quantified improvement over equal allocation baseline
- Risk-adjusted pool selection with confidence scores
- USD-denominated returns where prices available

**Practical:**

- Clear voting recommendations ready before epoch boundaries
- Automated pipeline reducing manual work
- Documentation enabling independent operation

---

## Notes & Considerations

**🚨 PHASE 0 CRITICAL NOTES:**

1. **Time Pressure:** Phase 0.1 must be done in 1-2 hours. Use simplest working solution:
   - Equal 20% allocation across top 5 pools by ROI
   - Don't over-optimize on first iteration
   - Get vote submitted, then build automation

2. **Wallet Security:** Phase 0.2 requires careful key management:
   - Never commit private keys to git
   - Use environment variables or encrypted keyfile
   - Consider using a dedicated voting wallet with limited funds
   - Test transaction simulation before actual execution

3. **RPC Reliability:** Monitor service depends on reliable RPC:
   - Have backup RPC endpoints configured
   - Implement retry logic
   - Alert if RPC becomes unresponsive

4. **Automation Testing:** Before going live:
   - Test in dry-run mode extensively
   - Verify gas estimation is reasonable
   - Ensure notification system works
   - Have manual override readily available

5. **What If Automation Fails?:**
   - Set alarm for manual check 1 hour before boundary
   - Have Phase 0.1 script ready as fallback
   - Monitor logs leading up to boundary

**ORIGINAL NOTES:**

1. **Gas Costs:** Need to factor in transaction costs when comparing strategies. A 2% improvement in returns might not justify 5x gas costs.

2. **Price Data Quality:** Some tokens may never have reliable price data. Analysis must work with/without USD prices.

3. **Testing Strategy:** Consider creating a test database with synthetic data to validate analysis logic before running on production data.

4. **Backup Strategy:** Before any cleanup (Phase 1.1), ensure solid backups exist.

5. **Rate Limiting:** Phase 3.1 (price collection) must respect API limits. Consider spreading over days if needed.

6. **Validation:** Each phase should include validation against known-good results from earlier analysis.
