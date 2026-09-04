# Operations Runbook (Canonical)

Updated: 2026-04-08

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

## 2b) Weekly pricing maintenance (run well away from the boundary)

Two jobs keep the vote window cheap. Both are safe to run any time except close to the
flip; a good slot is right after the post-flip review.

### Measure pool liquidity

```bash
venv/bin/python scripts/measure_token_liquidity.py
```

Asks the router what each reward token's Base pools would actually pay for a $500 sale and
records it in `token_liquidity`. The price feed reads that cache and values unsellable
tokens at $0, without making a single network call during the vote.

This is a real correction, not a tidy-up. A price says nothing about whether the position
behind it can be sold: SPLASH quotes $5.91e-06 per token while its whole pool pays out
$0.28, so a million-token bribe reads as $5.91 and realises $0.047. As of 2026-08-21, six
of 123 reward tokens are below the floor — WNIBI $0.01, SPLASH $0.20, LAOD $1.91, ARIO
$4.15, hwUSD $44 — against WOLF at $6,751 and GHO at $26,353. ARIO carried a live bribe.

Takes ~3.5 minutes for 123 tokens. Measurements older than `HYDREX_LIQUIDITY_MAX_AGE_DAYS`
(21 days) are ignored, so missing a week is harmless; missing a month means those tokens go
unchecked rather than wrongly zeroed. A token the router cannot quote is left unmeasured on
purpose — absent evidence must never zero a token.

### Refresh cached prices

```bash
venv/bin/python scripts/refresh_token_prices.py
```

Fills `token_prices` so phase 1 has nothing left to price. The boundary monitor spawns this
automatically on its prefetch cadence (see 3b), so running it by hand is only needed if you
are voting without the monitor, or want dormant tokens repriced sooner.

Covering dormant tokens is the point of the manual run: a token with no live bribe is never
touched by voting, so it sits on whatever it last had until somebody bribes with it. That is
how LAOD reached a vote carrying a 49-day-old price and cost about $8 in misallocated votes.

## 3) Live auto vote

### Pre-flight checklist

Before running, confirm:

1. `YOUR_VOTING_POWER` in `.env` is current (query on-chain or use last known value — see step 3a below).
2. Laptop is plugged in and sleep is suppressed (`caffeinate` handles this automatically in the canonical command).
3. Start the command at **~23:50 UTC** on Wednesday night. The epoch boundary is **00:00 UTC Thursday**.
4. The signer holds `PARTNER_ROLE` on the escrow. The monitor now checks this itself at
   startup and **exits non-zero** if the role is missing, so there is nothing to do by
   hand — but if you see `✗ PARTNER_ROLE check failed`, do not force past it without
   understanding why (see below).

The vote is gated on `PARTNER_ROLE` (`0x2f049b28…`) on the escrow, which is an
OpenZeppelin AccessControl contract. That grant lives on-chain and the `DEFAULT_ADMIN_ROLE`
holder can revoke it without any change to this repo, so a run can look correctly
configured and still revert at broadcast — inside the vote window, with no time to react.
The check is skipped under `--dry-run`, which never broadcasts. `--allow-missing-partner-role`
proceeds anyway; it exists for the case where the RPC cannot complete the check, not as a
way past a genuine revocation, because without the role the vote simply reverts.

### 3a) Check / update voting power

`YOUR_VOTING_POWER` in `.env` must be kept up to date manually, and the value hardcoded in the 3b command below goes stale with it — re-check both every week.

Authoritative source is on-chain: read `balanceOfNFT(tokenId)` on the VotingEscrow (`VoterV5._ve()`) for the veNFT held by `MY_ESCROW_ADDRESS`. Alternatively, run a live vote (not dry-run) and check the "Allocation validated" line, or check the escrow on a block explorer. Update `.env` **and** the `--your-voting-power` flag before running the boundary monitor.

Last verified: 2026-09-02 — escrow `0x768a675B8542F23C428C6672738E380176E7635C`, veNFT `#19435`, power `1,813,743`.

### 3b) Canonical weekly command (3-phase boundary monitor with caffeinate)

Run from the repo root, in an interactive terminal you can leave open until 00:05 UTC:

```bash
PYTHONUNBUFFERED=1 caffeinate -i venv/bin/python scripts/boundary_monitor.py \
  --trigger-seconds-before 240 \
  --second-trigger-seconds-before 60 \
  --third-trigger-seconds-before 35 \
  --enforce-pre-boundary-guard \
  --auto-top-k \
  --auto-top-k-return-tolerance-pct 5.0 \
  --your-voting-power 1813743 \
  --max-gas-price-gwei 10 \
  --phase1-price-max-age-hours 8.0 \
  --db-path data/db/data.db \
  2>&1 | tee logs/auto_voter/boundary_monitor_$(date -u +%Y%m%dT%H%M%SZ).log
```

**Important:** `PYTHONUNBUFFERED=1` must come **before** `caffeinate`, not after it.

What this does:

- `caffeinate -i` prevents macOS sleep for the duration.
- Phase 1 fires at T-240s (chain time), Phase 2 at T-60s, Phase 3 at T-35s. A phase needs ~21s from nominal trigger to broadcast, so these land at roughly T-220s, T-42s and T-17s. The earlier 40/20 spacing broadcast phase 3 at about T+1s and it reverted; 60/35 landed all three on 2026-08-27.
- Phase 1 does a full on-chain snapshot fetch (~80s for 291+ gauges); the T-240s trigger leaves ~160s of headroom so the vote lands before the 40s min-guard. Phases 2 and 3 use fast targeted bribe + vote-weight refreshes.
- `--auto-top-k` with 5% tolerance selects the optimal number of pools automatically.
- `--enforce-pre-boundary-guard` aborts if the epoch has already flipped before any tx is sent.
- Gas limit is auto-sized from simulation (actual usage ~5.8M gas); `--max-gas-price-gwei 10` caps fees.
- CoinGecko reference prices (`cg_ref`) are prefetched hourly while inside the 12h window, then a single guaranteed final refresh fires at `--cg-prefetch-stop-seconds-before` (default 600s) and prefetch goes silent — so no CG fetch competes with the phase triggers for network/RPC. The price feed prefers a fresh `cg_ref` (≤3h) over the routing quote, which defeats thin-liquidity routing mispricing (e.g. the BETR 2× overprice). `--cg-prefetch-stop-seconds-before` must exceed `--trigger-seconds-before`.
- Cached routing prices are refreshed hourly inside the same 12h window, writing to
  `token_prices`, then going quiet at `--cg-prefetch-stop-seconds-before` alongside the
  CoinGecko prefetch. Phase 1 skips any token already fresh within
  `--phase1-price-max-age-hours`, so pricing happens *before* the vote window rather than
  inside it. Measured 2026-08-21: 87 of 88 active tokens needed inline pricing before the
  prefetch and 0 of 88 after, moving ~46s of routing-API work out of a ~200s budget. The
  same refresh has been observed taking as long as 283s when the API is slow, which is what
  made doing it inline a poor trade. Disable with `--no-price-prefetch` if you ever need
  phase 1 to price everything itself.
- Output is logged to `logs/auto_voter/boundary_monitor_<timestamp>.log`.

Boundary safety policy:

- Epoch truth is on-chain (`_epochTimestamp`), not wall-clock UTC.
- Auto-voter aborts if on-chain epoch has advanced (mint/flip detected).
- Auto-voter aborts if remaining chain time is below configured minimum.

### 3c) Optional dry-run (verify allocation before committing)

Run this earlier in the day to check the allocation looks correct:

```bash
PYTHONUNBUFFERED=1 venv/bin/python scripts/auto_voter.py \
  --simulation-block latest \
  --max-gas-price-gwei 10 \
  --db-path data/db/data.db \
  --dry-run \
  2>&1 | tee logs/auto_voter/dry_run_$(date -u +%Y%m%dT%H%M%SZ).log
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
- `--run-boundary-refresh` is available when boundary reward coverage is missing and you want to force a fresh bribe refresh. A freshly closed epoch always needs it — without it the review aborts with `no boundary states with rewards` and `boundary_gauges=0`.
- `--boundary-ignore-whitelist` is **on by default** and should stay on. The refresh would otherwise build its `(bribe, token)` list from pairs that already have a non-zero row in `boundary_reward_snapshots`, which is self-referential: a token new to a bribe is never queried, so it never gets a row, so it stays excluded permanently. In epoch 1788393600 that hid a 54,595 oHYDX bribe (~$869) on the top-weighted gauge and understated `executed_realized_at_boundary` by $162.97 ($251.93 → $414.90), manufacturing a false outperformance. Ignoring the whitelist reads the `bribe_reward_tokens` table instead (3,472 pairs against the whitelist's 810), not on-chain enumeration, so the cost is negligible — the full review still runs in about 19s. Use `--no-boundary-ignore-whitelist` only to trade completeness for speed on an epoch you have already reconciled.
- `--boundary-price-source` controls how that refresh values rewards. It defaults to `snapshot`, which prices from the `auto_voter_snap` taken at or before the boundary — what the voter could actually see when it decided. Use `routing` only if you deliberately want current prices; re-quoting days later rewrites the vote-time basis and silently changes what the decision looked like. (On 2026-08-07 a `routing` refresh re-priced BETR at 2.0e-6 against a vote-time 7.99e-7 and turned a genuine $17.91 outperformance into a reported $42.78 shortfall.)
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
bash scripts/shell/run_preboundary_analysis_pipeline.sh

venv/bin/python scripts/export_boundary_optimal_allocation.py \
  --epoch 1773273600 \
  --voting-power 1183272
```

### Post-epoch price-feed audit (run each cycle)

Thin-liquidity tokens can quote persistently high on the Hydrex router. A *stable*
overprice is the dangerous kind: it sits under `PRICE_SANITY_MAX_SPIKE_RATIO` and, when no
CoinGecko reference is in range, the guard's last-resort anchor is the token's own previous
routing price — so the error is self-consistent and never trips at any threshold.

```bash
venv/bin/python scripts/audit_routing_price_divergence.py
```

Exits non-zero and prints a ready-to-paste address list when it finds a token whose median
routing/CoinGecko ratio is at or above 1.25 and which is not already routed via CoinGecko.
Append those to `HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS` in `.env`.

Note `.env` is gitignored, so that list is **not** version-controlled — the keys and their
rationale live in `.env.example`. Known persistent offenders as of 2026-08-07: REGENT (3.13x
median) and BETR (1.89x), both now routed via CoinGecko.

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
