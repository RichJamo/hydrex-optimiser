# Validation Commands

Copy-paste command reference for the post-mortem review and the claim-and-swap
pipeline, from dry runs to live broadcasts. The weekly procedure that uses these
commands is in [OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md).

The epoch-attribution test plan for the retired `analyze_boundary_maximum_return.py`
is kept in [archive/VALIDATION_COMMANDS.md](archive/VALIDATION_COMMANDS.md).

---

## Canonical No-Refetch Workflow (Preboundary + Review)

Use this sequence for routine analysis so we avoid redundant data pulls and keep live-vote inputs consistent.

### One-command post-mortem wrapper (recommended)

Supply only `--boundary-block` — the epoch is auto-derived from the block timestamp via RPC
(`RPC_URL` must be set, or pass `--epoch` explicitly):

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --boundary-block 43846889 \
  --voting-power 1183272
```

With an explicit epoch (still supported — useful when RPC_URL is unavailable):

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --epoch 1775088000 \
  --boundary-block 43846889 \
  --voting-power 1183272
```

> **Epoch key convention**: `--epoch` must be the Mint-event flip timestamp (`epoch_boundaries.epoch`),
> i.e. the new epoch start, **not** the vote-period key. The vote-period key used by `rewardData()`
> is `epoch - WEEK` and is derived automatically. When using `--boundary-block` auto-derivation this
> is handled correctly without manual calculation.

If the boundary row is already present and RPC_URL is unavailable, omit `--boundary-block` and pass `--epoch` explicitly:

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --epoch 1775088000 \
  --voting-power 1183272
```

This wrapper:

- optionally upserts `epoch_boundaries` from an explorer-confirmed block,
- runs `scripts/shell/run_preboundary_analysis_pipeline.sh` with the correct env wiring,
- passes `--ignore-whitelist` to the boundary refresh by default (`--boundary-ignore-whitelist`),
  so a reward token newly added to an existing bribe is still queried; without it such a token is
  never recorded and `executed_realized_at_boundary` comes out understated,
- exports `analysis/pre_boundary/epoch_<epoch>_boundary_opt_alloc_k<k>.csv`,
- prints a compact top-pool summary for operator review.

Dry-run validation:

```bash
venv/bin/python scripts/run_postmortem_review.py \
  --boundary-block 43846889 \
  --voting-power 1183272 \
  --dry-run
```

Low-level wrapper (pipeline only):

```bash
TARGET_EPOCH=1773273600 \
VOTING_POWER=1183272 \
RUN_BOUNDARY_REFRESH=false \
bash scripts/shell/run_preboundary_analysis_pipeline.sh
```

This lower-level wrapper defaults to resume mode (no forced overwrite), uses multicall-backed fetchers, and writes logs to `data/db/logs/`.

### 0) One-time (or when epoch range extends): boundary rewards via multicall

```bash
PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_epoch_bribes_multicall \
  --all-epochs \
  --progress-every-batches 6
```

Only re-run this when `epoch_boundaries` has new epochs or boundary reward coverage is missing.

### 1) Preboundary snapshots: T-1 only, resume by default

```bash
PYTHONUNBUFFERED=1 venv/bin/python -m data.fetchers.fetch_preboundary_snapshots \
  --start-epoch 1758153600 \
  --end-epoch 1772064000 \
  --snapshot-source onchain_rewarddata \
  --decision-windows T-1 \
  --db-path data/db/preboundary_dev.db \
  --live-db-path data/db/data.db \
  --min-reward-usd 0 \
  --log-file data/db/logs/preboundary_dev_t1_bulk.log
```

Notes:

- Keep `--resume` behavior (default) for incremental runs; use `--no-resume` only when intentionally rebuilding.
- `weightsAt` and `rewardData` are multicall-batched.
- Token lists are reused from `bribe_reward_tokens`, and newly discovered pairs are persisted to reduce future RPC enumeration.

### 2) All-epoch predicted vs optimal review

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/preboundary_epoch_review.py \
  --db-path data/db/data.db \
  --preboundary-db-path data/db/preboundary_dev.db \
  --recent-epochs 100 \
  --decision-window T-1 \
  --voting-power 1183272 \
  --candidate-pools 60 \
  --k-min 1 --k-max 50 --k-step 1 \
  --progress-every-k 10 \
  --output-csv analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv \
  --log-file data/db/logs/preboundary_epoch_review_all.log
```

### 2b) Export a single-epoch boundary-optimal allocation CSV

```bash
venv/bin/python scripts/export_boundary_optimal_allocation.py \
  --epoch 1773273600 \
  --voting-power 1183272
```

Expected:

- Resolves `boundary_opt_k` from `analysis/pre_boundary/epoch_boundary_vs_t1_review_all.csv` when present.
- Falls back to a local k-sweep when the review CSV does not yet contain the target epoch.
- Writes `analysis/pre_boundary/epoch_1773273600_boundary_opt_alloc_k48.csv` and prints a top-10 cumulative return summary.

### 3) Quick coverage checks before live-vote runs

```bash
sqlite3 data/db/data.db "SELECT MIN(epoch), MAX(epoch), COUNT(*) FROM epoch_boundaries;"
sqlite3 data/db/preboundary_dev.db "SELECT COUNT(DISTINCT epoch) FROM preboundary_snapshots WHERE decision_window='T-1';"
```

If epoch counts diverge, run step (1) incrementally for missing epochs instead of re-running full history.

---

## Claim + Swap Validation (Phase 1-6)

`--claim-source` defaults to `auto`, which reads `PartnerEscrow.tokenId()` and
`VotingEscrow.ownerOf()` and picks the source from actual veNFT ownership. It does **not**
follow `VOTE_FROM`: delegation moves voting rights, not reward accrual, so an escrow-owned
veNFT is claimed through the escrow even while `VOTE_FROM=signer`
(see `docs/OPERATIONS_RUNBOOK.md` §3a). Omitting the flag is the normal case; pass
`escrow`/`voter`/`distributor` only to override the resolution.

Use these commands to validate the new `scripts/claim_and_swap_rewards.py` flow safely.

### Dry-run discovery + claim simulation only

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --claim-source escrow \
  --escrow-address <escrow> \
  --claim-mode all \
  --output phase1_3_artifact.test.json \
  --loglevel INFO
```

Expected:

- Preflight checks pass (RPC, chain ID, signer, gas).
- Gauge/bribe/reward token summary prints.
- Phase 3 shows dry-run claim batches.
- Phase 3 performs escrow `claimRewards(...)` dry-run batches across discovered fee/bribe contracts.
- If signer is not authorized, script fails early with escrow preflight authorization error.

### Dry-run escrow claim simulation (`claimRewards`) only

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --claim-source escrow \
  --escrow-address <escrow> \
  --claim-mode all \
  --output phase3_escrow_artifact.test.json \
  --loglevel INFO
```

Expected:

- Escrow preflight estimates `claimRewards(feeAddresses, bribeAddresses, claimTokens)` gas.
- Fee/bribe addresses are discovered from gauge mappings; `claimTokens` comes from enumerated reward tokens.
- Phase 3 shows escrow claim batches (no Voter batch calls in escrow mode).
- If signer is not authorized for escrow claim execution, script fails before any broadcast.

### Dry-run targeted claim simulation for an explicit voted pool list

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --claim-source escrow \
  --escrow-address <escrow> \
  --pool-addresses "0xf19787f048b3401546aa7a979afa79d555c114dd,0x2df4af05f8c4aff0d3fbfc327595dbb7fc6498bf" \
  --output phase3_targeted_claim_artifact.test.json \
  --loglevel INFO
```

Expected:

- The script bypasses auto-discovery and resolves only the supplied pools to gauges.
- Phase 2 summary includes only the targeted gauges/bribes/tokens.
- Phase 3 simulates claims only for bribes attached to that explicit target set.
- If a supplied pool or gauge is unknown to the local `gauges` table, the script fails early with an explicit unresolved-target error.

### Dry-run distributor claim simulation (`claim(tokenId)`) only

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --claim-source distributor \
  --rewards-distributor-address <HYDREX_REWARDS_DISTRIBUTOR_ADDRESS> \
  --distributor-token-id <veNFT id> \
  --output phase3_distributor_artifact.test.json \
  --loglevel INFO
```

Expected:

- Distributor preflight checks `claimable(tokenId)` and claim authorization by gas estimation.
- Phase 3 shows a single distributor claim action for tokenId `<veNFT id>`.
- No Voter `claimFees`/`claimBribes` batches are built when `--claim-source distributor` is set.
- If signer is not authorized for tokenId ownership/approval, script fails before any broadcast.

### Dry-run Phase 4 swaps without claim execution

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --skip-claims \
  --enable-swaps \
  --write-run-log \
  --output runs/phase1_4_artifact.test.json \
  --loglevel INFO
```

Expected:

- Phase 3 is skipped.
- Swap intents are generated only for non-USDC balances above dust threshold.
- Phase 4 summary is printed and `swap_results` is included in artifact JSON.
- Phase 5 writes a `phase5_summary` row into `claim_swap_execution_log`.

### Dry-run Phase 4 swaps — router-batch mode (single executeSwaps tx)

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --skip-claims \
  --enable-swaps \
  --swap-mode router-batch \
  --swap-recipient 0xA99C19D3E64b92441C5CC00f6d51f0Fe94E24f91 \
  --output phase11_router_batch_dryrun.json \
  --loglevel INFO
```

Expected:

- Routes all token→USDC swaps via `POST https://router.api.hydrex.fi/quote/multi`.
- Validates multi-router bytecode at `0x599bFa1039C9e22603F15642B711D56BE62071f4`.
- Dry-run: prints per-leg route summary without broadcasting any transactions.
- `swap_results` in artifact contains a single `BATCH` entry with `legs` array.

### Live broadcast — router-batch mode (explicit opt-in)

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "op://<vault>/<item>/<field>" \
  --broadcast \
  --claim-source escrow \
  --escrow-address <escrow> \
  --claim-mode all \
  --enable-swaps \
  --swap-mode router-batch \
  --swap-recipient <recipient_address> \
  --output phase11_router_batch_live.json \
  --loglevel INFO
```

Safety notes:

- Sends N approve txs (one per input token, skipped if allowance already sufficient).
- Sends 1 `executeSwaps` tx to Hydrex multi-router `0x599bFa1039C9e22603F15642B711D56BE62071f4`.
- If `--swap-recipient` differs from signer, sends 1 additional USDC forward tx.
- `HYDREX_ROUTING_SLIPPAGE_BPS` (default 50 = 0.5%) controls min output amounts.

### Live broadcast (explicit opt-in)

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "op://<vault>/<item>/<field>" \
  --broadcast \
  --claim-source escrow \
  --escrow-address <escrow> \
  --claim-mode all \
  --enable-swaps \
  --swap-recipient <recipient_address> \
  --output runs/phase1_4_artifact.live.json \
  --loglevel INFO
```

Safety notes:

- Broadcast happens only when `--broadcast` is set.
- Claim execution calls escrow `claimRewards(...)` in batches across discovered fee/bribe contracts.
- Swap execution uses exact approval per swap and slippage ladder retries.
- Keep artifact output for post-run review.

### Live distributor broadcast (explicit opt-in)

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "op://<vault>/<item>/<field>" \
  --broadcast \
  --claim-source distributor \
  --rewards-distributor-address <HYDREX_REWARDS_DISTRIBUTOR_ADDRESS> \
  --distributor-token-id <veNFT id> \
  --enable-swaps \
  --swap-recipient <recipient_address> \
  --output phase3_distributor_artifact.live.json \
  --loglevel INFO
```

Safety notes:

- Broadcast happens only when `--broadcast` is set.
- Distributor mode calls `claim(tokenId)` on the configured rewards distributor and skips Voter batch claims.
- Keep artifact output for post-run review and reconciliation.

### Phase 6 report-only rollup (no wallet/RPC required)

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --report-only \
  --report-lookback-days 14 \
  --report-json-output weekly_claim_swap_report.test.json \
  --report-csv-output weekly_claim_swap_report_swaps.test.csv \
  --loglevel INFO
```

Expected:

- Reads `claim_swap_execution_log` and prints weekly phase/status and swap-token rollups.
- Writes JSON rollup to `weekly_claim_swap_report.test.json`.
- Writes swap-token CSV rollup to `weekly_claim_swap_report_swaps.test.csv`.

### Integrated run + Phase 6 rollup in one command

```bash
venv/bin/python scripts/claim_and_swap_rewards.py \
  --wallet "$TEST_WALLET_PK" \
  --dry-run true \
  --skip-claims \
  --enable-swaps \
  --write-run-log \
  --weekly-report \
  --report-lookback-days 7 \
  --report-json-output weekly_claim_swap_report.json \
  --report-csv-output weekly_claim_swap_report_swaps.csv \
  --output phase1_6_artifact.test.json \
  --loglevel INFO
```

Expected:

- Phase 5 persistence writes run rows first.
- Phase 6 rollup runs at end of command and exports JSON/CSV outputs.
- Artifact JSON includes `claim_results` and `swap_results` arrays.
