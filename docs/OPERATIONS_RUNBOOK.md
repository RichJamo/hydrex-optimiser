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

## 4) Weekly review + k-sweep

Baseline review:

```bash
venv/bin/python scripts/weekly_allocation_review.py \
  --strategy-tag manual \
  --summary-k-mode best-sweep
```

Expanded k study (10 -> 30):

```bash
venv/bin/python scripts/weekly_allocation_review.py \
  --strategy-tag manual \
  --k-sweep-max 30 \
  --k-sweep-max-combos 500000 \
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
