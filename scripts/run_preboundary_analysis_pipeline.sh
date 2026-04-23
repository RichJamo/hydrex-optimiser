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

TARGET_EPOCH="${TARGET_EPOCH:-}"
RUN_BOUNDARY_VOTES_REFRESH="${RUN_BOUNDARY_VOTES_REFRESH:-auto}"
ACTUAL_REWARDS_JSON="${ACTUAL_REWARDS_JSON:-}"

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
echo "TARGET_EPOCH: ${TARGET_EPOCH:-auto}"
echo "RUN_BOUNDARY_VOTES_REFRESH: $RUN_BOUNDARY_VOTES_REFRESH"
echo "ACTUAL_REWARDS_JSON: ${ACTUAL_REWARDS_JSON:-none}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "ERROR: Python binary not found/executable: $PYTHON_BIN" >&2
  exit 1
fi

if [[ -z "$TARGET_EPOCH" ]]; then
  TARGET_EPOCH=$(sqlite3 "$LIVE_DB_PATH" "SELECT MAX(epoch) FROM epoch_boundaries;")
fi

if [[ -z "$TARGET_EPOCH" ]]; then
  echo "ERROR: Could not determine TARGET_EPOCH from epoch_boundaries" >&2
  exit 1
fi

if ! [[ "$TARGET_EPOCH" =~ ^[0-9]+$ ]]; then
  echo "ERROR: TARGET_EPOCH must be an integer epoch timestamp" >&2
  exit 1
fi

BOUNDARY_ROW=$(sqlite3 "$LIVE_DB_PATH" "SELECT boundary_block || ',' || vote_epoch FROM epoch_boundaries WHERE epoch=${TARGET_EPOCH} LIMIT 1;")
if [[ -z "$BOUNDARY_ROW" ]]; then
  echo "ERROR: TARGET_EPOCH=${TARGET_EPOCH} not found in epoch_boundaries" >&2
  echo "Hint: run the manual boundary command first:" >&2
  echo "  venv/bin/python scripts/set_epoch_boundary_manual.py --epoch ${TARGET_EPOCH} --boundary-block <block>" >&2
  echo "Or use the wrapper:" >&2
  echo "  venv/bin/python scripts/run_postmortem_review.py --epoch ${TARGET_EPOCH} --boundary-block <block> --voting-power <votes>" >&2
  exit 1
fi

BOUNDARY_BLOCK="${BOUNDARY_ROW%,*}"
VOTE_EPOCH_FROM_BOUNDARY="${BOUNDARY_ROW#*,}"

if [[ -z "$START_EPOCH" ]]; then
  START_EPOCH="$TARGET_EPOCH"
fi
if [[ -z "$END_EPOCH" ]]; then
  END_EPOCH="$TARGET_EPOCH"
fi

echo "Using target epoch: $TARGET_EPOCH"
echo "Boundary block: $BOUNDARY_BLOCK"
echo "Vote epoch: $VOTE_EPOCH_FROM_BOUNDARY"

if [[ "$RUN_BOUNDARY_REFRESH" == "true" ]]; then
  echo "\n[1/6] Refreshing boundary rewards via multicall"
  read -r -a REFRESH_ARGS <<< "$BOUNDARY_REFRESH_ARGS"
  if [[ ${#REFRESH_ARGS[@]} -eq 0 ]]; then
    REFRESH_ARGS=(--epochs "$TARGET_EPOCH" --progress-every-batches 1)
  fi
  run_cmd "$PYTHON_BIN" -m data.fetchers.fetch_epoch_bribes_multicall \
    --db-path "$LIVE_DB_PATH" \
    "${REFRESH_ARGS[@]}"
else
  echo "\n[1/6] Skipping boundary reward refresh (RUN_BOUNDARY_REFRESH=false)"
fi

GAUGE_CACHE_COUNT=$(sqlite3 "$LIVE_DB_PATH" "SELECT COUNT(*) FROM boundary_gauge_values WHERE epoch=${TARGET_EPOCH} AND active_only=1;")
if [[ "$RUN_BOUNDARY_VOTES_REFRESH" == "true" || ( "$RUN_BOUNDARY_VOTES_REFRESH" == "auto" && "${GAUGE_CACHE_COUNT:-0}" -eq 0 ) ]]; then
  echo "\n[2/6] Refreshing boundary vote cache (boundary_gauge_values)"
  run_cmd "$PYTHON_BIN" -m data.fetchers.fetch_boundary_votes \
    --end-epoch "$TARGET_EPOCH" \
    --weeks 1 \
    --db "$LIVE_DB_PATH" \
    --progress-every 100
else
  echo "\n[2/6] Skipping boundary vote cache refresh (rows=${GAUGE_CACHE_COUNT:-0}, RUN_BOUNDARY_VOTES_REFRESH=$RUN_BOUNDARY_VOTES_REFRESH)"
fi

echo "\n[3/6] Fetching preboundary snapshots (resume mode, no forced overwrite)"
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

echo "\n[4/6] Running epoch review (predicted vs optimal)"
if [[ "$VOTING_POWER" -le 0 ]]; then
  echo "ERROR: VOTING_POWER must be > 0 (set VOTING_POWER or YOUR_VOTING_POWER)" >&2
  exit 1
fi
run_cmd "$PYTHON_BIN" "$ROOT_DIR/scripts/preboundary_epoch_review.py" \
  --db-path "$LIVE_DB_PATH" \
  --preboundary-db-path "$PREBOUNDARY_DB_PATH" \
  --epochs "$TARGET_EPOCH" \
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

echo "\n[5/6] Coverage checks"
run_cmd sqlite3 "$LIVE_DB_PATH" "SELECT MIN(epoch), MAX(epoch), COUNT(*) FROM epoch_boundaries;"
run_cmd sqlite3 "$PREBOUNDARY_DB_PATH" "SELECT COUNT(DISTINCT epoch) FROM preboundary_snapshots WHERE decision_window='T-1';"

echo "\n[6/6] Summary for target epoch"
run_cmd sqlite3 "$LIVE_DB_PATH" "SELECT epoch,boundary_block,vote_epoch,source_tag FROM epoch_boundaries WHERE epoch=${TARGET_EPOCH};"
run_cmd "$PYTHON_BIN" - <<PY
import csv
import json
import sqlite3
from pathlib import Path

target_epoch = int(${TARGET_EPOCH})
csv_path = Path("${OUTPUT_CSV}")
db_path = Path("${LIVE_DB_PATH}")
actual_rewards_json = "${ACTUAL_REWARDS_JSON}"
if not csv_path.exists():
    print(f"SUMMARY: output CSV not found: {csv_path}")
    raise SystemExit(0)

row = None
with csv_path.open("r", encoding="utf-8") as f:
    reader = csv.DictReader(f)
    for r in reader:
        if int(r.get("epoch", "0") or 0) == target_epoch:
            row = r
            break

if not row:
    print(f"SUMMARY: epoch {target_epoch} not present in {csv_path}")
    raise SystemExit(0)

def as_float(value: str) -> float:
  try:
    return float(value)
  except Exception:
    return 0.0

def fmt_diff(value: float) -> str:
  return f"{value:+.6f}"

def expected_return_usd(total_usd: float, base_votes: float, your_votes: float) -> float:
  if your_votes <= 0:
    return 0.0
  denom = float(base_votes) + float(your_votes)
  if denom <= 0:
    return 0.0
  return float(total_usd) * (float(your_votes) / denom)

boundary_opt = as_float(row.get("boundary_opt_expected_usd", "0"))
t1_pred = as_float(row.get("t1_pred_expected_usd", "0"))
t1_realized = as_float(row.get("t1_realized_at_boundary_usd", "0"))

print("SUMMARY: boundary_opt_k=", row.get("boundary_opt_k"))
print("SUMMARY: boundary_opt_expected_usd=", row.get("boundary_opt_expected_usd"))
print("SUMMARY: t1_pred_k=", row.get("t1_pred_k"))
print("SUMMARY: t1_pred_expected_usd=", row.get("t1_pred_expected_usd"))
print("SUMMARY: t1_realized_at_boundary_usd=", row.get("t1_realized_at_boundary_usd"))
print("SUMMARY: opportunity_gap_usd=", row.get("opportunity_gap_usd"))

if not db_path.exists():
  print(f"SUMMARY: DB not found for executed attribution: {db_path}")
  raise SystemExit(0)

conn = sqlite3.connect(str(db_path))
try:
  boundary_row = conn.execute(
    "SELECT boundary_block, vote_epoch FROM epoch_boundaries WHERE epoch = ?",
    (target_epoch,),
  ).fetchone()

  if not boundary_row:
    print(f"SUMMARY: boundary row missing for epoch {target_epoch}")
    raise SystemExit(0)

  boundary_block = int(boundary_row[0])
  vote_epoch = int(boundary_row[1])

  boundary_votes_rows = conn.execute(
    """
    SELECT lower(gauge_address) AS gauge_address,
           CAST(votes_raw AS REAL) AS votes_raw
    FROM boundary_gauge_values
    WHERE epoch = ?
      AND active_only = 1
    """,
    (target_epoch,),
  ).fetchall()
  boundary_votes_by_gauge = {
    str(g): float(v or 0.0)
    for g, v in boundary_votes_rows
    if g
  }

  boundary_reward_rows = conn.execute(
    """
    SELECT lower(s.gauge_address) AS gauge_address,
           lower(s.reward_token) AS reward_token,
           s.rewards_raw,
           COALESCE(s.token_decimals, m.decimals, 18) AS token_decimals,
           COALESCE(s.usd_price, 0.0) AS usd_price,
           COALESCE(s.total_usd, 0.0) AS total_usd,
           COALESCE(m.symbol, '') AS symbol
    FROM boundary_reward_snapshots s
    LEFT JOIN token_metadata m ON lower(m.token_address) = lower(s.reward_token)
    WHERE s.epoch = ?
      AND s.active_only = 1
    """,
    (target_epoch,),
  ).fetchall()

  price_rows = conn.execute(
    """
    SELECT lower(token_address) AS token_address, COALESCE(usd_price, 0.0) AS usd_price
    FROM token_prices
    WHERE COALESCE(usd_price, 0.0) > 0
    """
  ).fetchall()
  token_prices_by_address = {
    str(token): float(price or 0.0)
    for token, price in price_rows
    if token
  }

  rewards_usd_by_gauge = {}
  for gauge, token, rewards_raw, token_decimals, usd_price, total_usd, _symbol in boundary_reward_rows:
    gauge_l = str(gauge or '').lower()
    if not gauge_l:
      continue

    total_usd_f = float(total_usd or 0.0)
    if total_usd_f > 0:
      rewards_usd_by_gauge[gauge_l] = rewards_usd_by_gauge.get(gauge_l, 0.0) + total_usd_f
      continue

    token_l = str(token or '').lower()
    decimals_i = int(token_decimals or 18)
    try:
      reward_amt = float(int(str(rewards_raw or '0'))) / float(10 ** max(0, decimals_i))
    except Exception:
      reward_amt = 0.0
    if reward_amt <= 0:
      continue

    price = float(usd_price or 0.0)
    if price <= 0:
      price = float(token_prices_by_address.get(token_l, 0.0))
    if price <= 0:
      continue

    rewards_usd_by_gauge[gauge_l] = rewards_usd_by_gauge.get(gauge_l, 0.0) + (reward_amt * price)

  exec_row = conn.execute(
    """
    SELECT id, vote_sent_at, tx_hash, expected_return_usd
    FROM auto_vote_runs
    WHERE status = 'tx_success'
      AND vote_sent_at IS NOT NULL
      AND vote_sent_at >= ?
      AND vote_sent_at < ?
    ORDER BY vote_sent_at DESC
    LIMIT 1
    """,
    (vote_epoch, target_epoch),
  ).fetchone()

  print("SUMMARY: boundary_block=", boundary_block)
  print("SUMMARY: vote_epoch=", vote_epoch)

  if not exec_row:
    print("SUMMARY: executed_run_id= none")
    print("SUMMARY: executed_sendtime_expected_usd= none")
    print("SUMMARY: executed_realized_at_boundary_usd= none")
  else:
    executed_run_id = int(exec_row[0])
    executed_vote_sent_at = int(exec_row[1])
    executed_tx_hash = str(exec_row[2] or "")
    executed_sendtime_expected = as_float(str(exec_row[3] if exec_row[3] is not None else "0"))

    strategy_tag = f"auto_voter_run_{executed_run_id}"
    executed_rows = conn.execute(
      """
      SELECT lower(gauge_address), executed_votes
      FROM executed_allocations
      WHERE epoch = ? AND strategy_tag = ?
      ORDER BY rank ASC
      """,
      (target_epoch, strategy_tag),
    ).fetchall()

    executed_votes_by_gauge = {
      str(g): int(v)
      for g, v in executed_rows
      if g and v is not None and int(v) > 0
    }

    executed_realized = None
    if executed_votes_by_gauge:
      realized_total = 0.0
      for gauge, votes in executed_votes_by_gauge.items():
        # boundary_votes_raw includes our executed votes, so subtract them to get
        # others-only votes — prevents double-counting our votes in the denominator.
        total_boundary_votes = float(boundary_votes_by_gauge.get(gauge, 0.0))
        base_votes = max(0.0, total_boundary_votes - float(votes))
        gauge_rewards_usd = float(rewards_usd_by_gauge.get(gauge, 0.0))
        realized_total += expected_return_usd(gauge_rewards_usd, base_votes, float(votes))
      executed_realized = float(realized_total)

    expected_token_amounts = {}
    expected_token_usd = {}
    for gauge, token, rewards_raw, token_decimals, usd_price, total_usd, symbol in boundary_reward_rows:
      gauge_l = str(gauge or '').lower()
      if not gauge_l:
        continue
      executed_votes = int(executed_votes_by_gauge.get(gauge_l, 0))
      if executed_votes <= 0:
        continue

      base_votes = float(boundary_votes_by_gauge.get(gauge_l, 0.0))
      denom = base_votes + float(executed_votes)
      if denom <= 0:
        continue
      share = float(executed_votes) / denom

      token_l = str(token or '').lower()
      decimals_i = int(token_decimals or 18)
      token_symbol = str(symbol or '').strip() or token_l[:10]

      try:
        reward_amt = float(int(str(rewards_raw or '0'))) / float(10 ** max(0, decimals_i))
      except Exception:
        reward_amt = 0.0
      expected_amt = reward_amt * share

      token_price = float(usd_price or 0.0)
      if token_price <= 0:
        token_price = float(token_prices_by_address.get(token_l, 0.0))

      total_usd_f = float(total_usd or 0.0)
      if total_usd_f > 0:
        expected_usd = total_usd_f * share
      elif token_price > 0 and expected_amt > 0:
        expected_usd = expected_amt * token_price
      else:
        expected_usd = 0.0

      expected_token_amounts[token_symbol] = expected_token_amounts.get(token_symbol, 0.0) + expected_amt
      expected_token_usd[token_symbol] = expected_token_usd.get(token_symbol, 0.0) + expected_usd

    print("SUMMARY: executed_run_id=", executed_run_id)
    print("SUMMARY: executed_vote_sent_at=", executed_vote_sent_at)
    print("SUMMARY: executed_tx_hash=", executed_tx_hash)
    print("SUMMARY: executed_sendtime_expected_usd=", f"{executed_sendtime_expected:.6f}")
    if executed_realized is None:
      print("SUMMARY: executed_allocation_rows_missing= true")
      print("SUMMARY: executed_realized_at_boundary_usd= none")
      print("SUMMARY: delta_executed_vs_boundary_opt_usd= none")
      print("SUMMARY: delta_executed_vs_t1_pred_usd= none")
      print("SUMMARY: delta_executed_vs_t1_realized_usd= none")
    else:
      print("SUMMARY: executed_realized_at_boundary_usd=", f"{executed_realized:.6f}")
      print("SUMMARY: delta_executed_vs_boundary_opt_usd=", fmt_diff(executed_realized - boundary_opt))
      print("SUMMARY: delta_executed_vs_t1_pred_usd=", fmt_diff(executed_realized - t1_pred))
      print("SUMMARY: delta_executed_vs_t1_realized_usd=", fmt_diff(executed_realized - t1_realized))

    if actual_rewards_json and executed_votes_by_gauge:
      actual_path = Path(actual_rewards_json)
      if not actual_path.exists():
        print(f"SUMMARY: actual_rewards_json_not_found= {actual_path}")
      else:
        try:
          payload = json.loads(actual_path.read_text(encoding='utf-8'))
        except Exception as exc:
          print(f"SUMMARY: actual_rewards_json_error= {exc}")
        else:
          actual_tokens_raw = payload.get('actual_tokens', {}) if isinstance(payload, dict) else {}
          file_token_prices = payload.get('token_prices', {}) if isinstance(payload, dict) else {}

          actual_amounts = {}
          for symbol, raw_val in (actual_tokens_raw.items() if isinstance(actual_tokens_raw, dict) else []):
            sym = str(symbol).strip()
            if not sym:
              continue
            if isinstance(raw_val, (int, float)):
              actual_amounts[sym] = float(raw_val)
            elif isinstance(raw_val, dict):
              if 'amount' in raw_val:
                actual_amounts[sym] = as_float(str(raw_val.get('amount')))
              elif 'value' in raw_val:
                actual_amounts[sym] = as_float(str(raw_val.get('value')))

          token_price_by_symbol = {}
          for symbol, price in (file_token_prices.items() if isinstance(file_token_prices, dict) else []):
            sym = str(symbol).strip()
            if not sym:
              continue
            token_price_by_symbol[sym] = as_float(str(price))

          total_expected_tokens_usd = 0.0
          total_actual_tokens_usd = 0.0
          token_rows = []

          all_symbols = sorted(set(expected_token_amounts.keys()) | set(actual_amounts.keys()))
          for sym in all_symbols:
            exp_amt = float(expected_token_amounts.get(sym, 0.0))
            exp_usd = float(expected_token_usd.get(sym, 0.0))
            act_amt = float(actual_amounts.get(sym, 0.0))

            price = float(token_price_by_symbol.get(sym, 0.0))
            if price <= 0 and exp_amt > 0:
              price = exp_usd / exp_amt if exp_amt != 0 else 0.0

            act_usd = act_amt * price if price > 0 else 0.0
            total_expected_tokens_usd += exp_usd
            total_actual_tokens_usd += act_usd
            token_rows.append((sym, exp_amt, act_amt, exp_usd, act_usd, act_usd - exp_usd))

          print(f"SUMMARY: token_reconciliation_rows= {len(token_rows)}")
          print(f"SUMMARY: token_expected_total_usd= {total_expected_tokens_usd:.6f}")
          print(f"SUMMARY: token_actual_total_usd= {total_actual_tokens_usd:.6f}")
          print(f"SUMMARY: token_actual_minus_expected_usd= {fmt_diff(total_actual_tokens_usd - total_expected_tokens_usd)}")

          preview = sorted(token_rows, key=lambda x: abs(x[5]), reverse=True)[:12]
          for sym, exp_amt, act_amt, exp_usd, act_usd, delta_usd in preview:
            print(
              "SUMMARY:TOKEN:",
              sym,
              f"expected_amt={exp_amt:.6f}",
              f"actual_amt={act_amt:.6f}",
              f"expected_usd={exp_usd:.6f}",
              f"actual_usd={act_usd:.6f}",
              f"delta_usd={delta_usd:+.6f}",
            )
    elif actual_rewards_json and not executed_votes_by_gauge:
      print("SUMMARY: token_reconciliation_skipped= missing_executed_allocation_rows")
finally:
  conn.close()
PY

echo "\nPipeline complete."
echo "Output CSV: $OUTPUT_CSV"
echo "Fetch log:   $FETCH_LOG_FILE"
echo "Review log:  $REVIEW_LOG_FILE"
