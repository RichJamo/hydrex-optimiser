#!/bin/bash
set -euo pipefail

# Production Auto-Vote Scheduler with Caffeinate Protection
# Usage: schedule_auto_vote_production.sh <delay> [--dry-run] [--skip-fresh-fetch] [--tolerance-pct N]
# Example: schedule_auto_vote_production.sh "+15m" --skip-fresh-fetch --tolerance-pct 1.0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
VENV_PYTHON="$REPO_ROOT/venv/bin/python"

if [[ ! -f "$VENV_PYTHON" ]]; then
  echo "ERROR: venv not found at $VENV_PYTHON"
  exit 1
fi

# Parse arguments
DELAY="${1:?Usage: $0 <delay> [options]}"
shift || true

DRY_RUN_FLAG=""
SKIP_FETCH_FLAG=""
TOLERANCE_PCT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN_FLAG="--dry-run"
      shift
      ;;
    --skip-fresh-fetch)
      SKIP_FETCH_FLAG="--skip-fresh-fetch"
      shift
      ;;
    --tolerance-pct)
      TOLERANCE_PCT="$2"
      shift 2
      ;;
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Calculate target time
NOW=$(date +%s)
SLEEP_SECS=$(echo "$DELAY" | sed -E 's/^\+([0-9]+)m$/\1 * 60/' | bc)
TARGET_SECS=$((NOW + SLEEP_SECS))
TARGET_ISO=$(date -u -r "$TARGET_SECS" +"%Y-%m-%dT%H:%M:%SZ")

echo "================================================================"
echo "Production Auto-Vote Scheduler"
echo "================================================================"
echo "Now (UTC):                 $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "Scheduled run (UTC):       $TARGET_ISO"
echo "Delay:                     $DELAY (~$SLEEP_SECS seconds)"
echo "Protection:                caffeinate (prevents sleep)"
echo "Options:                   ${DRY_RUN_FLAG:-None} ${SKIP_FETCH_FLAG:-} ${TOLERANCE_PCT:+tolerance=$TOLERANCE_PCT%}"
echo "================================================================"
echo ""

# Create timestamped log
LOG_NAME="production_scheduled_$(date -u +%Y%m%dT%H%M%SZ).log"
LOG_PATH="$REPO_ROOT/logs/auto_voter/$LOG_NAME"
mkdir -p "$(dirname "$LOG_PATH")"

# Build auto-voter command
AUTO_VOTER_CMD="$VENV_PYTHON $SCRIPT_DIR/auto_voter.py"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --simulation-block latest"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --gas-limit 3000000"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --max-gas-price-gwei 10"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --db-path data/db/data.db"

[[ -n "$DRY_RUN_FLAG" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD $DRY_RUN_FLAG"
[[ -n "$SKIP_FETCH_FLAG" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD $SKIP_FETCH_FLAG"
[[ -n "$TOLERANCE_PCT" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD --auto-top-k-return-tolerance-pct $TOLERANCE_PCT"

# Guarded + caffeinated command
FINAL_CMD="caffeinate -i bash $SCRIPT_DIR/run_auto_voter_safe.sh $AUTO_VOTER_CMD"

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Launching background job..."
echo "Log file: $LOG_PATH"
echo ""

# Launch in background with redirection
(
  {
    echo "test_mode=production_caffeinate"
    echo "scheduled_created_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "scheduled_run_utc=$TARGET_ISO"
    echo ""
    sleep "$SLEEP_SECS"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] actual_start_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] executing: $FINAL_CMD"
    echo ""
    eval "$FINAL_CMD"
    RC=$?
    echo ""
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] completed_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] exit_code=$RC"
  } >> "$LOG_PATH" 2>&1
) &

BG_PID=$!

echo "Ready. Background job (PID $BG_PID) will run under caffeinate."
echo "You can close this terminal now."
echo ""

exit 0
