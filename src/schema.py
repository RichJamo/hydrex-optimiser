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
CURRENT_SCHEMA_VERSION = 1

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
    # legacy
    EPOCHS_LEGACY,
    BRIBES_LEGACY,
    VOTES_LEGACY,
    GAUGES_LEGACY,
    HISTORICAL_ANALYSIS_LEGACY,
]
