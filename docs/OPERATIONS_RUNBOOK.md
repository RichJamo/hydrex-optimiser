# Operations Runbook (Canonical)

Updated: 2026-02-27

This runbook is the canonical entry point for operating the live voting workflow and associated maintenance.

## 1) Pre-flight

- Activate environment:

```bash
source venv/bin/activate
```

- Confirm required env vars in `.env`:
  - `RPC_URL`
  - `MY_ESCROW_ADDRESS`
  - `YOUR_VOTING_POWER`
  - `TEST_WALLET_PK`

## 2) Repository + DB cleanup audit

Run audit (read-only):

```bash
venv/bin/python scripts/repo_cleanup_audit.py
```

Explicit table drop (safe pattern):

```bash
venv/bin/python scripts/repo_cleanup_audit.py \
  --drop-table <table_name> \
  --apply
```

Behavior:

- Creates DB backup at `data/db/backups/data_cleanup_backup.db` before apply mode.
- Never drops tables unless explicitly named via `--drop-table`.

## 3) Live auto vote

Canonical boundary-safe mode (chain-time dual-phase monitor, recommended for weekly execution):

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/boundary_monitor.py \
  --trigger-seconds-before 90 \
  --second-trigger-seconds-before 20 \
  --enforce-pre-boundary-guard \
  --skip-fresh-fetch
```

Boundary safety policy (implemented):

- Epoch truth is on-chain (`_epochTimestamp`), not wall-clock UTC.
- Phase 1 trigger starts when chain time is within 90s of boundary.
- Phase 2 trigger starts when chain time is within 20s of boundary.
- Auto-voter aborts if on-chain epoch has advanced (mint/flip detected).
- Auto-voter aborts if remaining chain time is below configured minimum.

Recommended command (current gas headroom):

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/auto_voter.py \
  --simulation-block latest \
  --gas-limit 3000000
```

Allocator tuning (chunked marginal allocation, best-effort 1000-vote steps):

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/auto_voter.py \
  --simulation-block latest \
  --gas-limit 3000000 \
  --top-k 10 \
  --candidate-pools 20 \
  --min-votes-per-pool 1000
```

Optional safe dry-run:

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/auto_voter.py \
  --simulation-block latest \
  --gas-limit 3000000 \
  --dry-run
```

## 4) Post-flip weekly review (canonical single-command flow)

This is the canonical post-boundary flow to analyze the just-closed epoch using boundary values and export the operator-ready allocation artifact.

### Recommended command — boundary block known

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --epoch 1773273600 \
  --boundary-block 43242133 \
  --voting-power 1183272
```

### Recommended command — boundary row already present

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --epoch 1773273600 \
  --voting-power 1183272
```

Operator notes:

- `--boundary-block` is optional; when supplied, the wrapper first upserts `epoch_boundaries` via `scripts/set_epoch_boundary_manual.py`.
- `--epoch` defaults to the latest `epoch_boundaries` row when omitted, but passing it explicitly is safer for post-mortems.
- `--run-boundary-refresh` is available when boundary reward coverage is missing and you want to force a fresh bribe refresh.
- The wrapper then runs the deterministic review pipeline and exports the boundary-optimal allocation CSV with a top-10 console summary.

What this produces for the target epoch:

- boundary-optimal return (k-sweep on boundary values),
- predicted return from `T-1` preboundary snapshot,
- realized-at-boundary estimate and opportunity gap,
- executed-run attribution from `auto_vote_runs` with boundary-safe filtering,
- executed realized-at-boundary computed from persisted `executed_allocations` rows for the selected `run_id`,
- boundary-optimal allocation CSV at `analysis/pre_boundary/epoch_<epoch>_boundary_opt_alloc_k<k>.csv`.

Optional token-level reconciliation (if you have a JSON of actual received token amounts):

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --epoch 1773273600 \
  --voting-power 1183272 \
  --actual-rewards-json ./actual_rewards_epoch_1773273600.json
```

JSON shape:

```json
{
  "actual_tokens": { "USDC": 444.28, "HYDX": 6385.43 },
  "token_prices": { "USDC": 1.0, "HYDX": 0.064 }
}
```

Note: `executed_realized_at_boundary_usd` and token reconciliation require that the vote run was recorded with the current `scripts/auto_voter.py`, which now persists run-specific executed allocations.

Main outputs:

- CSV: `analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv` (or overridden `OUTPUT_CSV`)
- CSV: `analysis/pre_boundary/epoch_<epoch>_boundary_opt_alloc_k<k>.csv`
- Logs: `data/db/logs/preboundary_dev_t1_bulk.log`, `data/db/logs/preboundary_epoch_review_all.log`

Low-level fallback (only if you need to run the underlying components manually):

```bash
venv/bin/python scripts/set_epoch_boundary_manual.py \
  --epoch 1773273600 \
  --boundary-block 43242133

TARGET_EPOCH=1773273600 \
VOTING_POWER=1183272 \
RUN_BOUNDARY_REFRESH=true \
RUN_BOUNDARY_VOTES_REFRESH=auto \
bash scripts/run_preboundary_analysis_pipeline.sh

venv/bin/python scripts/export_boundary_optimal_allocation.py \
  --epoch 1773273600 \
  --voting-power 1183272
```

### Optional: historical strategy review

```bash
venv/bin/python scripts/weekly_allocation_review.py \
  --strategy-tag manual \
  --summary-k-mode best-sweep
```

## 5) Fetch pipeline

Canonical fetch docs are maintained in `data/fetchers/README.md`.

Full bribe refresh:

```bash
PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_epoch_bribes_multicall \
  --all-epochs --ignore-whitelist --progress-every-batches 6
```

## 6) Production scheduling safeguards

Minimum safeguards for unattended execution:

- Single-instance lockfile around auto vote execution.
- Retry policy with bounded attempts.
- Gas guardrails (`--max-gas-price-gwei`, `--gas-limit`).
- Structured stdout/stderr log retention and tx hash capture.
- Failure alert hook (mail/webhook) on non-zero exit.

Use these before enabling cron/service execution.
