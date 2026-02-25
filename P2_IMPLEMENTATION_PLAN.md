# P2 Collector Implementation Plan (Approved Design)

**Target:** MVP by Wednesday night  
**Approved decisions:**

1. Retrospective backtest using boundary tables ✅
2. Data sources: boundary_gauge_values + boundary_reward_snapshots ✅
3. Defer price staleness to P3 ✅
4. Filter: `total_usd >= 100` per gauge ✅
5. Decision windows: `day`, `T-1`, `boundary` (drop T-5) ✅

---

## File Dependency DAG

```
config/preboundary_settings.py (new)
    ⬅ P0 defaults + data quality gates + resume config
    ⬇
src/preboundary_store.py (additions)
    ⬅ New materialization functions
    ⬇ (existing upsert_truth_labels_from_boundary already done)
data/fetchers/fetch_preboundary_snapshots.py (new)
    ⬅ Orchestrator; uses store functions
    ⬇
analysis/pre_boundary/__init__.py (new, stub)
    ⬇
analysis/pre_boundary/validate_snapshots.py (new, optional for sanity checks)
    ⬅ Uses truth labels to validate snapshot quality
```

**Parallel work (no dependencies):**

- Config defaults finalization
- Store helper functions
- Validator setup

**Sequential work:**

- Config → Store → Fetcher → Validator

---

## Detailed Spec: Each File

### 1) `config/preboundary_settings.py` (NEW)

**Purpose:** Centralize all P0 defaults + data quality gates + resume/logging config.

**Dependencies:** None

**Scope:**

```python
# Decision window timestamp definitions
DECISION_WINDOWS = {
    "day": {"seconds_before_boundary": 86400, "description": "24 hours before boundary"},
    "T-1": {"seconds_before_boundary": 60, "description": "1 min before boundary"},
    "boundary": {"seconds_before_boundary": 0, "description": "at boundary timestamp"},
}

# Scenario weights (from P0 defaults)
SCENARIO_WEIGHTS = {
    "conservative": 0.25,
    "base": 0.50,
    "aggressive": 0.25,
}

# Risk & allocation (from P0 defaults)
LAMBDA_RISK = 0.20
K_MAX = 5
MIN_VOTES_PER_POOL = 50_000
MAX_TURNOVER_RATIO_PER_REVOTE = 0.80

# Guardrails (from P0 defaults)
MIN_EXPECTED_UPLIFT_BPS_FOR_REVOTE = 50
MIN_EXPECTED_UPLIFT_USD_FOR_REVOTE = 25
T0_HIGH_RISK_BLOCK_REWRITE = True

# Inclusion risk mapping (from P0 defaults)
INCLUSION_RISK_THRESHOLDS = {
    "Low": 0.95,    # >= 0.95
    "Med": 0.80,    # >= 0.80 and < 0.95
    "High": 0.0,    # < 0.80
}

# Data quality gates (from P0 defaults)
MAX_PRICE_AGE_SECONDS = 3600
MIN_GAUGE_HISTORY_EPOCHS_FOR_GAUGE_LEVEL_MODEL = 6
MIN_CLUSTER_HISTORY_EPOCHS_FOR_CLUSTER_FALLBACK = 4

# Confidence penalties (from P0 defaults)
CONFIDENCE_PENALTY_SPARSE_HISTORY = 0.10
CONFIDENCE_PENALTY_HIGH_VARIANCE = 0.10
CONFIDENCE_PENALTY_STALE_PRICE = 0.15
CONFIDENCE_PENALTY_CAP = 0.30

# P2 Snapshot collection gates (NEW for MVP)
MIN_REWARD_TOTAL_USD_PER_GAUGE = 100.0    # Filter: exclude gauges < $100
MIN_GAUGES_PER_EPOCH = 20                 # Warn if < 20 gauges in epoch
MIN_REWARD_COVERAGE_RATIO = 0.70          # Warn if < 70% gauges have reward data

# Block time estimate for decision_block inference
BLOCK_TIME_ESTIMATE_SECONDS = 12

# Resume & logging
LOGGING_DIR = "data/db/logs"
PBOUNDARY_LOG_FILE = "data/db/logs/preboundary_collection.log"
HEARTBEAT_INTERVAL_ROWS = 50

# DB path (MVP uses preboundary_dev.db, deploy to data.db in P6)
PREBOUNDARY_DB_PATH = "data/db/preboundary_dev.db"
```

**Functions to export:**

- `get_decision_window(name: str) -> dict` — Returns window config
- `get_inclusion_risk_level(prob: float) -> str` — Maps probability to "Low"/"Med"/"High"
- `make_logging_dir() -> None` — Creates data/db/logs if missing

---

### 2) `src/preboundary_store.py` (ADDITIONS)

**Purpose:** Add snapshot materialization helpers. (Existing upsert functions already sufficient.)

**Existing exports:** ✅ `upsert_preboundary_snapshots()`, `upsert_truth_labels_from_boundary()`, etc.

**New functions to add:**

```python
def materialize_preboundary_snapshots_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    decision_windows: Sequence[str] = ("day", "T-5", "T-1", "T-0"),
    min_reward_usd: float = 100.0,
) -> Dict[str, List[Tuple]]:
    """
    Materialize snapshot rows for a single epoch across all decision windows.

    Reads from boundary_gauge_values + boundary_reward_snapshots.
    Reconstructs "state at decision time" from boundary state + heuristics.

    Returns:
        Dict[window_name] -> List[snapshot_row_tuples]
        Each row tuple:
          (epoch, decision_window, decision_timestamp, decision_block,
           boundary_timestamp, boundary_block, gauge_address, pool_address,
           votes_now_raw, rewards_now_usd, inclusion_prob, data_quality_score, source_tag)

    Filters:
      - Only gauges with boundary total_usd >= min_reward_usd
      - Only active_only=1 from boundary tables
      - Excludes zero/null votes/rewards

    Decision time calculation:
      - boundary_timestamp = (from boundary_gauge_values)
      - decision_timestamp = boundary_timestamp - window_seconds_before (per DECISION_WINDOWS)
      - decision_block ≈ boundary_block - (window_seconds_before // BLOCK_TIME_ESTIMATE)

    Votes proxy:
      - votes_now_raw = votes_raw from boundary_gauge_values (at boundary)

    Rewards proxy:
      - rewards_now_usd = SUM(boundary_reward_snapshots.total_usd) grouped by gauge

    Inclusion probability (MVP heuristic):
      - day: 0.95 (high confidence, no execution risk)
      - T-1: 0.85 (moderate execution risk, <2min window)
      - boundary: 0.70 (high execution risk, minimal window)

    Data quality score (MVP simple):
      - 1.0 if votes_now_raw > 0 AND rewards_now_usd > 0 AND has pool mapping
      - 0.8 if missing pool mapping
      - 0.5 otherwise (sparse data)
    """

def get_gauges_for_epoch_with_mapping(
    conn: sqlite3.Connection,
    epoch: int,
    active_only: int = 1,
) -> Dict[str, Tuple[str, str, str]]:
    """
    Fetch gauge list for epoch with pool/bribe mappings.

    Returns:
        Dict[gauge_address] -> (pool_address, internal_bribe, external_bribe)

    Uses gauge_bribe_mapping if available, else fallback to gauges table.
    """

def get_preboundary_epoch_snapshot_count(
    conn: sqlite3.Connection,
    epoch: int,
    decision_window: str,
) -> int:
    """
    Count existing snapshot rows for (epoch, decision_window).
    Used for resume/completeness checks.
    """

def get_incomplete_decision_windows(
    conn: sqlite3.Connection,
    epoch: int,
    expected_windows: Sequence[str] = ("day", "T-1", "boundary"),
) -> List[str]:
    """
    Return windows that have zero snapshot rows for epoch.
    Used by fetcher to decide which windows to materialize.
    """
```

**Entry point for testing:**

```python
# In test_preboundary_store.py or similar
if __name__ == "__main__":
    conn = sqlite3.connect("data/db/preboundary_dev.db")
    snapshots = materialize_preboundary_snapshots_for_epoch(conn, 1771372800)
    for window, rows in snapshots.items():
        print(f"{window}: {len(rows)} rows")
```

---

### 3) `data/fetchers/fetch_preboundary_snapshots.py` (NEW)

**Purpose:** Orchestrator for P2 collection. Entry point for backfilling historical epochs.

**Dependencies:**

- `src/preboundary_store.py` (materialization helpers)
- `config/preboundary_settings.py` (config)
- sqlite3, logging, argparse

**Scope:**

```python
#!/usr/bin/env python3
"""
P2 Collector: Materialize pre-boundary snapshot state from boundary tables.

MVP: Backfill historical epochs (offline backtest).
P6+: Add real-time T-1/boundary collection.

Usage:
  # Backfill 10 most recent epochs
  python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 10

  # Backfill specific epoch range
  python -m data.fetchers.fetch_preboundary_snapshots --start-epoch 1771372800 --end-epoch 1771977600

  # Resume incomplete collection
  python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 10 --resume

  # Check completeness for a single epoch
  python -m data.fetchers.fetch_preboundary_snapshots --check-epoch 1771372800
"""

def get_recent_boundary_epochs(
    conn: sqlite3.Connection,
    limit: int = 10,
) -> List[int]:
    """
    Fetch N most recent epochs from boundary_gauge_values.
    Returns sorted list [oldest, ..., newest].
    """

def collect_preboundary_snapshots_for_epoch(
    conn: sqlite3.Connection,
    epoch: int,
    resume: bool = True,
    log_file: Optional[TextIO] = None,
) -> Dict[str, int]:
    """
    Materialize + upsert snapshots for all decision windows in a single epoch.

    Args:
        conn: database connection
        epoch: boundary epoch to backfill
        resume: if True, skip windows already complete; if False, overwrite
        log_file: file handle for heartbeat logging

    Returns:
        Dict[window_name] -> rows_inserted

    Process:
      1. Check completeness: which windows already done?
      2. If resume=True, skip complete windows
      3. For each incomplete window:
         a. Materialize snapshot rows (calls store function)
         b. Upsert to preboundary_snapshots table
         c. Log heartbeat (epoch, window, count)
      4. After all windows done for epoch:
         a. Materialize + upsert truth labels (calls existing function)
         b. Log summary (epoch complete, total rows)
      5. Return row counts
    """

def collect_preboundary_batch(
    db_path: str,
    epochs: List[int],
    resume: bool = True,
    log_file: Optional[str] = None,
) -> Dict[int, Dict[str, int]]:
    """
    Backfill snapshots for multiple epochs.

    Args:
        db_path: path to preboundary database (e.g. data/db/preboundary_dev.db)
        epochs: list of boundary epochs to backfill
        resume: if True, skip complete epochs
        log_file: path to durable log file (created if missing)

    Returns:
        Dict[epoch] -> Dict[window] -> rows_inserted

    Process:
      1. Open log file (append mode, create if missing)
      2. Connect to db_path
      3. Ensure preboundary schema
      4. For each epoch:
         a. Log "Starting epoch X"
         b. Call collect_preboundary_snapshots_for_epoch()
         c. Log "Completed epoch X: summary"
      5. Print final summary (total epochs, total rows)
      6. Close log file
    """

def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Backfill pre-boundary snapshots")
    parser.add_argument("--recent-epochs", type=int, default=None, help="Backfill N most recent epochs")
    parser.add_argument("--start-epoch", type=int, default=None, help="Start epoch timestamp")
    parser.add_argument("--end-epoch", type=int, default=None, help="End epoch timestamp")
    parser.add_argument("--check-epoch", type=int, default=None, help="Check completeness for single epoch")
    parser.add_argument("--resume", action="store_true", help="Skip already complete partitions")
    parser.add_argument("--db-path", type=str, default="data/db/preboundary_dev.db", help="Database path")
    parser.add_argument("--log-file", type=str, default="data/db/logs/preboundary_collection.log", help="Log file")
    args = parser.parse_args()

    # Implement CLI logic:
    # - If --check-epoch: print status + return
    # - If --recent-epochs: fetch N epochs, call collect_preboundary_batch()
    # - If --start-epoch/--end-epoch: infer epoch range, call collect_preboundary_batch()
    # - Always use --resume by default (user must pass --no-resume to force overwrite)
```

**Key behaviors:**

- **Idempotency:** (epoch, decision_window, gauge_address) PK ensures upsert skips duplicates
- **Resume:** Checks completeness-per-window before materializing
- **Durable logging:** Appends to log file in workspace (survives session resets)
- **Heartbeat:** Log one line per window completion + epoch summary

**Testing entry point:**

```bash
# Dry-run on 1 small epoch
python -m data.fetchers.fetch_preboundary_snapshots --check-epoch 1771372800

# Backfill 5 recent
python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 5 --resume

# Full backfill (if time)
python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 15 --resume
```

---

### 4) `analysis/pre_boundary/__init__.py` (NEW, stub)

```python
"""
Pre-boundary analysis module.

Phase 2: snapshot collection (fetch_preboundary_snapshots.py)
Phase 3: feature + proxy layer (features.py, proxies.py) — TBD
Phase 4: scenario engine + optimizer (scenarios.py, optimizer.py) — TBD
Phase 5: offline backtest (backtest.py) — TBD
"""

__version__ = "0.1.0"
```

---

### 5) `analysis/pre_boundary/validate_snapshots.py` (OPTIONAL for MVP)

**Purpose:** Sanity-check snapshot quality against realized truth labels.

**Dependencies:**

- `src/preboundary_store.py`
- sqlite3

**Scope (minimal, test-only):**

```python
def snapshot_truth_coverage_report(
    conn: sqlite3.Connection,
    epoch: int,
) -> Dict[str, object]:
    """
    Compare snapshot rows vs truth label rows for an epoch.

    Returns:
        {
            "epoch": int,
            "snapshot_rows": int,
            "truth_rows": int,
            "coverage_ratio": float (0.0-1.0),
            "coverage_ok": bool (>= 0.80),
            "sample_snapshots": [(gauge, votes_now, rewards_now), ...] (min 5),
            "sample_truths": [(gauge, final_votes, final_rewards), ...] (min 5),
        }
    """

if __name__ == "__main__":
    # Simple test harness for validation
    conn = sqlite3.connect("data/db/preboundary_dev.db")
    report = snapshot_truth_coverage_report(conn, 1771372800)
    print(json.dumps(report, indent=2))
```

---

## Implementation Sequence (Tuesday → Wednesday)

### Tuesday A.M. (9–12)

1. **Create `config/preboundary_settings.py`** (30 min)
   - Copy defaults from P0 section of spec
   - Add helper functions (`get_decision_window()`, `get_inclusion_risk_level()`)
   - Test: `python -c "from config.preboundary_settings import *; print(LAMBDA_RISK)"`

2. **Add functions to `src/preboundary_store.py`** (60 min)
   - `materialize_preboundary_snapshots_for_epoch()` — 40 min (SQL + row construction)
   - `get_gauges_for_epoch_with_mapping()` — 10 min (existing table query)
   - `get_incomplete_decision_windows()` — 10 min (completeness query)
   - Test: Create small test script reading preboundary_dev.db, materialize 1 epoch

3. **Create `analysis/pre_boundary/__init__.py`** (5 min)

**Tuesday checkpoint (12:30):** All core helpers in place; can materialize snapshot rows.

### Tuesday P.M. (1–5)

4. **Implement `data/fetchers/fetch_preboundary_snapshots.py`** (120 min)
   - `get_recent_boundary_epochs()` — 10 min
   - `collect_preboundary_snapshots_for_epoch()` — 50 min (main materialization loop)
   - `collect_preboundary_batch()` — 30 min (batch orchestrator)
   - `main()` + CLI argument parsing — 30 min
   - Test: Backfill 3 recent epochs using `--recent-epochs 3 --resume`

**Tuesday checkpoint (5pm):** P2 collector working end-to-end. Backfill 3–5 epochs without errors.

### Wednesday A.M. (9–12)

5. **Full historical backfill** (60 min)
   - Run `python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 15 --resume`
   - Monitor log file; confirm all windows present for each epoch
   - Verify zero duplicates (re-run on same epoch, row count unchanged)

6. **Create `analysis/pre_boundary/validate_snapshots.py`** (30 min)
   - Implement `snapshot_truth_coverage_report()`
   - Run validation on 5 backfilled epochs
   - Confirm ≥80% coverage

7. **Summary report & documentation** (30 min)
   - Count total rows: `SELECT COUNT(*) FROM preboundary_snapshots`
   - Per-epoch coverage: `SELECT epoch, COUNT(DISTINCT decision_window) FROM preboundary_snapshots GROUP BY epoch`
   - Verify truth labels: `SELECT COUNT(*) FROM preboundary_truth_labels`
   - Document findings

**Wednesday checkpoint (12:30):** P2 complete. Data ready for P3 (features.py).

---

## Testing Strategy

### Unit Tests (Tuesday evening)

1. **`test_preboundary_store.py`**
   - `test_materialize_empty_epoch()` — verify empty/sparse epoch handling
   - `test_materialize_decision_windows()` — check all 3 windows present
   - `test_snapshot_row_schema()` — verify tuple structure matches table

2. **`test_fetch_preboundary_snapshots.py`**
   - `test_get_recent_boundary_epochs()` — correct query order
   - `test_collect_single_epoch_resume()` — upsert idempotency
   - `test_collect_batch()` — multiple epochs, no duplicates

### Integration Tests (Wednesday A.M.)

1. **Full backfill + validation**

   ```bash
   make test-preboundary-collection
   # OR manual:
   python -m data.fetchers.fetch_preboundary_snapshots --recent-epochs 10 --resume
   python analysis/pre_boundary/validate_snapshots.py 1771372800
   ```

2. **Completeness checks**

   ```sql
   -- All 3 windows present for N epochs?
   SELECT epoch, COUNT(DISTINCT decision_window) as windows_present
   FROM preboundary_snapshots
   GROUP BY epoch
   HAVING windows_present = 3;

   -- No duplicates?
   SELECT COUNT(*) FROM preboundary_snapshots;
   -- Re-run collector, count should be same

   -- Truth labels aligned?
   SELECT COUNT(*) FROM preboundary_truth_labels
   WHERE (epoch, gauge_address) IN (
     SELECT DISTINCT epoch, gauge_address FROM preboundary_snapshots
   );
   ```

---

## Git Workflow

```bash
# Monday PM: Create feature branch
git checkout -b feature/p2-collector

# Tuesday: Commit incrementally
git add config/preboundary_settings.py
git commit -m "P2: Add preboundary configuration defaults"

git add src/preboundary_store.py
git commit -m "P2: Add snapshot materialization helpers to preboundary_store"

git add data/fetchers/fetch_preboundary_snapshots.py analysis/pre_boundary/__init__.py
git commit -m "P2: Implement snapshot collector orchestrator"

# Wednesday: Final validation
git add analysis/pre_boundary/validate_snapshots.py
git commit -m "P2: Add snapshot quality validation utility"

git commit -m "P2: Backfill complete 15 epochs, 80%+ truth coverage"
git push origin feature/p2-collector
```

---

## Success Criteria (Wednesday night)

- [ ] `config/preboundary_settings.py` exists with all P0 defaults
- [ ] `src/preboundary_store.py` exports `materialize_preboundary_snapshots_for_epoch()`
- [ ] `data/fetchers/fetch_preboundary_snapshots.py` runs `--recent-epochs 15 --resume` without errors
- [ ] `preboundary_snapshots` table has ≥ 10 epochs × 3 windows = ≥ 30 partitions
- [ ] Total snapshots: ≥ (10 epochs × 50 gauges × 3 windows) = ≥ 1500 rows
- [ ] `preboundary_truth_labels` materialized + ≥ 80% coverage vs snapshots
- [ ] Zero duplicate rows on resume (idempotency verified)
- [ ] Log file in `data/db/logs/preboundary_collection.log` shows heartbeats + summary

---

## Hand-off to P3 (Thursday)

Once P2 complete, P3 features.py will:

1. Read from `preboundary_snapshots` (no P2 changes needed)
2. Join with `preboundary_truth_labels` for backtest target
3. Compute drift + uplift proxies (next phase)

**P2 code is frozen after Wednesday.** P3 works read-only against P2 output.
