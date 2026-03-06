#!/bin/bash
set -euo pipefail

# Schedule auto-voter test at 23:59 UTC (1 minute before midnight)
# Usage: schedule_midnight_test.sh [--dry-run] [--skip-fresh-fetch]

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"

# Parse options (pass through to scheduler)
DRY_RUN_FLAG=""
SKIP_FETCH_FLAG=""

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
    *)
      echo "Unknown option: $1"
      exit 1
      ;;
  esac
done

# Calculate seconds until 00:00 UTC tomorrow
NOW=$(date +%s)
# Get seconds until next midnight UTC
# macOS: use -v to get tomorrow's date, then convert to seconds
TOMORROW_DATE=$(date -u -v+1d +"%Y-%m-%d")
# Use Python for reliable cross-platform timestamp calculation
TOMORROW_MIDNIGHT=$(python3 -c "
import datetime
tomorrow_utc = datetime.datetime.now(datetime.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
print(int(tomorrow_utc.timestamp()))
")
# Run at 23:59 UTC (60 seconds before midnight)
TARGET_TIME=$((TOMORROW_MIDNIGHT - 60))
DELAY_SECS=$((TARGET_TIME - NOW))

if [[ $DELAY_SECS -lt 0 ]]; then
  echo "ERROR: Target time is in the past"
  exit 1
fi

# Convert to minutes for delay string (round up)
DELAY_MINS=$(( (DELAY_SECS + 59) / 60 ))

echo "================================================================"
echo "Midnight Timing Test - Auto-Voter Scheduler"
echo "================================================================"
echo "Current time (UTC):   $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo "Scheduled run (UTC):  $(date -u -r "$TARGET_TIME" +'%Y-%m-%dT%H:%M:%SZ') (23:59 UTC)"
echo "Time until run:       ${DELAY_MINS} minutes (~${DELAY_SECS} seconds)"
echo "Test mode:            ${DRY_RUN_FLAG:-live vote}"
echo "================================================================"
echo ""

# Call simple scheduler with calculated delay
exec "$SCRIPT_DIR/schedule_auto_vote_simple.sh" "+${DELAY_MINS}m" ${DRY_RUN_FLAG} ${SKIP_FETCH_FLAG}
