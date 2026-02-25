# P3 Feature + Proxy Layer Implementation Plan

**Target:** This phase prepares data for P4 optimizer  
**Entry point:** Read from `preboundary_snapshots` table  
**Exit point:** Write to `preboundary_forecasts` table  
**Scope:** Feature engineering + historical proxy estimation (drift/uplift)

---

## Overview: P3 Data Flow

```
preboundary_snapshots (P2 output)
    ⬇
    ├─→ analysis/pre_boundary/features.py
    │     └─→ Build features from snapshots
    │         (votes_now, rewards_now, inclusion_prob, data_quality_score)
    │
    ├─→ analysis/pre_boundary/proxies.py
    │     ├─→ Estimate vote drift (p25/p50/p75) per gauge + window
    │     ├─→ Estimate reward uplift (p25/p50/p75) per gauge + window
    │     ├─→ Apply fallback hierarchy (gauge → cluster → global)
    │     └─→ Compute confidence penalties
    │
    ├─→ analysis/pre_boundary/scenarios.py (P4)
    │     └─→ Build conservative/base/aggressive forecasts
    │
    └─→ preboundary_forecasts table (feed to optimizer)
```

---

## Phase 3 Architecture

### Core Concepts

1. **Features**: Observable state at decision time
   - `votes_now_raw`: Vote denominator from snapshot
   - `rewards_now_usd`: Visible rewards at decision time
   - `inclusion_prob`: Heuristic probability your vote lands pre-boundary
   - `data_quality_score`: Confidence in snapshot completeness

2. **Proxies**: Estimates of boundary state using historical patterns
   - `vote_drift`: Historical change in votes from decision time → boundary
   - `reward_uplift`: Historical late-added reward amount
   - Quantiles (p25, p50, p75) for scenario generation
   - Confidence penalties for sparse/unstable gauges

3. **Fallback Hierarchy**: When gauge history is sparse
   ```
   Gauge-level → Cluster-level → Global prior
   (6+ epochs)   (4+ epochs)    (always available)
   ```

---

## File 1: `analysis/pre_boundary/features.py` (NEW)

**Purpose:** Build feature vectors from snapshots  
**Dependencies:** `preboundary_snapshots` table, `config/preboundary_settings.py`

### Function Specifications

```python
def build_snapshot_features(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
    min_data_quality_score: float = 0.5,
) -> List[Dict[str, Any]]:
    """
    Load and prepare features from preboundary_snapshots for a single window.

    Args:
        conn: database connection
        epoch: boundary epoch
        decision_window: "day", "T-1", or "boundary"
        min_data_quality_score: filter out gauges below this quality threshold

    Returns:
        List[Dict] with keys:
          - epoch, decision_window, decision_timestamp, decision_block
          - boundary_timestamp, boundary_block
          - gauge_address, pool_address
          - votes_now_raw (FLOAT)
          - rewards_now_usd (FLOAT)
          - inclusion_prob (FLOAT)
          - data_quality_score (FLOAT)
          - source_tag (STR)
          - feature_computed_at (INT timestamp)

    Filters:
      - Only rows with data_quality_score >= min_data_quality_score
      - Only rows with votes_now_raw > 0 AND rewards_now_usd > 0
    """

def compute_feature_statistics(
    features: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Compute summary statistics over a feature set.

    Returns:
        {
            "num_gauges": int,
            "total_votes": float,
            "total_rewards_usd": float,
            "avg_data_quality_score": float,
            "avg_inclusion_prob": float,
            "missing_pool_count": int,
        }
    """

def validate_features(
    features: List[Dict[str, Any]],
    min_gauges: int = 10,
) -> Tuple[bool, str]:
    """
    Validate feature set for completeness.

    Returns:
        (is_valid: bool, message: str)

    Checks:
      - At least min_gauges rows
      - No NaN/null critical fields
      - Data quality trends reasonable
      - Timestamp alignment across all rows
    """
```

### Implementation Details

**No heavy computation here** — just data loading and validation. Raw snapshots are already clean from P2.

**Key pattern:**

```python
# Load raw snapshot
features = conn.execute(
    "SELECT * FROM preboundary_snapshots WHERE epoch=? AND decision_window=?",
    (epoch, window)
).fetchall()

# Convert to dicts for downstream consumption
features_with_metadata = [
    {
        **snapshot_row,
        "feature_computed_at": int(time.time()),
    }
    for snapshot_row in features
]

return features_with_metadata
```

---

## File 2: `analysis/pre_boundary/proxies.py` (NEW)

**Purpose:** Estimate drift/uplift from historical data + apply fallbacks  
**Dependencies:** `preboundary_snapshots`, `preboundary_truth_labels`, config

### Core Data Structures

```python
class VoteDriftEstimate:
    """Vote denominator drift from decision time to boundary."""
    gauge_address: str
    decision_window: str

    # Quantile estimates
    p25_drift: float  # (final_votes - votes_now) / votes_now
    p50_drift: float
    p75_drift: float

    # Metadata
    num_obs: int           # Number of historical observations
    model_source: str      # "gauge-level" | "cluster-level" | "global"
    confidence_score: float


class RewardUpliftEstimate:
    """Reward increase from decision time to boundary."""
    gauge_address: str
    decision_window: str
    reward_token: str

    # Quantile estimates (in USD)
    p25_uplift_usd: float
    p50_uplift_usd: float
    p75_uplift_usd: float

    # Metadata
    num_obs: int
    model_source: str
    confidence_score: float
```

### Function Specifications

```python
def learn_vote_drift_by_window(
    conn: sqlite3.Connection,
    decision_window: str,
    min_observations: int = 6,
) -> Dict[str, VoteDriftEstimate]:
    """
    Learn vote drift distribution from historical snapshots + truth labels.

    For each gauge, compute:
      drift_i = (final_votes_i - votes_now_i) / votes_now_i

    Then compute quantiles across epochs.

    Args:
        conn: database connection
        decision_window: "day", "T-1", or "boundary"
        min_observations: minimum epochs to fit gauge-level model

    Returns:
        Dict[gauge_address] → VoteDriftEstimate

    Fallback logic:
      - If gauge has >= min_observations: use gauge-level quantiles
      - Else cluster by reward_now_usd quartile: use cluster quantiles
      - Else use global quantiles across all gauges
    """

def learn_reward_uplift_by_window(
    conn: sqlite3.Connection,
    decision_window: str,
    min_observations: int = 6,
) -> Dict[str, RewardUpliftEstimate]:
    """
    Learn reward uplift distribution from historical data.

    For each gauge + token, compute:
      uplift_i = final_rewards_i - rewards_now_i

    Then compute quantiles.

    Similar fallback hierarchy as vote_drift.
    """

def compute_gauge_cluster(
    rewards_now_usd: float,
    num_clusters: int = 4,
) -> str:
    """
    Assign gauge to a cluster based on reward size.

    Returns cluster label: "low", "mid-low", "mid-high", "high"
    Used for fallback when gauge history is sparse.
    """

def apply_confidence_penalty(
    num_observations: int,
    variance_drift_p25_p75: float,
    data_quality_score: float,
) -> float:
    """
    Compute confidence penalty for proxy estimate.

    Args:
        num_observations: epochs used for estimation
        variance_drift_p25_p75: spread between p25 and p75
        data_quality_score: from snapshot (0.0-1.0)

    Returns:
        penalty: float in [0.0, CONFIDENCE_PENALTY_CAP]

    Penalties applied for:
      - Sparse history (< min_observations)
      - High variance (unstable patterns)
      - Low data quality (missing pool, stale prices, etc.)
    """

def get_vote_drift_estimate(
    gauge_address: str,
    decision_window: str,
    drift_by_gauge: Dict[str, VoteDriftEstimate],
    drift_global_fallback: Dict[str, float],  # p25, p50, p75
) -> VoteDriftEstimate:
    """
    Retrieve or compute vote drift estimate with fallback.

    Returns best available estimate:
      1. Gauge-level if available
      2. Global fallback if gauge missing
    """

def get_reward_uplift_estimate(
    gauge_address: str,
    reward_token: str,
    decision_window: str,
    uplift_by_gauge: Dict[str, RewardUpliftEstimate],
    uplift_token_fallback: Dict[str, float],
    uplift_global_fallback: Dict[str, float],
) -> RewardUpliftEstimate:
    """
    Retrieve reward uplift estimate with fallback hierarchy.

    Returns best available:
      1. Gauge-level if available
      2. Token-level fallback
      3. Global fallback
    """
```

### Implementation Strategy

**Compute all proxies offline per window** (this will be done once, then cached):

```python
# Pseudo-code flow
for window in ("day", "T-1", "boundary"):
    # Learn drift from historical data
    drift_estimates = learn_vote_drift_by_window(conn, window)

    # Learn uplift from historical data
    uplift_estimates = learn_reward_uplift_by_window(conn, window)

    # Store/cache in memory or separate table for P4 to consume
    cache[window] = {
        "drift": drift_estimates,
        "uplift": uplift_estimates,
    }
```

**For a single decision:** Attach proxies to features

```python
# At decision time, for each gauge in features:
for feature in features:
    gauge = feature["gauge_address"]
    window = feature["decision_window"]

    drift_est = get_vote_drift_estimate(gauge, window, ...)
    uplift_est = get_reward_uplift_estimate(gauge, ..., window, ...)

    feature["vote_drift_p50"] = drift_est.p50_drift
    feature["vote_drift_p25"] = drift_est.p25_drift
    feature["vote_drift_p75"] = drift_est.p75_drift
    feature["reward_uplift_p50_usd"] = uplift_est.p50_uplift_usd
    feature["confidence_penalty"] = compute_confidence_penalty(...)
```

---

## File 3: `analysis/pre_boundary/compute_proxies.py` (NEW, optional utility)

**Purpose:** One-time CLI to compute and cache proxy estimates  
**Use case:** Pre-compute proxies before running multiple epoch backtests

```python
#!/usr/bin/env python3
"""
Compute and cache vote drift + reward uplift proxies.

Usage:
  python -m analysis.pre_boundary.compute_proxies \
    --db-path data/db/preboundary_dev.db \
    --output-json analysis/pre_boundary/cached_proxies.json
"""

def main():
    """
    For each decision_window:
      1. Learn vote drift quantiles
      2. Learn reward uplift quantiles
      3. Compute global fallbacks

    Save to JSON for fast retrieval during backtests.
    """
```

---

## File 4: `analysis/pre_boundary/feature_validator.py` (NEW, test harness)

**Purpose:** Validate feature quality and proxy estimates  
**Entry point:** CLI for manual inspection

```python
#!/usr/bin/env python3
"""
Validate features and proxies for a specific epoch.

Usage:
  python -m analysis.pre_boundary.feature_validator \
    --epoch 1771372800 \
    --decision-window T-1 \
    --db-path data/db/preboundary_dev.db
"""

def validate_epoch_features(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
) -> Dict[str, Any]:
    """
    Run comprehensive validation and produce a report.

    Returns:
        {
            "features_loaded": int,
            "features_valid": int,
            "data_quality_distribution": {...},
            "drift_coverage": {gauge: (has_estimate, source)},
            "uplift_coverage": {gauge: (has_estimate, source)},
            "warnings": [...],
            "summary": "PASS" | "WARN" | "FAIL",
        }
    """
```

---

## Implementation Sequence (Wed morning → Wed afternoon)

### Wednesday A.M. (9–10:30)

1. **Create `analysis/pre_boundary/features.py`** (45 min)
   - `build_snapshot_features()` — load + filter snapshots
   - `compute_feature_statistics()` — summary stats
   - `validate_features()` — quality checks
   - Test on 1 epoch

### Wednesday A.M. (10:30–12)

2. **Create `analysis/pre_boundary/proxies.py`** (90 min)
   - `learn_vote_drift_by_window()` — compute historical drift
   - `learn_reward_uplift_by_window()` — compute historical uplift
   - `compute_gauge_cluster()` — clustering for fallback
   - `apply_confidence_penalty()` — confidence scoring
   - Fallback hierarchy helpers
   - Test on 1 epoch/window with realistic data

### Wednesday P.M. (1–2)

3. **Create `analysis/pre_boundary/compute_proxies.py`** (CLI, optional)
   - Cache proxies to JSON for fast retrieval
   - Useful for multiple backtest runs

### Wednesday P.M. (2–3)

4. **Create `analysis/pre_boundary/feature_validator.py`** (test harness)
   - Validation reports for manual inspection
   - Coverage metrics

### Wednesday P.M. (3–4)

5. **Integration test**
   - Run features.py + proxies.py end-to-end on 1 epoch
   - Produce sample output showing:
     - Raw features (27 gauges × 3 windows)
     - Drift/uplift estimates with fallback sources
     - Confidence penalties applied
     - Ready for P4 optimizer

---

## Success Criteria (Wed evening)

- [ ] `build_snapshot_features()` loads 81 rows (27 × 3 windows) from preboundary_snapshots
- [ ] `validate_features()` confirms all rows are quality >= 0.5, complete timestamps
- [ ] `learn_vote_drift_by_window()` computes drift quantiles (at least global fallback)
- [ ] `learn_reward_uplift_by_window()` computes uplift quantiles
- [ ] Confidence penalty applied (sparse gauges flagged)
- [ ] Feature → proxy pipeline runs <10s per epoch
- [ ] Sample output shows all 3 windows with drift/uplift p25/p50/p75 attached

---

## Data Structures for P4

After P3 completes, output should be ready for P4 optimizer:

```python
# Example feature with proxies attached (ready for P4)
feature_with_proxies = {
    # From snapshot (P2)
    "epoch": 1771372800,
    "decision_window": "T-1",
    "decision_timestamp": 1771372740,  # boundary_timestamp - 60
    "gauge_address": "0x008a...",
    "votes_now_raw": 42.5,
    "rewards_now_usd": 250.0,
    "inclusion_prob": 0.85,
    "data_quality_score": 1.0,

    # From proxies (P3)
    "vote_drift_p25": -0.05,      # votes might decrease 5%
    "vote_drift_p50": 0.02,       # or increase 2%
    "vote_drift_p75": 0.15,       # or increase 15%
    "reward_uplift_p25_usd": 0.0,
    "reward_uplift_p50_usd": 50.0,
    "reward_uplift_p75_usd": 150.0,
    "confidence_penalty": 0.05,   # only 5% penalty (good data)
    "drift_source": "gauge-level",
    "uplift_source": "gauge-level",
}

# P4 will:
# 1. For each scenario (conservative/base/aggressive):
#    final_votes = votes_now * (1 + drift_scenario)
#    final_rewards = rewards_now + uplift_scenario
# 2. Compute return: (final_rewards * allocation) / (final_votes + allocation)
# 3. Optimize allocation subject to constraints
```

---

## Integration Points

### With P2 (read-only):

- `preboundary_snapshots` → raw features
- `preboundary_truth_labels` → historical drift/uplift learning

### With P4 (output):

- Features + proxies → `preboundary_forecasts` (written by P4)
- Scenarios use proxy p25/p50/p75 for forecasting

### Config:

- `CONFIDENCE_PENALTY_*` constants from `config/preboundary_settings.py`
- `MIN_GAUGE_HISTORY_EPOCHS_FOR_GAUGE_LEVEL_MODEL` = 6 (fallback threshold)

---

## Q&A: P3 Design Decisions

**Q: Why compute proxies offline instead of live?**  
A: Historical drift/uplift is stable across epochs; computing once and caching is ~1000x faster than recomputing per epoch during backtest.

**Q: How do we handle gauges with <6 epochs of history?**  
A: Use cluster-level quantiles (mid-range gauges group together), then global fallback. Mark confidence penalty accordingly.

**Q: Why quantiles instead of mean?**  
A: Captures distribution shape. Conservative scenario uses p75 (worst-case drift), base uses p50, aggressive uses p25. Much more robust than mean ± std.

**Q: What if a token has missing price data?**  
A: Set uplift_usd to global median (or 0), mark data_quality_score accordingly, apply confidence penalty. P4 optimizer will de-weight this gauge.

---

## Ready to Code?

Approve this architecture and I'll implement:

1. `features.py` — load snapshots + validate
2. `proxies.py` — learn drift/uplift + fallbacks
3. `compute_proxies.py` — CLI to pre-compute
4. `feature_validator.py` — test harness
5. End-to-end test on 1 epoch

All should complete Wed afternoon, leaving P4 optimizer + P5 backtest for Wed evening.
