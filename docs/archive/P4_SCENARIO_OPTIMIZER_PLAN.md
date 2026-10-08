# P4 Scenario Engine & Risk-Aware Optimizer Plan

**Phase:** P4 (Pre-Boundary Scenario Generation & Allocation Optimization)  
**Timeline:** Wednesday 1pm–3pm  
**Exit Criteria:** `preboundary_forecasts` table populated with recommended allocations for all epochs  
**Dependencies:** P3 complete (features + proxies cached)

---

## Overview

P4 transforms feature+proxy data (P3) into three economic scenarios (conservative/base/aggressive), then solves a risk-aware portfolio optimization problem to recommend vote allocations at each decision window.

### Architecture Flow

```
P3 features + cached proxies
    ↓
[scenarios.py] → build 3 forecast scenarios (drift/uplift quantiles)
    ↓
[optimizer.py] → solve risk-aware allocation problem
    ↓
preboundary_forecasts table (allocations + expected returns + risk metrics)
```

---

## File Specifications

### 1. `analysis/pre_boundary/scenarios.py` (NEW)

**Purpose:** Generate conservative/base/aggressive forecast scenarios using P3 proxy quantiles.

**Data Structures**

```python
from dataclasses import dataclass
from typing import Dict, List

@dataclass
class ForecastScenario:
    """Forecast scenario (conservative, base, aggressive)."""
    scenario_name: str  # "conservative" / "base" / "aggressive"
    gauge_address: str
    decision_window: str
    
    # Drift assumptions for this scenario
    vote_drift: float  # (p75 for conservative, p50 for base, p25 for aggressive)
    
    # Uplift assumptions for this scenario
    reward_uplift: float  # (p25 for conservative, p50 for base, p75 for aggressive)
    
    # Derived forecast state
    votes_final_estimate: float  # votes_now * (1 + vote_drift)
    rewards_final_estimate: float  # rewards_now * (1 + reward_uplift)
    
    # Metadata
    source: str  # proxy source ("gauge_level", "cluster_fallback", "global_fallback")
    confidence_penalty: float
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize for storage."""
        pass
```

**Function Signatures**

```python
def build_scenarios_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
    cache_dir: str = "data/preboundary_cache",
) -> Dict[str, List[ForecastScenario]]:
    """
    Build all 3 scenarios for a single decision window.
    
    Load cached proxies from P3.
    For each gauge/feature:
    - Conservative: drift_p75 (high dilution), uplift_p25 (low bribes)
    - Base: drift_p50, uplift_p50
    - Aggressive: drift_p25 (low dilution), uplift_p75 (high bribes)
    
    Return: Dict[scenario_name] → List[ForecastScenario]
    """
    pass

def compute_scenario_returns(
    features: List[Dict],
    scenarios: Dict[str, List[ForecastScenario]],
    voting_power: float = 1_000_000,  # default 1M votes
) -> Dict[str, Dict[str, float]]:
    """
    Compute per-gauge return for each scenario.
    
    Return formula:
    Return_i(x_i) = Rewards_final_i * x_i / (Votes_final_i + x_i)
    
    For baseline (x_i = 0):
    Baseline_return = 0
    
    For marginal 1-unit allocation:
    Marginal_return_i = Rewards_final_i / (Votes_final_i + 1)
    
    Args:
        features: List of feature dicts with votes_now_raw, rewards_now_usd
        scenarios: Dict[scenario_name] → List[ForecastScenario]
        voting_power: total voting power (for return scaling)
    
    Return: Dict[scenario_name] → Dict[gauge_address] → marginal_return_bps
    """
    pass

def validate_scenarios(
    scenarios_by_name: Dict[str, List[ForecastScenario]],
) -> Tuple[bool, List[str]]:
    """
    Validate scenario consistency and reasonableness.
    
    Checks:
    - All gauges present in all 3 scenarios
    - votes_final > 0, rewards_final >= 0
    - Conservative has higher votes_final than Base than Aggressive
    - Conservative has lower rewards_final than Base than Aggressive
    - No NaN/Inf values
    
    Return: (is_valid: bool, warnings: List[str])
    """
    pass
```

**Implementation Details**

- Load cached drift/uplift estimates from `data/preboundary_cache/` JSON files (from P3 compute_proxies.py)
- Conservative scenario: drift=p75 (high), uplift=p25 (low) — worst-case late moves
- Base scenario: drift=p50, uplift=p50 — median expectation
- Aggressive scenario: drift=p25 (low), uplift=p75 (high) — best-case late moves
- Compute final vote/reward estimates: `final = now * (1 + scenario_drift_or_uplift)`
- Validate ordering: conservative votes > base votes > aggressive votes (higher dilution = lower return potential)

---

### 2. `analysis/pre_boundary/optimizer.py` (NEW)

**Purpose:** Solve risk-aware portfolio optimization to recommend allocations.

**Algorithm Overview**

**Optimization Problem:**

Maximize risk-adjusted return:

$$\max_x \quad E[Return(x)] - \lambda_{risk} \cdot Downside(x)$$

Subject to:
- $\sum_i x_i = V$ (voting power constraint)
- $x_i \geq 0$ (non-negativity)
- $x_i \geq \text{MIN_VOTES_PER_POOL}$ (minimum per pool, if active)
- $|\{i : x_i > 0\}| \leq K_{MAX}$ (max pools constraint)

**Expected Return Computation:**

For each scenario $s$ and allocation $x$:
$$\text{Return}_s(x) = \sum_i \frac{\text{Rewards}_{final,i,s} \cdot x_i}{\text{Votes}_{final,i,s} + x_i}$$

Weighted return across scenarios:
$$E[Return(x)] = \sum_s w_s \cdot \text{Return}_s(x)$$

where $w_s$ are configured scenario weights (conservative=0.25, base=0.50, aggressive=0.25).

**Downside Metric:**

Use P10 (10th percentile) return as downside proxy:
- Sort returns across all 3 scenarios
- Downside = min(return_aggressive, return_conservative)
- Risk penalty = $\lambda_{risk}$ × (Base_return - Downside)

**Function Signatures**

```python
def optimize_allocation(
    features: List[Dict[str, Any]],
    scenarios: Dict[str, List[ForecastScenario]],
    voting_power: float = 1_000_000,
    lambda_risk: float = 0.20,
    k_max: int = 5,
    min_votes_per_pool: int = 50_000,
) -> Dict[str, Any]:
    """
    Solve risk-aware allocation optimization.
    
    Args:
        features: List of feature dicts from P3
        scenarios: Dict[scenario_name] → List[ForecastScenario] from scenarios.py
        voting_power: total voting power to allocate
        lambda_risk: risk penalty coefficient
        k_max: maximum number of pools to allocate to
        min_votes_per_pool: minimum votes per active pool
    
    Returns:
        {
            'allocation': Dict[gauge_address] → votes_allocated,
            'expected_return': float,
            'downside_return': float,
            'risk_adjustment': float,
            'num_gauges': int,
            'validation_warnings': List[str],
        }
    """
    pass

def compute_portfolio_return(
    allocation: Dict[str, float],  # gauge_address → votes
    forecast_scenario: List[ForecastScenario],
) -> Tuple[float, Dict[str, float]]:
    """
    Compute total portfolio return for a given scenario.
    
    Return: (total_return, per_gauge_returns)
    """
    pass

def apply_optimizer_guardrails(
    allocation: Dict[str, float],
    features: List[Dict],
    voting_power: float,
    k_max: int = 5,
    min_votes_per_pool: int = 50_000,
) -> Tuple[bool, List[str]]:
    """
    Validate allocation against guardrails:
    - sum(allocation) == voting_power
    - all(allocation >= 0)
    - all(allocation > 0 implies allocation >= min_votes_per_pool)
    - num_nonzero(allocation) <= k_max
    - all gauges in features
    
    Return: (is_valid: bool, warnings: List[str])
    """
    pass

def compute_downside_metrics(
    allocation: Dict[str, float],
    scenarios: Dict[str, List[ForecastScenario]],
) -> Dict[str, float]:
    """
    Compute downside risk metrics for allocation.
    
    Return:
    {
        'return_conservative': float,
        'return_base': float,
        'return_aggressive': float,
        'return_p10': float,  # min of 3 scenarios
        'return_weighted': float,  # scenario-weighted average
    }
    """
    pass
```

**Implementation Notes**

- Use scipy.optimize.minimize or linear programming solver (e.g., HiGHS via scipy)
- Objective function: `-E[Return] + λ_risk * Downside`
- Constraint encoding: sum = V, x >= 0, cardinality <= K_max
- For cardinality constraint: branch-and-bound or enumerate top-K candidates
- Handle edge case: if all rewards are zero, return zero allocation (no incentive)
- **MVP approach:** Use greedy allocation if optimization is slow:
  1. Rank gauges by marginal return per vote in base scenario
  2. Allocate greedily to top-K while respecting min_votes constraint

---

### 3. `analysis/pre_boundary/optimizer_runner.py` (NEW)

**Purpose:** Orchestrator CLI to run optimization for all epochs/windows and populate `preboundary_forecasts`.

**Function Signatures**

```python
def populate_forecasts_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    cache_dir: str = "data/preboundary_cache",
    voting_power: float = 1_000_000,
    log_file: Optional[str] = None,
) -> Dict[str, int]:
    """
    Run full P3→P4 pipeline for a single epoch:
    1. Load features (P3)
    2. Build scenarios
    3. Optimize allocation per window
    4. Populate preboundary_forecasts table
    
    Return: Dict[window] → rows_inserted
    """
    pass

def cli_main():
    """
    CLI entry point.
    
    Usage:
      python -m analysis.pre_boundary.optimizer_runner \
        --db-path data/db/preboundary_dev.db \
        --cache-dir data/preboundary_cache \
        --voting-power 1000000 \
        --recent-epochs 1 \
        --log-file data/db/logs/optimizer.log
    """
    pass
```

---

## Database Schema Update

**New Table: `preboundary_forecasts` (if not already created)**

```sql
CREATE TABLE IF NOT EXISTS preboundary_forecasts (
    epoch INTEGER NOT NULL,
    decision_window TEXT NOT NULL,
    gauge_address TEXT NOT NULL,
    
    -- Recommended allocation
    votes_recommended INTEGER,
    
    -- Scenario returns (per-gauge)
    return_conservative_bps INTEGER,  -- basis points
    return_base_bps INTEGER,
    return_aggressive_bps INTEGER,
    
    -- Portfolio returns (all gauges in allocation)
    portfolio_return_bps INTEGER,
    portfolio_downside_bps INTEGER,
    
    -- Risk metrics
    risk_adjustment_bps INTEGER,
    confidence_penalty REAL,
    
    -- Optimization state
    num_gauges_allocated INTEGER,
    was_constrained INTEGER,  -- 1 if K_max or min_votes binding
    optimizer_status TEXT,  -- "success" / "constrained" / "degenerate"
    
    -- Metadata
    source_tag TEXT,
    computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    PRIMARY KEY (epoch, decision_window, gauge_address)
);

CREATE INDEX idx_preboundary_forecasts_epoch_window
    ON preboundary_forecasts(epoch, decision_window);
```

---

## Integration Points

### Input (from P3)

- **Features:** `preboundary_snapshots` table (votes_now_raw, rewards_now_usd, inclusion_prob, data_quality_score)
- **Proxies:** Cached JSON files in `data/preboundary_cache/` (drift_p25/p50/p75, uplift_p25/p50/p75)

### Output (to P5 backtest & P6 runtime)

- **Table:** `preboundary_forecasts` (allocation recommendations + return estimates)
- **Artifacts:** Summary report of allocations per epoch/window

---

## Implementation Sequence

### Step 1: Create Scenario Builder (20 min)
- Load cached proxies from P3
- Build 3 scenarios per gauge/window
- Validate scenario consistency and monotonicity

### Step 2: Implement Optimizer (40 min)
- Define objective function (weighted return - risk penalty)
- Implement constraint enforcement
- Handle cardinality constraint (K_max)
- Add guardrail validation

### Step 3: Compute Returns (20 min)
- Implement portfolio return computation per scenario
- Downside metrics (P10, regret)
- Confidence penalty integration

### Step 4: Implement Orchestrator (20 min)
- Load features + scenarios
- Run optimization per window
- Upsert results into forecasts table
- Durable logging

### Step 5: Integration Test (15 min)
- Run full pipeline for 1 epoch
- Verify allocations + return estimates
- Check constraints satisfied
- Validate table population

---

## Success Criteria

1. **Scenarios:** All 3 scenarios built consistently for each gauge (conservative stricter bounds than base stricter than aggressive)
2. **Allocations:** Recommended allocations satisfy all constraints (sum = voting_power, min_votes, K_max)
3. **Returns:** Expected returns positive for base scenario across all epochs
4. **Downside:** P10 return non-negative (no catastrophic outcomes in conservative scenario)
5. **Guardrails:** All allocations pass validation checks
6. **Table:** `preboundary_forecasts` populated with non-zero allocations per epoch/window
7. **Execution:** Full pipeline < 30s per epoch

---

## Effort Estimate

- **Total time:** ~2 hours (Wed 1pm–3pm)
- Buffer for debugging: +30 min
- Ready for P5 backtest by 3:30pm

---

## Dependencies & Imports

```python
# Standard library
import sqlite3
from dataclasses import dataclass, asdict
from typing import Dict, List, Tuple, Optional, Any
import json
import logging
from pathlib import Path

# Third-party
import numpy as np
from scipy.optimize import minimize, linprog

# Workspace
from config.preboundary_settings import (
    SCENARIO_WEIGHTS,
    LAMBDA_RISK,
    K_MAX,
    MIN_VOTES_PER_POOL,
    CONFIDENCE_PENALTY_CAP,
)
from analysis.pre_boundary.features import build_snapshot_features
from analysis.pre_boundary.proxies import (
    VoteDriftEstimate,
    RewardUpliftEstimate,
)
from analysis.pre_boundary.compute_proxies import load_proxy_cache
```

---

## References

- **P0 Config:** `config/preboundary_settings.py` (SCENARIO_WEIGHTS, LAMBDA_RISK, K_MAX, MIN_VOTES_PER_POOL)
- **P3 Features:** `analysis/pre_boundary/features.py` (build_snapshot_features)
- **P3 Proxies:** `analysis/pre_boundary/proxies.py`, cached in `data/preboundary_cache/`
- **Optimization spec:** PRE_BOUNDARY_ALLOCATION_PHASE1_SPEC.md (Section 1–4)
