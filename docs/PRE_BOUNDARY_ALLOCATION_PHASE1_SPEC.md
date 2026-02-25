# Pre-Boundary Allocation Forecasting (Phase 1 Spec)

## Objective

Build a **pre-boundary decision framework** that recommends vote allocation at **T-5 min**, **T-1 min**, and **T-0 (1 block pre-boundary)** to maximize expected final reward, while controlling downside under uncertain late moves by other participants.

This phase is planning/spec only (no production coding yet).

---

## Implementation Status & Known Issues (Updated: 2026-02-23)

### Current Status

- P0-P5 complete: schema, collector, features/proxies, optimizer, backtest MVP operational
- Gauge-level diagnostics added to backtest.py with `--diagnostics` flag
- Command: `python3 -m analysis.pre_boundary.backtest --db-path data/db/preboundary_dev.db --recent-epochs 1 --diagnostics --diagnostics-limit 12`

### Active Bug: Suspected Data Leakage

**Symptom:**

- `votes_now_raw` and `rewards_now_usd` in `preboundary_snapshots` are effectively identical to final truth values across all gauges and decision windows (`day`, `T-1`, `boundary`)
- This causes unrealistic backtest performance as the model "predicts" values it should not yet know
- `portfolio_return_bps` is repeated per gauge (schema-expected but confusing in diagnostics)

**Suspected Root Cause:**
Snapshot materialization in `fetch_preboundary_snapshots.py` or `preboundary_store.py` may not properly filter by decision timestamp/block, instead using boundary-final state for all windows.

**Investigation Priority:**

1. Validate snapshot construction truly uses state observable at `decision_timestamp` / `decision_block`, not boundary-final state
2. SQL proof: compare snapshot vs truth match-rate per gauge to quantify leakage
3. Add scenario-level diagnostics showing per-gauge drift/uplift inputs and per-scenario estimates (conservative/base/aggressive)

**Next Steps (for continuation):**

- Audit `fetch_preboundary_snapshots.py` and `preboundary_store.py` time-filtering logic
- Add scenario decomposition diagnostics: per-gauge `drift_estimate`, `uplift_estimate`, `votes_final_est`, `rewards_final_est`, marginal returns per scenario
- Split diagnostics into: (a) gauge-level inputs, (b) window-level portfolio outputs, (c) scenario decomposition
- Rerun backtest on latest epoch and identify where optimistic bias enters

### Debugging Decision Tree: Why Expected ≠ Actual?

**Step 1: Quantify snapshot accuracy**

```sql
-- Per gauge/window, compute match rate
SELECT decision_window,
       AVG(ABS(s.votes_now_raw - t.final_votes_raw) / NULLIF(t.final_votes_raw, 0)) AS votes_error_pct,
       AVG(ABS(s.rewards_now_usd - t.final_rewards_usd) / NULLIF(t.final_rewards_usd, 0)) AS rewards_error_pct
FROM preboundary_snapshots s
JOIN preboundary_truth_labels t USING (epoch, gauge_address)
GROUP BY decision_window;
```

- If error < 1% → **leakage confirmed**, proceed to Step 2
- If error > 10% → snapshots are correctly forward-looking; bug is elsewhere

**Step 2: Check if drift/uplift amplify perfect snapshots**

```sql
-- Per gauge, compare snapshot → forecast → truth
SELECT gauge_address, decision_window,
       votes_now_raw, votes_final_conservative, votes_final_base, final_votes_raw,
       rewards_now_usd, rewards_final_conservative, rewards_final_base, final_rewards_usd
FROM preboundary_snapshots s
JOIN preboundary_forecasts f USING (epoch, decision_window, gauge_address)
JOIN preboundary_truth_labels t USING (epoch, gauge_address)
LIMIT 10;
```

- If `votes_now ≈ truth` but `votes_final_base > truth` → **drift/uplift adding bias to perfect data**
- If `votes_now ≈ truth` AND `votes_final_base ≈ truth` → bug is in optimizer/return calculation (Step 3)

**Step 3: Validate marginal return formula**
Check optimizer's per-gauge return calculation:

```python
# Expected per scenarios.py or optimizer.py
marginal_return = (rewards_final_est * allocation) / (votes_final_est + allocation)
```

Common bugs:

- Forgetting to add `allocation` to denominator
- Using wrong scenario (conservative/base/aggressive) in objective
- Portfolio return not summing gauge returns correctly

**Step 4: Check if problem is per-gauge or portfolio-level**

- Print per-gauge: `expected_return_gauge`, `actual_return_gauge` (both in same units)
- Print portfolio: sum of above vs reported portfolio expected/actual
- If per-gauge accurate but portfolio wrong → aggregation bug
- If per-gauge already wrong → formula bug at gauge level

---

## 1) Decision Problem Definition

At decision time `t ∈ {T-5, T-1, T-0}` choose allocation vector `x` over candidate gauges/pools:

- `x_i >= 0`
- `sum_i x_i = V` (your total voting power)
- optional constraints: `x_i >= min_votes`, `num_nonzero(x) <= K_max`

Maximize a risk-aware objective:

- Primary: maximize `E[Return(x)]`
- Risk control: maximize `E[Return(x)] - λ * Downside(x)`
- Downside metrics to support: `P10(Return)`, CVaR-like tail proxy, regret vs hindsight best.

---

## 2) Known vs Unknown at Decision Time

### Known / Observable

- Current per-gauge vote denominator proxy (`weightsAt`/latest available vote state)
- Current visible bribe/reward state per token/contract
- Your voting power and execution constraints
- Historical late-window behavior (last-minute vote shifts, late bribes)

### Unknown / Uncertain

- Competitor reallocations after decision time
- Bribes added very late (after snapshot time)
- Final inclusion timing and ordering near boundary (especially at T-0)

Critical uncertainty: **final per-gauge denominator at boundary**.

---

## 3) Forecast Targets (what we estimate)

For each candidate gauge `i`, at each decision time `t`:

1. `Votes_final_i | t` (final denominator at boundary)
2. `Rewards_final_i | t` (final total rewards at boundary)
3. `InclusionProb(t)` for your transaction landing before boundary

Then compute scenario returns:

`Return_i(x_i) = Rewards_final_i * x_i / (Votes_final_i + x_i)`

Portfolio return is sum across selected gauges.

---

## 4) Proxy Stack (practical, robust)

### A. Vote-denominator proxy

`Votes_final_i = Votes_now_i * (1 + Drift_i,t)`

- `Drift_i,t` learned from historical late windows by gauge (or cluster fallback)
- Use quantiles, not only mean (e.g. p25/p50/p75 drift)

### B. Reward proxy

`Rewards_final_i = Rewards_now_i * (1 + Uplift_i,t)`

- `Uplift_i,t` from historical late-bribe additions
- Include token-level fallback where gauge history is sparse

### C. Execution proxy

- `InclusionProb(t)` estimated from your observed send-to-inclusion delays, gas strategy, and boundary congestion behavior.
- At T-0, expected return should be multiplied by inclusion probability.

### D. Scenario model

For each gauge/time, construct 3 scenarios:

- Conservative: high denominator drift, low reward uplift
- Base: median drift/uplift
- Aggressive: low denominator drift, high reward uplift

Use weighted scenario objective for optimization.

---

## 5) Data Constraints and Fallback Rules

1. **Snapshot latency/granularity**
   - Preferred: block-level near boundary
   - Fallback: minute bucket snapshots with known timestamp offset

2. **Coverage gaps**
   - Missing token price/reward metadata: use last valid price with age cap, else exclude/penalize gauge
   - Sparse gauge history: back off to cluster-level priors

3. **Timestamp alignment**
   - Enforce explicit mapping among decision timestamp, vote epoch, and boundary block
   - Log all resolved timestamps used in each decision

4. **Regime stability**
   - Detect unstable gauges (high variance drift) and apply confidence penalty

---

## 6) Phase 1 Validation Protocol (offline backtest)

Backtest over historical epochs for each decision window (`T-5`, `T-1`, `T-0`):

- Generate recommendation using only information available at that decision timestamp
- Compare against realized boundary outcome
- Report:
  - Expected vs realized return
  - Median and P10 return
  - Regret vs hindsight-optimal allocation
  - Calibration of scenario intervals

Acceptance criteria for moving to implementation:

- Positive median uplift vs baseline strategy
- Controlled downside (P10 not materially worse than baseline)
- Stable performance across multiple epochs, not one-off outliers

---

## 7) Repo Organization Proposal (new functionality)

Keep this separate from existing boundary collector/analyzer:

- `analysis/pre_boundary/`
  - `features.py` (build snapshot features at T-5/T-1/T-0)
  - `proxies.py` (drift/uplift estimators + fallback logic)
  - `scenarios.py` (conservative/base/aggressive generation)
  - `optimizer.py` (risk-aware allocation solver)
  - `backtest.py` (historical simulation and metrics)

- `data/fetchers/`
  - `fetch_preboundary_snapshots.py` (time-window snapshot collector)

- `src/`
  - `preboundary_recommender.py` (runtime orchestration entrypoint)

- `docs/`
  - this spec + runbook + metric definitions

- `data/db/` (new tables)
  - `preboundary_snapshots`
  - `preboundary_forecasts`
  - `preboundary_recommendations`
  - `preboundary_backtest_results`

---

## 8) 2am Operational Infra Plan (Mac mini standby-safe)

For unattended overnight runs, assume machine may sleep unless prevented.

### Required controls

1. **Prevent sleep during run**
   - Launch via `caffeinate -dimsu` wrapping the full command.

2. **Durable logs in repo/workspace (not /tmp)**
   - Write logs under `data/db/` or `logs/` to survive reboot/session resets.

3. **Checkpointing/idempotency**
   - Every epoch/window write must be resumable (`UPSERT`, skip completed partitions).

4. **Health heartbeat**
   - Append periodic progress line with timestamp, current epoch/window, rows written.

5. **Auto-resume command**
   - Use resume loop that checks DB completeness before fetching each partition.

### Night run pattern

- Start command under `caffeinate`
- Persist log file in workspace
- In morning, verify completeness by counting expected epochs/windows and row totals

### Nice-to-have (Phase 2 infra)

- Add `launchd` job for scheduled 2am runs
- Add retry policy (e.g., per-epoch up to N retries)
- Add summary notification on completion/failure

---

## 9) Immediate Next Step (before coding)

Finalize and approve:

1. scenario weights (conservative/base/aggressive)
2. risk penalty `λ`
3. minimum data quality gates (what to exclude)
4. target decision windows (`T-5`, `T-1`, `T-0`) and exact timestamp definitions

Once approved, implementation starts with `fetch_preboundary_snapshots.py` + `analysis/pre_boundary/features.py` and a minimal offline backtest loop.

---

## 10) Implementation Checklist (Priority Order)

This is the recommended execution order for implementation, with each step gated by a concrete output.

### P0 — Lock design constants (must do first)

1. Finalize scenario weights: `w_conservative`, `w_base`, `w_aggressive`.
2. Finalize risk penalty `λ` and max pools `K_max`.
3. Finalize data quality gates (price staleness cap, minimum history depth, exclusion rules).
4. Finalize exact timestamp semantics for `T-5`, `T-1`, `T-0`.

**Exit criteria:** single approved config block (checked into repo, e.g. in `config/settings.py` or dedicated pre-boundary config module).

### P0 Default Constants (MVP Baseline)

Use the following defaults unless explicitly overridden:

#### Scenario weights

- `w_conservative = 0.25`
- `w_base = 0.50`
- `w_aggressive = 0.25`

Rationale: neutral center-weighted prior for first MVP; avoid overfitting optimistic tails.

#### Risk and allocation controls

- `lambda_risk = 0.20`
- `K_max = 5`
- `min_votes_per_pool = 50_000`
- `max_turnover_ratio_per_revote = 0.80` (avoid full churn unless strong edge)

#### No-change / rewrite guardrails

- `min_expected_uplift_bps_for_revote = 50` (0.50%)
- `min_expected_uplift_usd_for_revote = 25`
- `t0_high_risk_block_rewrite = true` if inclusion risk is `High` and uplift below either threshold.

#### Inclusion risk mapping (MVP heuristic)

- `Low`: expected inclusion probability `>= 0.95`
- `Med`: `0.80 <= p < 0.95`
- `High`: `p < 0.80`

#### Data quality gates

- `max_price_age_seconds = 3600` (1 hour)
- `min_gauge_history_epochs_for_gauge_level_model = 6`
- `min_cluster_history_epochs_for_cluster_fallback = 4`
- If none of the above are met, fallback to global priors + confidence penalty.

#### Confidence penalties

- `confidence_penalty_sparse_history = 0.10`
- `confidence_penalty_high_variance = 0.10`
- `confidence_penalty_stale_price = 0.15`

Total penalty is additive and capped at `0.30` for MVP.

#### Decision window semantics

- `day`: user-triggered intraday anchor vote (typically hours before boundary)
- `T-5`: 5 minutes before boundary timestamp
- `T-1`: 1 minute before boundary timestamp
- `T-0`: last observed block before boundary; submission only if risk gate passes

#### Timestamp / block resolution rules

1. Resolve boundary block from boundary timestamp.
2. Resolve `T-5` / `T-1` snapshots to nearest block at or before target timestamp.
3. Resolve `T-0` as `boundary_block - 1` where chain data permits; otherwise nearest prior block.
4. Persist both target timestamp and resolved block in output tables for auditability.

### P1 — Data foundation and schema (highest engineering priority)

1. Create DB tables:
   - `preboundary_snapshots`
   - `preboundary_forecasts`
   - `preboundary_recommendations`
   - `preboundary_backtest_results`
2. Add indexes for common keys: `(epoch, decision_time, gauge_address)` and `(epoch, decision_window)`.
3. Implement idempotent upsert helpers and completeness checks per epoch/window.

**Exit criteria:** schema migration runs cleanly; repeat runs do not duplicate rows; completeness query is reliable.

### P2 — Snapshot collector (`fetch_preboundary_snapshots.py`)

1. Collect raw state at `T-5`, `T-1`, `T-0` for each target epoch:
   - vote denominator proxy
   - visible reward state
   - timestamp/block metadata
2. Write heartbeat logs and durable run logs in workspace.
3. Add resume behavior: skip already complete `(epoch, decision_window)` partitions.

**Exit criteria:** collector can backfill N epochs and resume after interruption with no manual cleanup.

### P3 — Feature + proxy layer

1. Implement `analysis/pre_boundary/features.py`:
   - construct model-ready features from snapshot rows
   - attach data quality flags and confidence scores
2. Implement `analysis/pre_boundary/proxies.py`:
   - estimate vote drift quantiles (p25/p50/p75)
   - estimate reward uplift quantiles
   - apply fallback hierarchy (gauge → cluster → global)

**Exit criteria:** deterministic feature/proxy outputs for a fixed epoch set; no missing critical fields.

### P4 — Scenario engine + optimizer

1. Implement `analysis/pre_boundary/scenarios.py`:
   - build conservative/base/aggressive forecast sets.
2. Implement `analysis/pre_boundary/optimizer.py`:
   - optimize allocation with constraints and risk-adjusted objective.
3. Add guardrails: reject allocations violating min votes/max pools/inclusion assumptions.

**Exit criteria:** optimizer returns valid allocation for each decision window and logs objective decomposition.

### P5 — Offline backtest harness

1. Implement `analysis/pre_boundary/backtest.py` to:
   - replay decisions at `T-5`, `T-1`, `T-0`
   - compare expected vs realized outcomes
   - compute median, P10, regret, and calibration metrics
2. Persist results in `preboundary_backtest_results`.

**Exit criteria:** reproducible backtest report over historical epochs with no look-ahead leakage.

### P6 — Runtime recommender CLI

1. Implement `src/preboundary_recommender.py` for live decision support:
   - load latest snapshot
   - generate forecast scenarios
   - output recommended allocation + risk diagnostics
2. Add `--decision-window {T-5,T-1,T-0}` and `--risk-profile` flags.

**Exit criteria:** single command produces recommendation and diagnostics in <30s using cached data.

### P7 — 2am operations hardening

1. Add a caffeinated runner script (or task) for overnight execution.
2. Add automatic retry per partition (`N` retries with backoff).
3. Add morning verification command/report (expected vs actual partition counts).

**Exit criteria:** overnight run survives sleep/network interruptions and auto-resumes without data corruption.

---

## 11) Suggested 2-Week Execution Plan

- **Week 1:** P0 → P3 (config + schema + collector + features/proxies)
- **Week 2:** P4 → P7 (optimizer + backtest + runtime CLI + ops hardening)

If time is constrained, ship an MVP after P5 (offline recommendation quality validated) before live runtime CLI hardening.

---

## 12) Wednesday-Night MVP Plan (Time-boxed)

Target: have a usable MVP by Wednesday night that can produce actionable recommendations for multiple vote checkpoints in the same epoch.

### Scope included in MVP

1. Snapshot ingestion for `T-5`, `T-1`, `T-0` windows.
2. Deterministic feature generation and proxy-based forecasts (conservative/base/aggressive).
3. Risk-aware allocation output for each window.
4. Offline backtest summary over available historical epochs.
5. Runbook command to generate day-time + near-boundary recommendation set.

### Scope deferred until after MVP

1. Full launchd scheduling automation.
2. Advanced retry orchestration service.
3. Extended calibration dashboards.

### Delivery schedule (Mon → Wed)

- **Monday (today):** finalize P0 constants and finish P1 schema + completeness checks.
- **Tuesday:** implement P2 collector and P3 feature/proxy layer end-to-end.
- **Wednesday:** implement P4 optimizer + minimal P5 backtest, produce MVP run command and validation report.

### MVP acceptance criteria

1. Command returns recommendation set for all windows (`day`, `T-5`, `T-1`, `T-0`) in under 60 seconds from cached data.
2. Recommendation output includes expected return, downside metric, and confidence flag.
3. Backtest report prints median return, P10, and regret vs hindsight baseline.

---

## 13) Multi-Vote Execution Policy (Resilience)

Because multiple votes are allowed, the strategy should explicitly use staged submissions to reduce execution risk.

### Policy

1. Submit a **daytime anchor vote** (baseline protection).
2. Recompute and submit at **T-5**.
3. Recompute and submit at **T-1**.
4. Attempt final update at **T-0** (1 block pre-boundary).

### Why this is important

- If `T-0` fails, `T-1` still stands.
- If `T-1` fails, `T-5` still stands.
- If all late attempts fail, daytime anchor prevents total miss.

### MVP output requirement for this policy

For each window, output:

1. Allocation recommendation.
2. Delta from prior submitted allocation (so only changes are executed).
3. Inclusion risk indicator (Low/Med/High).
4. "No-change" guardrail if expected uplift is below a configurable threshold.

### Operational guardrails

1. Keep a persisted record of "last successfully submitted allocation".
2. At each window, compute **incremental change set** instead of full rewrite when possible.
3. Abort late rewrite when inclusion risk is high and expected uplift is marginal.

---

## 14) P2 Data Points and Integration with Existing Pipeline

To avoid duplicating work, P2 should collect only **pre-boundary observable state** and reuse the existing post-boundary pipeline as truth labels.

### A. What P2 must collect (features at decision time)

Per `(epoch, decision_window, gauge)` collect:

1. Identity/context
   - `epoch`, `decision_window`, `decision_timestamp`, `decision_block`
   - `boundary_timestamp`, `boundary_block`
   - `gauge_address`, `pool_address`

2. Vote state now (denominator proxy)
   - `votes_now_raw` from `VoterV5.weightsAt(pool_address, vote_epoch)` queried at each decision block (`day`, `T-1`, `boundary`)
   - optional near-term flow features (if available): `votes_delta_60m`, `votes_delta_15m`, `votes_delta_5m`

3. Reward state now
   - `rewards_now_usd` (canonical) from `Bribe.rewardData(reward_token, vote_epoch).rewardsPerEpoch` queried at each decision block and converted to USD.
   - Optional diagnostic residual (not canonical reward pot):
     - `residual_usd ~= ERC20.balanceOf(bribe_contract) - rewardsPerEpoch` per token,
     - useful for carry-over/undistributed monitoring only,
     - do **not** use residual balance as allocatable epoch reward.
   - reward metadata quality flags (e.g., stale price, missing token decimals)

4. Execution and quality
   - `inclusion_prob` (MVP heuristic)
   - `data_quality_score`
   - `source_tag`, `computed_at`

### B. What P2 should NOT collect

1. Do not compute final realized rewards/votes in P2.
2. Do not duplicate post-boundary fetch logic already handled by `fetch_boundary_snapshots.py` and related tables.

### C. Truth labels (reuse existing tables)

Truth labels for backtest/calibration are materialized from:

1. `boundary_gauge_values` → `final_votes_raw`
2. `boundary_reward_snapshots` (SUM by gauge) → `final_rewards_usd`

Stored in:

- `preboundary_truth_labels(epoch, vote_epoch, gauge_address, final_votes_raw, final_rewards_usd, source_tag, computed_at)`

### D. Join keys and alignment contract

1. Snapshot key: `(epoch, decision_window, gauge_address)`
2. Truth key: `(epoch, vote_epoch, gauge_address)`
3. Alignment rule:
   - `epoch` = boundary epoch being predicted
   - `vote_epoch` = epoch used by boundary collector for `weightsAt/rewardData`

### E. Why this integration is correct

1. Pre-boundary model uses only information available at decision time.
2. Realized outcomes are sourced from the same post-boundary pipeline already in production.
3. Backtest quality improves without introducing a second competing source of truth.
