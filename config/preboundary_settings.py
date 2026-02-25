"""
Pre-boundary configuration and P0 defaults.

Centralized configuration for the pre-boundary allocation forecasting system.
All P0 default constants defined here per the spec.
"""

# ═══════════════════════════════════════════════════════════════════════════════
# Decision Window Definitions
# ═══════════════════════════════════════════════════════════════════════════════

DECISION_WINDOWS = {
    "day": {
        "seconds_before_boundary": 86400,
        "description": "24 hours before boundary",
    },
    "T-1": {
        "seconds_before_boundary": 60,
        "description": "1 minute before boundary",
    },
    "boundary": {
        "seconds_before_boundary": 0,
        "description": "at boundary timestamp",
    },
}

# ═══════════════════════════════════════════════════════════════════════════════
# P0 Scenario Weights (from spec section 10)
# ═══════════════════════════════════════════════════════════════════════════════

SCENARIO_WEIGHTS = {
    "conservative": 0.25,
    "base": 0.50,
    "aggressive": 0.25,
}

# ═══════════════════════════════════════════════════════════════════════════════
# P0 Risk and Allocation Controls
# ═══════════════════════════════════════════════════════════════════════════════

LAMBDA_RISK = 0.20
K_MAX = 5
MIN_VOTES_PER_POOL = 50_000
MAX_TURNOVER_RATIO_PER_REVOTE = 0.80

# ═══════════════════════════════════════════════════════════════════════════════
# P0 No-Change / Rewrite Guardrails
# ═══════════════════════════════════════════════════════════════════════════════

MIN_EXPECTED_UPLIFT_BPS_FOR_REVOTE = 50  # 0.50%
MIN_EXPECTED_UPLIFT_USD_FOR_REVOTE = 25
T0_HIGH_RISK_BLOCK_REWRITE = True

# ═══════════════════════════════════════════════════════════════════════════════
# P0 Inclusion Risk Mapping (MVP Heuristic)
# ═══════════════════════════════════════════════════════════════════════════════

INCLUSION_RISK_THRESHOLDS = {
    "Low": 0.95,  # >= 0.95
    "Med": 0.80,  # >= 0.80 and < 0.95
    "High": 0.0,  # < 0.80
}

# Inclusion probability per decision window (for MVP heuristic scoring)
INCLUSION_PROB_BY_WINDOW = {
    "day": 0.95,
    "T-1": 0.85,
    "boundary": 0.70,
}

# ═══════════════════════════════════════════════════════════════════════════════
# P0 Data Quality Gates
# ═══════════════════════════════════════════════════════════════════════════════

MAX_PRICE_AGE_SECONDS = 3600  # 1 hour
MIN_GAUGE_HISTORY_EPOCHS_FOR_GAUGE_LEVEL_MODEL = 6
MIN_CLUSTER_HISTORY_EPOCHS_FOR_CLUSTER_FALLBACK = 4

# ═══════════════════════════════════════════════════════════════════════════════
# P0 Confidence Penalties
# ═══════════════════════════════════════════════════════════════════════════════

CONFIDENCE_PENALTY_SPARSE_HISTORY = 0.10
CONFIDENCE_PENALTY_HIGH_VARIANCE = 0.10
CONFIDENCE_PENALTY_STALE_PRICE = 0.15
CONFIDENCE_PENALTY_CAP = 0.30

# ═══════════════════════════════════════════════════════════════════════════════
# P2 Snapshot Collection Gates (MVP)
# ═══════════════════════════════════════════════════════════════════════════════

MIN_REWARD_TOTAL_USD_PER_GAUGE = 100.0  # Exclude gauges < $100
MIN_GAUGES_PER_EPOCH = 20  # Warn if < 20 gauges in epoch
MIN_REWARD_COVERAGE_RATIO = 0.70  # Warn if < 70% of gauges have reward data

# ═══════════════════════════════════════════════════════════════════════════════
# Block Time Estimate (for decision_block inference)
# ═══════════════════════════════════════════════════════════════════════════════

BLOCK_TIME_ESTIMATE_SECONDS = 12

# ═══════════════════════════════════════════════════════════════════════════════
# Logging and Storage Configuration
# ═══════════════════════════════════════════════════════════════════════════════

LOGGING_DIR = "data/db/logs"
PBOUNDARY_LOG_FILE = "data/db/logs/preboundary_collection.log"
HEARTBEAT_INTERVAL_ROWS = 50  # Log heartbeat every N rows processed

# MVP uses preboundary_dev.db, migrated to data.db in P6
PREBOUNDARY_DB_PATH = "data/db/preboundary_dev.db"

# ═══════════════════════════════════════════════════════════════════════════════
# Helper Functions
# ═══════════════════════════════════════════════════════════════════════════════


def get_decision_window(name: str) -> dict:
    """
    Get decision window configuration by name.

    Args:
        name: Window name ("day", "T-1", or "boundary")

    Returns:
        Dict with "seconds_before_boundary" and "description"

    Raises:
        KeyError if window name not found
    """
    return DECISION_WINDOWS[name]


def get_inclusion_risk_level(prob: float) -> str:
    """
    Map inclusion probability to risk level.

    Args:
        prob: Inclusion probability (0.0 to 1.0)

    Returns:
        Risk level: "Low", "Med", or "High"
    """
    if prob >= INCLUSION_RISK_THRESHOLDS["Low"]:
        return "Low"
    elif prob >= INCLUSION_RISK_THRESHOLDS["Med"]:
        return "Med"
    else:
        return "High"


def make_logging_dir() -> None:
    """Create logging directory if it doesn't exist."""
    from pathlib import Path

    Path(LOGGING_DIR).mkdir(parents=True, exist_ok=True)
