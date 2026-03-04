#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python}"

LIVE_DB_PATH="${LIVE_DB_PATH:-$ROOT_DIR/data/db/data.db}"
PREBOUNDARY_DB_PATH="${PREBOUNDARY_DB_PATH:-$ROOT_DIR/data/db/preboundary_dev.db}"
PIPELINE_LOG_DIR="${PIPELINE_LOG_DIR:-$ROOT_DIR/data/db/logs}"

SNAPSHOT_SOURCE="${SNAPSHOT_SOURCE:-onchain_rewarddata}"
DECISION_WINDOWS="${DECISION_WINDOWS:-T-1}"
MIN_REWARD_USD="${MIN_REWARD_USD:-0}"
MAX_GAUGES="${MAX_GAUGES:-0}"

RUN_BOUNDARY_REFRESH="${RUN_BOUNDARY_REFRESH:-false}"
BOUNDARY_REFRESH_ARGS="${BOUNDARY_REFRESH_ARGS:---all-epochs --progress-every-batches 6}"

VOTING_POWER="${VOTING_POWER:-${YOUR_VOTING_POWER:-0}}"
CANDIDATE_POOLS="${CANDIDATE_POOLS:-60}"
MIN_VOTES_PER_POOL="${MIN_VOTES_PER_POOL:-${MIN_VOTE_ALLOCATION:-1000}}"
K_MIN="${K_MIN:-1}"
K_MAX="${K_MAX:-50}"
K_STEP="${K_STEP:-1}"
PROGRESS_EVERY_K="${PROGRESS_EVERY_K:-10}"
RECENT_EPOCHS="${RECENT_EPOCHS:-100}"

OUTPUT_CSV="${OUTPUT_CSV:-$ROOT_DIR/analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv}"
REVIEW_LOG_FILE="${REVIEW_LOG_FILE:-$PIPELINE_LOG_DIR/preboundary_epoch_review_all.log}"
FETCH_LOG_FILE="${FETCH_LOG_FILE:-$PIPELINE_LOG_DIR/preboundary_dev_t1_bulk.log}"

START_EPOCH="${START_EPOCH:-}"
END_EPOCH="${END_EPOCH:-}"

DRY_RUN="${DRY_RUN:-false}"

mkdir -p "$PIPELINE_LOG_DIR" "$ROOT_DIR/analysis/pre_boundary"

run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "[DRY_RUN] $*"
    return 0
  fi
  "$@"
}

echo "=== Preboundary Analysis Pipeline ==="
echo "ROOT_DIR: $ROOT_DIR"
echo "PYTHON_BIN: $PYTHON_BIN"
echo "LIVE_DB_PATH: $LIVE_DB_PATH"
echo "PREBOUNDARY_DB_PATH: $PREBOUNDARY_DB_PATH"
echo "DECISION_WINDOWS: $DECISION_WINDOWS"
echo "SNAPSHOT_SOURCE: $SNAPSHOT_SOURCE"
echo "RUN_BOUNDARY_REFRESH: $RUN_BOUNDARY_REFRESH"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not found/executable: $PYTHON_BIN" >&2
  exit 1
fi

if [[ "$RUN_BOUNDARY_REFRESH" == "true" ]]; then
  echo "\n[1/4] Refreshing boundary rewards via multicall"
  read -r -a REFRESH_ARGS <<< "$BOUNDARY_REFRESH_ARGS"
  run_cmd "$PYTHON_BIN" -m data.fetchers.fetch_epoch_bribes_multicall \
    --db-path "$LIVE_DB_PATH" \
    "${REFRESH_ARGS[@]}"
else
  echo "\n[1/4] Skipping boundary reward refresh (RUN_BOUNDARY_REFRESH=false)"
fi

if [[ -z "$START_EPOCH" || -z "$END_EPOCH" ]]; then
  EPOCH_RANGE=$(sqlite3 "$LIVE_DB_PATH" "SELECT MIN(epoch) || ',' || MAX(epoch) FROM epoch_boundaries;")
  START_EPOCH="${EPOCH_RANGE%,*}"
  END_EPOCH="${EPOCH_RANGE#*,}"
fi

if [[ -z "$START_EPOCH" || -z "$END_EPOCH" ]]; then
  echo "ERROR: Could not determine START_EPOCH/END_EPOCH from epoch_boundaries" >&2
  exit 1
fi

echo "\n[2/4] Fetching preboundary snapshots (resume mode, no forced overwrite)"
echo "Epoch range: $START_EPOCH -> $END_EPOCH"
run_cmd "$PYTHON_BIN" -m data.fetchers.fetch_preboundary_snapshots \
  --start-epoch "$START_EPOCH" \
  --end-epoch "$END_EPOCH" \
  --snapshot-source "$SNAPSHOT_SOURCE" \
  --decision-windows "$DECISION_WINDOWS" \
  --db-path "$PREBOUNDARY_DB_PATH" \
  --live-db-path "$LIVE_DB_PATH" \
  --min-reward-usd "$MIN_REWARD_USD" \
  --max-gauges "$MAX_GAUGES" \
  --log-file "$FETCH_LOG_FILE"

echo "\n[3/4] Running epoch review (predicted vs optimal)"
if [[ "$VOTING_POWER" -le 0 ]]; then
  echo "ERROR: VOTING_POWER must be > 0 (set VOTING_POWER or YOUR_VOTING_POWER)" >&2
  exit 1
fi
run_cmd "$PYTHON_BIN" "$ROOT_DIR/scripts/preboundary_epoch_review.py" \
  --db-path "$LIVE_DB_PATH" \
  --preboundary-db-path "$PREBOUNDARY_DB_PATH" \
  --recent-epochs "$RECENT_EPOCHS" \
  --decision-window "T-1" \
  --voting-power "$VOTING_POWER" \
  --candidate-pools "$CANDIDATE_POOLS" \
  --min-votes-per-pool "$MIN_VOTES_PER_POOL" \
  --k-min "$K_MIN" \
  --k-max "$K_MAX" \
  --k-step "$K_STEP" \
  --progress-every-k "$PROGRESS_EVERY_K" \
  --output-csv "$OUTPUT_CSV" \
  --log-file "$REVIEW_LOG_FILE"

echo "\n[4/4] Coverage checks"
run_cmd sqlite3 "$LIVE_DB_PATH" "SELECT MIN(epoch), MAX(epoch), COUNT(*) FROM epoch_boundaries;"
run_cmd sqlite3 "$PREBOUNDARY_DB_PATH" "SELECT COUNT(DISTINCT epoch) FROM preboundary_snapshots WHERE decision_window='T-1';"

echo "\nPipeline complete."
echo "Output CSV: $OUTPUT_CSV"
echo "Fetch log:   $FETCH_LOG_FILE"
echo "Review log:  $REVIEW_LOG_FILE"
