#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-$ROOT_DIR/venv/bin/python}"

LOCK_DIR="${AUTO_VOTE_LOCK_DIR:-$ROOT_DIR/data/locks/auto_voter.lock}"
LOG_DIR="${AUTO_VOTE_LOG_DIR:-$ROOT_DIR/logs/auto_voter}"
MAX_RETRIES="${AUTO_VOTE_MAX_RETRIES:-3}"
RETRY_DELAY_SECONDS="${AUTO_VOTE_RETRY_DELAY_SECONDS:-20}"
SIMULATION_BLOCK="${AUTO_VOTE_SIMULATION_BLOCK:-latest}"
GAS_LIMIT="${AUTO_VOTE_GAS_LIMIT:-3000000}"
MAX_GAS_PRICE_GWEI="${AUTO_VOTE_MAX_GAS_PRICE_GWEI:-10}"

mkdir -p "$(dirname "$LOCK_DIR")" "$LOG_DIR"

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] lock active: $LOCK_DIR" >&2
  exit 2
fi

cleanup() {
  rmdir "$LOCK_DIR" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOG_FILE="$LOG_DIR/auto_voter_$TIMESTAMP.log"

BASE_CMD=(
  "$PYTHON_BIN"
  "$ROOT_DIR/scripts/auto_voter.py"
  "--simulation-block" "$SIMULATION_BLOCK"
  "--gas-limit" "$GAS_LIMIT"
  "--max-gas-price-gwei" "$MAX_GAS_PRICE_GWEI"
)

if [[ "${AUTO_VOTE_DRY_RUN:-false}" == "true" ]]; then
  BASE_CMD+=("--dry-run")
fi

if [[ -n "${AUTO_VOTE_EXTRA_ARGS:-}" ]]; then
  read -r -a EXTRA_ARGS <<< "$AUTO_VOTE_EXTRA_ARGS"
  BASE_CMD+=("${EXTRA_ARGS[@]}")
fi

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting guarded auto voter" | tee -a "$LOG_FILE"
echo "cmd: ${BASE_CMD[*]}" | tee -a "$LOG_FILE"

attempt=1
while [[ "$attempt" -le "$MAX_RETRIES" ]]; do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] attempt $attempt/$MAX_RETRIES" | tee -a "$LOG_FILE"

  if "${BASE_CMD[@]}" 2>&1 | tee -a "$LOG_FILE"; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] success" | tee -a "$LOG_FILE"
    exit 0
  fi

  if [[ "$attempt" -lt "$MAX_RETRIES" ]]; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] retrying in ${RETRY_DELAY_SECONDS}s" | tee -a "$LOG_FILE"
    sleep "$RETRY_DELAY_SECONDS"
  fi

  attempt=$((attempt + 1))
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] failed after $MAX_RETRIES attempts" | tee -a "$LOG_FILE"

if [[ -n "${AUTO_VOTE_ALERT_CMD:-}" ]]; then
  "$AUTO_VOTE_ALERT_CMD" "auto_voter_failed" "$LOG_FILE" || true
fi

exit 1
