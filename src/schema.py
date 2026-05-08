"""
Canonical DDL for data/db/data.db.

All table and index creation is expressed as plain SQL strings here.
Use src/db.py:apply_schema() to initialise or migrate a database.

No SQLAlchemy.  No ORM.  Just sqlite3.
"""

# ---------------------------------------------------------------------------
# Schema version tracking
# ---------------------------------------------------------------------------

# Bump this when adding a new migration step below.
CURRENT_SCHEMA_VERSION = 4

SCHEMA_VERSION = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     INTEGER NOT NULL,
    applied_at  INTEGER NOT NULL,
    notes       TEXT
)
"""

# ---------------------------------------------------------------------------
# Migration steps
#
# Each entry is (target_version, description, sql_or_callable).
# apply_schema() in src/db.py runs any step whose target_version is greater
# than the version currently stored in schema_version.
#
# Rules:
#   - Never edit or remove an existing entry; only append new ones.
#   - sql_or_callable may be a plain SQL string or a callable(conn) for
#     multi-statement migrations.
#   - The initial schema creation (CREATE TABLE IF NOT EXISTS for all tables)
#     is handled separately and is NOT listed here — it is always idempotent.
# ---------------------------------------------------------------------------

MIGRATIONS: list[tuple[int, str, object]] = [
    # (1, "initial schema", None)  # version 1 = first apply_schema() run;
    #                              # no DDL change needed beyond table creation
    (2, "add actual_epoch_rewards table", None),  # DDL added to ALL_TABLES; IF NOT EXISTS handles creation
    (3, "add epoch_pool_realisation view", None),   # DDL added to ALL_VIEWS; IF NOT EXISTS handles creation
    (4, "add preboundary analysis tables", None),   # DDL added to ALL_TABLES; IF NOT EXISTS handles creation
]

# ---------------------------------------------------------------------------
# Core lookup / reference tables
# ---------------------------------------------------------------------------

EPOCH_BOUNDARIES = """
CREATE TABLE IF NOT EXISTS epoch_boundaries (
    epoch               INTEGER NOT NULL PRIMARY KEY,
    boundary_block      INTEGER NOT NULL,
    boundary_timestamp  INTEGER NOT NULL,
    vote_epoch          INTEGER NOT NULL,
    reward_epoch        INTEGER NOT NULL,
    source_tag          TEXT,
    computed_at         INTEGER NOT NULL
)
"""

GAUGE_BRIBE_MAPPING = """
CREATE TABLE IF NOT EXISTS gauge_bribe_mapping (
    gauge_address   TEXT PRIMARY KEY,
    internal_bribe  TEXT,
    external_bribe  TEXT,
    created_at      INTEGER NOT NULL
)
"""

BRIBE_REWARD_TOKENS = """
CREATE TABLE IF NOT EXISTS bribe_reward_tokens (
    bribe_contract  TEXT NOT NULL,
    reward_token    TEXT NOT NULL,
    is_reward_token INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (bribe_contract, reward_token)
)
"""

TOKEN_METADATA = """
CREATE TABLE IF NOT EXISTS token_metadata (
    token_address   TEXT NOT NULL PRIMARY KEY,
    symbol          TEXT,
    decimals        INTEGER,
    updated_at      INTEGER
)
"""

TOKEN_PRICES = """
CREATE TABLE IF NOT EXISTS token_prices (
    token_address   TEXT NOT NULL PRIMARY KEY,
    usd_price       REAL,
    updated_at      INTEGER
)
"""

HISTORICAL_TOKEN_PRICES = """
CREATE TABLE IF NOT EXISTS historical_token_prices (
    token_address   TEXT    NOT NULL,
    timestamp       INTEGER NOT NULL,
    granularity     TEXT    NOT NULL,
    usd_price       REAL,
    updated_at      INTEGER,
    PRIMARY KEY (token_address, timestamp, granularity)
)
"""

# ---------------------------------------------------------------------------
# Boundary snapshot tables (rewards and votes at / near epoch boundary)
# ---------------------------------------------------------------------------

BOUNDARY_REWARD_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS boundary_reward_snapshots (
    epoch               INTEGER NOT NULL,
    vote_epoch          INTEGER NOT NULL,
    active_only         INTEGER NOT NULL,
    boundary_block      INTEGER NOT NULL,
    gauge_address       TEXT    NOT NULL,
    bribe_contract      TEXT    NOT NULL,
    reward_token        TEXT    NOT NULL,
    rewards_raw         TEXT    NOT NULL,
    token_decimals      INTEGER,
    usd_price           REAL,
    total_usd           REAL    NOT NULL,
    computed_at         INTEGER NOT NULL,
    PRIMARY KEY (epoch, vote_epoch, active_only, bribe_contract, reward_token)
)
"""

BOUNDARY_REWARD_SAMPLES = """
CREATE TABLE IF NOT EXISTS boundary_reward_samples (
    epoch                   INTEGER NOT NULL,
    vote_epoch              INTEGER NOT NULL,
    active_only             INTEGER NOT NULL,
    boundary_block          INTEGER NOT NULL,
    query_block             INTEGER NOT NULL,
    blocks_before_boundary  INTEGER NOT NULL,
    gauge_address           TEXT    NOT NULL,
    bribe_contract          TEXT    NOT NULL,
    reward_token            TEXT    NOT NULL,
    rewards_raw             TEXT    NOT NULL,
    token_decimals          INTEGER,
    usd_price               REAL,
    total_usd               REAL    NOT NULL,
    computed_at             INTEGER NOT NULL,
    PRIMARY KEY (
        epoch, vote_epoch, active_only, blocks_before_boundary,
        bribe_contract, reward_token, gauge_address
    )
)
"""

BOUNDARY_GAUGE_VALUES = """
CREATE TABLE IF NOT EXISTS boundary_gauge_values (
    epoch           INTEGER NOT NULL,
    boundary_block  INTEGER NOT NULL,
    gauge_address   TEXT    NOT NULL,
    pool_address    TEXT    NOT NULL,
    votes_raw       TEXT    NOT NULL,
    total_usd       REAL    NOT NULL,
    computed_at     INTEGER NOT NULL,
    vote_epoch      INTEGER,
    active_only     INTEGER,
    PRIMARY KEY (epoch, boundary_block, gauge_address)
)
"""

BOUNDARY_VOTE_SAMPLES = """
CREATE TABLE IF NOT EXISTS boundary_vote_samples (
    epoch                   INTEGER NOT NULL,
    vote_epoch              INTEGER NOT NULL,
    active_only             INTEGER NOT NULL,
    boundary_block          INTEGER NOT NULL,
    query_block             INTEGER NOT NULL,
    blocks_before_boundary  INTEGER NOT NULL,
    gauge_address           TEXT    NOT NULL,
    pool_address            TEXT    NOT NULL,
    votes_raw               REAL    NOT NULL,
    total_usd               REAL    NOT NULL,
    computed_at             INTEGER NOT NULL,
    PRIMARY KEY (epoch, vote_epoch, active_only, blocks_before_boundary, gauge_address)
)
"""

# ---------------------------------------------------------------------------
# Live snapshot tables (intra-epoch monitoring)
# ---------------------------------------------------------------------------

LIVE_GAUGE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS live_gauge_snapshots (
    snapshot_ts                 INTEGER NOT NULL,
    query_block                 INTEGER NOT NULL,
    vote_epoch                  INTEGER NOT NULL,
    gauge_address               TEXT    NOT NULL,
    pool_address                TEXT    NOT NULL,
    is_alive                    INTEGER NOT NULL,
    votes_raw                   REAL    NOT NULL,
    rewards_raw_total           TEXT    NOT NULL,
    rewards_normalized_total    REAL    NOT NULL,
    computed_at                 INTEGER NOT NULL,
    PRIMARY KEY (snapshot_ts, gauge_address)
)
"""

LIVE_REWARD_TOKEN_SAMPLES = """
CREATE TABLE IF NOT EXISTS live_reward_token_samples (
    snapshot_ts         INTEGER NOT NULL,
    query_block         INTEGER NOT NULL,
    vote_epoch          INTEGER NOT NULL,
    gauge_address       TEXT    NOT NULL,
    bribe_contract      TEXT    NOT NULL,
    reward_token        TEXT    NOT NULL,
    rewards_raw         TEXT    NOT NULL,
    token_decimals      INTEGER,
    rewards_normalized  REAL    NOT NULL,
    computed_at         INTEGER NOT NULL,
    PRIMARY KEY (snapshot_ts, gauge_address, bribe_contract, reward_token)
)
"""

# ---------------------------------------------------------------------------
# Actual (post-epoch) reward records
# ---------------------------------------------------------------------------

ACTUAL_EPOCH_REWARDS = """
CREATE TABLE IF NOT EXISTS actual_epoch_rewards (
    epoch           INTEGER NOT NULL,
    symbol          TEXT    NOT NULL,
    token_address   TEXT,
    amount_tokens   REAL    NOT NULL,
    usd_price       REAL    NOT NULL,
    total_usd       REAL    NOT NULL,
    notes           TEXT,
    recorded_at     INTEGER NOT NULL,
    PRIMARY KEY (epoch, symbol)
)
"""

# ---------------------------------------------------------------------------
# Execution / tracking tables
# ---------------------------------------------------------------------------

AUTO_VOTE_RUNS = """
CREATE TABLE IF NOT EXISTS auto_vote_runs (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    initiated_at            INTEGER NOT NULL,
    execution_started_at    INTEGER,
    vote_sent_at            INTEGER,
    completed_at            INTEGER,
    status                  TEXT    NOT NULL,
    dry_run                 INTEGER NOT NULL,
    snapshot_ts             INTEGER,
    vote_epoch              INTEGER,
    query_block             INTEGER,
    selected_k              INTEGER,
    pool_count              INTEGER,
    expected_return_usd     REAL,
    tx_hash                 TEXT,
    error_text              TEXT,
    created_at              INTEGER NOT NULL DEFAULT (strftime('%s','now'))
)
"""

EXECUTED_ALLOCATIONS = """
CREATE TABLE IF NOT EXISTS executed_allocations (
    epoch           INTEGER NOT NULL,
    strategy_tag    TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    gauge_address   TEXT    NOT NULL,
    pool_address    TEXT    NOT NULL,
    executed_votes  INTEGER NOT NULL,
    source          TEXT    NOT NULL,
    tx_hash         TEXT,
    recorded_at     INTEGER NOT NULL,
    PRIMARY KEY (epoch, strategy_tag, gauge_address)
)
"""

PREDICTED_ALLOCATIONS = """
CREATE TABLE IF NOT EXISTS predicted_allocations (
    epoch           INTEGER NOT NULL,
    vote_epoch      INTEGER NOT NULL,
    snapshot_ts     INTEGER,
    query_block     INTEGER,
    strategy_tag    TEXT    NOT NULL,
    rank            INTEGER NOT NULL,
    gauge_address   TEXT    NOT NULL,
    pool_address    TEXT    NOT NULL,
    predicted_votes INTEGER NOT NULL,
    created_at      INTEGER NOT NULL,
    PRIMARY KEY (epoch, strategy_tag, gauge_address)
)
"""

ALLOCATION_PERFORMANCE_METRICS = """
CREATE TABLE IF NOT EXISTS allocation_performance_metrics (
    epoch           INTEGER NOT NULL,
    strategy_tag    TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    metric_value    REAL    NOT NULL,
    computed_at     INTEGER NOT NULL,
    notes           TEXT,
    PRIMARY KEY (epoch, strategy_tag, metric_name)
)
"""

CLAIM_SWAP_EXECUTION_LOG = """
CREATE TABLE IF NOT EXISTS claim_swap_execution_log (
    run_ts          INTEGER NOT NULL,
    epoch           INTEGER NOT NULL,
    phase           TEXT    NOT NULL,
    action_type     TEXT,
    token_address   TEXT,
    token_symbol    TEXT,
    bribe_count     INTEGER,
    token_count     INTEGER,
    amount_in_raw   TEXT,
    usd_value       REAL,
    slippage_pct    REAL,
    status          TEXT    NOT NULL,
    tx_hash         TEXT,
    error_text      TEXT,
    metadata_json   TEXT,
    PRIMARY KEY (run_ts, phase, action_type, token_address, tx_hash)
)
"""

# ---------------------------------------------------------------------------
# Legacy ORM-generated tables (kept for backward compatibility; read-only)
# ---------------------------------------------------------------------------

EPOCHS_LEGACY = """
CREATE TABLE IF NOT EXISTS epochs (
    timestamp       INTEGER NOT NULL PRIMARY KEY,
    total_votes     INTEGER,
    total_bribes_usd REAL,
    indexed_at      INTEGER
)
"""

BRIBES_LEGACY = """
CREATE TABLE IF NOT EXISTS bribes (
    id              INTEGER NOT NULL PRIMARY KEY,
    epoch           INTEGER,
    bribe_contract  TEXT,
    reward_token    TEXT,
    amount          REAL,
    timestamp       INTEGER,
    indexed_at      INTEGER,
    amount_wei      TEXT,
    gauge_address   TEXT,
    bribe_type      TEXT,
    token_symbol    TEXT,
    token_decimals  INTEGER,
    usd_price       REAL,
    usd_value       REAL
)
"""

VOTES_LEGACY = """
CREATE TABLE IF NOT EXISTS votes (
    id          INTEGER NOT NULL PRIMARY KEY,
    epoch       INTEGER,
    gauge       TEXT,
    total_votes REAL,
    indexed_at  INTEGER
)
"""

GAUGES_LEGACY = """
CREATE TABLE IF NOT EXISTS gauges (
    address         TEXT PRIMARY KEY,
    pool            TEXT,
    internal_bribe  TEXT,
    external_bribe  TEXT,
    is_alive        INTEGER,
    created_at      INTEGER,
    last_updated    INTEGER,
    current_votes   TEXT DEFAULT '0'
)
"""

HISTORICAL_ANALYSIS_LEGACY = """
CREATE TABLE IF NOT EXISTS historical_analysis (
    epoch               INTEGER NOT NULL PRIMARY KEY,
    optimal_return      REAL,
    naive_return        REAL,
    opportunity_cost    REAL,
    optimal_allocation  TEXT,
    analyzed_at         INTEGER
)
"""

# ---------------------------------------------------------------------------
# Indexes
# ---------------------------------------------------------------------------

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_epoch_boundaries_block ON epoch_boundaries(boundary_block)",
    "CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_epoch_block ON boundary_gauge_values(epoch, boundary_block)",
    "CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_epoch_vote_epoch_block ON boundary_gauge_values(epoch, vote_epoch, boundary_block)",
    "CREATE INDEX IF NOT EXISTS idx_boundary_gauge_values_lookup ON boundary_gauge_values(epoch, vote_epoch, active_only, total_usd DESC)",
    "CREATE INDEX IF NOT EXISTS idx_boundary_reward_samples_lookup ON boundary_reward_samples(epoch, vote_epoch, active_only, blocks_before_boundary)",
    "CREATE INDEX IF NOT EXISTS idx_boundary_vote_samples_lookup ON boundary_vote_samples(epoch, vote_epoch, active_only, blocks_before_boundary)",
    "CREATE INDEX IF NOT EXISTS idx_executed_allocations_epoch ON executed_allocations(epoch, strategy_tag)",
    "CREATE INDEX IF NOT EXISTS idx_predicted_allocations_epoch ON predicted_allocations(epoch, strategy_tag)",
    "CREATE INDEX IF NOT EXISTS idx_allocation_performance_epoch ON allocation_performance_metrics(epoch, strategy_tag)",
    "CREATE INDEX IF NOT EXISTS idx_live_gauge_snapshots_lookup ON live_gauge_snapshots(snapshot_ts, vote_epoch, rewards_normalized_total DESC)",
    "CREATE INDEX IF NOT EXISTS idx_live_reward_token_samples_lookup ON live_reward_token_samples(snapshot_ts, vote_epoch, gauge_address)",
    "CREATE INDEX IF NOT EXISTS ix_bribes_epoch ON bribes(epoch)",
    "CREATE INDEX IF NOT EXISTS ix_bribes_bribe_contract ON bribes(bribe_contract)",
    "CREATE INDEX IF NOT EXISTS ix_votes_epoch ON votes(epoch)",
    "CREATE INDEX IF NOT EXISTS ix_votes_gauge ON votes(gauge)",
]

# ---------------------------------------------------------------------------
# Pre-boundary analysis tables (migrated from preboundary_dev.db in v4)
# ---------------------------------------------------------------------------

PREBOUNDARY_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS preboundary_snapshots (
    epoch               INTEGER NOT NULL,
    decision_window     TEXT    NOT NULL,
    decision_timestamp  INTEGER NOT NULL,
    decision_block      INTEGER NOT NULL,
    boundary_timestamp  INTEGER NOT NULL,
    boundary_block      INTEGER,
    gauge_address       TEXT    NOT NULL,
    pool_address        TEXT,
    votes_now_raw       REAL    NOT NULL,
    rewards_now_usd     REAL    NOT NULL,
    inclusion_prob      REAL,
    data_quality_score  REAL,
    source_tag          TEXT,
    computed_at         INTEGER NOT NULL,
    PRIMARY KEY (epoch, decision_window, gauge_address)
)
"""

PREBOUNDARY_TRUTH_LABELS = """
CREATE TABLE IF NOT EXISTS preboundary_truth_labels (
    epoch           INTEGER NOT NULL,
    vote_epoch      INTEGER NOT NULL,
    gauge_address   TEXT    NOT NULL,
    final_votes_raw REAL    NOT NULL,
    final_rewards_usd REAL  NOT NULL,
    source_tag      TEXT,
    computed_at     INTEGER NOT NULL,
    PRIMARY KEY (epoch, vote_epoch, gauge_address)
)
"""

PREBOUNDARY_FORECASTS = """
CREATE TABLE IF NOT EXISTS preboundary_forecasts (
    epoch                   INTEGER NOT NULL,
    decision_window         TEXT    NOT NULL,
    gauge_address           TEXT    NOT NULL,
    votes_recommended       INTEGER,
    portfolio_return_bps    INTEGER,
    portfolio_downside_bps  INTEGER,
    optimizer_status        TEXT,
    source_tag              TEXT,
    computed_at             INTEGER NOT NULL,
    PRIMARY KEY (epoch, decision_window, gauge_address)
)
"""

PREBOUNDARY_RECOMMENDATIONS = """
CREATE TABLE IF NOT EXISTS preboundary_recommendations (
    epoch               INTEGER NOT NULL,
    decision_window     TEXT    NOT NULL,
    run_id              TEXT    NOT NULL,
    gauge_address       TEXT    NOT NULL,
    alloc_votes         REAL    NOT NULL,
    expected_return_usd REAL    NOT NULL,
    downside_p10_usd    REAL,
    inclusion_risk      TEXT,
    delta_votes         REAL,
    no_change_flag      INTEGER NOT NULL,
    computed_at         INTEGER NOT NULL,
    PRIMARY KEY (epoch, decision_window, run_id, gauge_address)
)
"""

PREBOUNDARY_BACKTEST_GAUGE_RESULTS = """
CREATE TABLE IF NOT EXISTS preboundary_backtest_gauge_results (
    epoch                   INTEGER NOT NULL,
    decision_window         TEXT    NOT NULL,
    gauge_address           TEXT    NOT NULL,
    votes_recommended       INTEGER,
    final_votes             REAL,
    final_rewards_usd       REAL,
    expected_return_bps     INTEGER,
    realized_return_bps     INTEGER,
    forecast_error_bps      INTEGER,
    is_allocated            INTEGER,
    computed_at             INTEGER NOT NULL,
    PRIMARY KEY (epoch, decision_window, gauge_address)
)
"""

PREBOUNDARY_BACKTEST_RESULTS = """
CREATE TABLE IF NOT EXISTS preboundary_backtest_results (
    epoch                           INTEGER NOT NULL,
    decision_window                 TEXT    NOT NULL,
    run_id                          TEXT    NOT NULL,
    expected_return_usd             REAL    NOT NULL,
    realized_return_usd             REAL,
    p10_return_usd                  REAL,
    regret_usd                      REAL,
    calibration_error               REAL,
    computed_at                     INTEGER NOT NULL,
    expected_portfolio_return_bps   INTEGER,
    expected_portfolio_downside_bps INTEGER,
    realized_portfolio_return_bps   INTEGER,
    portfolio_error_bps             INTEGER,
    median_realized_return_bps      INTEGER,
    p10_realized_return_bps         INTEGER,
    regret_vs_hindsight_bps         INTEGER,
    calibration_score               REAL,
    source_tag                      TEXT    DEFAULT 'p5_backtest',
    baseline_portfolio_return_bps   INTEGER,
    uplift_vs_baseline_bps          INTEGER,
    baseline_topk_portfolio_return_bps INTEGER,
    uplift_vs_topk_baseline_bps     INTEGER,
    PRIMARY KEY (epoch, decision_window, run_id)
)
"""

PREBOUNDARY_BACKTEST_PORTFOLIO_RESULTS = """
CREATE TABLE IF NOT EXISTS preboundary_backtest_portfolio_results (
    epoch                               INTEGER NOT NULL,
    decision_window                     TEXT    NOT NULL,
    num_gauges_in_forecast              INTEGER,
    num_gauges_allocated                INTEGER,
    expected_portfolio_return_bps       INTEGER,
    realized_portfolio_return_bps       INTEGER,
    portfolio_error_bps                 INTEGER,
    median_realized_return_bps          INTEGER,
    p10_realized_return_bps             INTEGER,
    min_realized_return_bps             INTEGER,
    max_realized_return_bps             INTEGER,
    regret_vs_hindsight_bps             INTEGER,
    calibration_score                   REAL,
    num_positive_return_gauges          INTEGER,
    num_negative_return_gauges          INTEGER,
    num_zero_allocation_gauges          INTEGER,
    computed_at                         INTEGER NOT NULL,
    expected_portfolio_downside_bps     INTEGER,
    baseline_portfolio_return_bps       INTEGER,
    uplift_vs_baseline_bps              INTEGER,
    baseline_topk_portfolio_return_bps  INTEGER,
    uplift_vs_topk_baseline_bps         INTEGER,
    PRIMARY KEY (epoch, decision_window)
)
"""

# ---------------------------------------------------------------------------
# Ordered list of all CREATE TABLE statements (apply in this order)
# ---------------------------------------------------------------------------

ALL_TABLES = [
    # versioning (must be first)
    SCHEMA_VERSION,
    # reference
    EPOCH_BOUNDARIES,
    GAUGE_BRIBE_MAPPING,
    BRIBE_REWARD_TOKENS,
    TOKEN_METADATA,
    TOKEN_PRICES,
    HISTORICAL_TOKEN_PRICES,
    # boundary snapshots
    BOUNDARY_REWARD_SNAPSHOTS,
    BOUNDARY_REWARD_SAMPLES,
    BOUNDARY_GAUGE_VALUES,
    BOUNDARY_VOTE_SAMPLES,
    # live monitoring
    LIVE_GAUGE_SNAPSHOTS,
    LIVE_REWARD_TOKEN_SAMPLES,
    # execution
    AUTO_VOTE_RUNS,
    EXECUTED_ALLOCATIONS,
    PREDICTED_ALLOCATIONS,
    ALLOCATION_PERFORMANCE_METRICS,
    CLAIM_SWAP_EXECUTION_LOG,
    # actual rewards
    ACTUAL_EPOCH_REWARDS,
    # legacy
    EPOCHS_LEGACY,
    BRIBES_LEGACY,
    VOTES_LEGACY,
    GAUGES_LEGACY,
    HISTORICAL_ANALYSIS_LEGACY,
    # preboundary analysis (v4 — migrated from preboundary_dev.db)
    PREBOUNDARY_SNAPSHOTS,
    PREBOUNDARY_TRUTH_LABELS,
    PREBOUNDARY_FORECASTS,
    PREBOUNDARY_RECOMMENDATIONS,
    PREBOUNDARY_BACKTEST_GAUGE_RESULTS,
    PREBOUNDARY_BACKTEST_RESULTS,
    PREBOUNDARY_BACKTEST_PORTFOLIO_RESULTS,
]

# ---------------------------------------------------------------------------
# Views  (CREATE VIEW IF NOT EXISTS)
# ---------------------------------------------------------------------------

EPOCH_POOL_REALISATION = """
CREATE VIEW IF NOT EXISTS epoch_pool_realisation AS
WITH latest_live AS (
    -- Pick the strategy_tag with the highest recorded_at per epoch,
    -- excluding dry-run allocations.
    SELECT epoch, strategy_tag
    FROM (
        SELECT epoch, strategy_tag,
               ROW_NUMBER() OVER (
                   PARTITION BY epoch
                   ORDER BY MAX(recorded_at) DESC
               ) AS rn
        FROM executed_allocations
        WHERE source NOT LIKE 'dry_run:%'
        GROUP BY epoch, strategy_tag
    )
    WHERE rn = 1
),
canon AS (
    SELECT e.epoch, e.gauge_address, e.executed_votes, e.rank, e.strategy_tag
    FROM executed_allocations e
    JOIN latest_live l
      ON l.epoch = e.epoch AND l.strategy_tag = e.strategy_tag
)
SELECT
    bgv.epoch,
    bgv.gauge_address,
    bgv.pool_address,
    bgv.total_usd                                                  AS pool_bribe_usd,
    CAST(bgv.votes_raw AS REAL)                                    AS pool_votes_raw,
    canon.executed_votes                                           AS our_votes,
    canon.strategy_tag,
    canon.rank                                                     AS alloc_rank,
    CASE
        WHEN CAST(bgv.votes_raw AS REAL) > 0 AND canon.executed_votes IS NOT NULL
        THEN CAST(canon.executed_votes AS REAL) / CAST(bgv.votes_raw AS REAL)
        ELSE 0.0
    END                                                            AS our_vote_fraction,
    CASE
        WHEN CAST(bgv.votes_raw AS REAL) > 0 AND canon.executed_votes IS NOT NULL
        THEN (CAST(canon.executed_votes AS REAL) / CAST(bgv.votes_raw AS REAL)) * bgv.total_usd
        ELSE 0.0
    END                                                            AS our_expected_usd,
    CASE WHEN canon.gauge_address IS NOT NULL THEN 1 ELSE 0 END    AS voted
FROM boundary_gauge_values bgv
LEFT JOIN canon
    ON canon.epoch = bgv.epoch AND canon.gauge_address = bgv.gauge_address
WHERE bgv.total_usd > 0
ORDER BY bgv.epoch, bgv.total_usd DESC
"""

ALL_VIEWS = [
    EPOCH_POOL_REALISATION,
]

