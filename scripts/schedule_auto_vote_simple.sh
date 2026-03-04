#!/bin/bash
set -euo pipefail

# Simple Auto-Vote Scheduler with caffeinate protection
# Usage: schedule_auto_vote_simple.sh <delay> [--dry-run] [--skip-fresh-fetch] [--tolerance-pct N]
# Example: schedule_auto_vote_simple.sh "+15m" --skip-fresh-fetch --tolerance-pct 1.0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse delay
DELAY="${1:?Usage: $0 <delay> [options]}"
shift || true

# Parse options
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

# Calculate timing
NOW=$(date +%s)
SLEEP_SECS=$(echo "$DELAY" | sed -E 's/^\+([0-9]+)m$/\1 * 60/' | bc)
TARGET_SECS=$((NOW + SLEEP_SECS))
TARGET_ISO=$(date -u -r "$TARGET_SECS" +"%Y-%m-%dT%H:%M:%SZ")

# Create log name
LOG_NAME="scheduled_$(date -u +%Y%m%dT%H%M%SZ).log"
LOG_PATH="$REPO_ROOT/logs/auto_voter/$LOG_NAME"
mkdir -p "$(dirname "$LOG_PATH")"

# Build auto-voter command
AUTO_VOTER_CMD="$REPO_ROOT/venv/bin/python $SCRIPT_DIR/auto_voter.py"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --simulation-block latest"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --gas-limit 3000000"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --max-gas-price-gwei 10"
AUTO_VOTER_CMD="$AUTO_VOTER_CMD --db-path data/db/data.db"

[[ -n "$DRY_RUN_FLAG" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD $DRY_RUN_FLAG"
[[ -n "$SKIP_FETCH_FLAG" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD $SKIP_FETCH_FLAG"
[[ -n "$TOLERANCE_PCT" ]] && AUTO_VOTER_CMD="$AUTO_VOTER_CMD --auto-top-k-return-tolerance-pct $TOLERANCE_PCT"

# Print schedule info
echo "================================================================"
echo "Simple Auto-Vote Scheduler"
echo "================================================================"
echo "Now (UTC):           $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "Scheduled run:       $TARGET_ISO"
echo "Delay:               $DELAY (~$SLEEP_SECS seconds)"
echo "Protection:          caffeinate (prevents sleep)"
echo "Options:             ${DRY_RUN_FLAG:-live} ${SKIP_FETCH_FLAG:-} ${TOLERANCE_PCT:+tol=$TOLERANCE_PCT%}"
echo "Log:                 $LOG_PATH"
echo "================================================================"
echo ""

# Launch background job with caffeinate
(
  {
    echo "test_mode=simple_scheduler_caffeinate"
    echo "scheduled_created_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "scheduled_run_utc=$TARGET_ISO"
    echo ""
    sleep "$SLEEP_SECS"
    echo "actual_start_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] starting auto voter under caffeinate"
    echo "cmd: $AUTO_VOTER_CMD"
    echo ""
    
    cd "$REPO_ROOT"
    if caffeinate -i $AUTO_VOTER_CMD; then
      RC=0
      echo ""
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] success"
    else
      RC=$?
      echo ""
      echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] failed with exit code $RC"
    fi
    
    echo "final_rc=$RC"
    echo "completed_utc=$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    exit $RC
  } >> "$LOG_PATH" 2>&1
) &

BG_PID=$!

echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] Background job launched (PID: $BG_PID)"
echo ""
echo "✓ Ready. You can close this terminal or sleep your Mac."
echo "  The vote will run under caffeinate at the scheduled time."
echo ""

exit 0
