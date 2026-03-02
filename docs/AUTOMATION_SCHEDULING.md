# Automation Scheduling (Cron / Service)

Updated: 2026-02-27

This document productionizes auto-voting with lockfile protection, retries, gas guardrails, and logs.

## Wrapper script

Use:

```bash
scripts/run_auto_voter_safe.sh
```

Default safeguards:

- single-instance lock (`data/locks/auto_voter.lock`)
- retry loop (`AUTO_VOTE_MAX_RETRIES`, default `3`)
- retry delay (`AUTO_VOTE_RETRY_DELAY_SECONDS`, default `20`)
- gas guardrails (`AUTO_VOTE_GAS_LIMIT` default `3000000`, `AUTO_VOTE_MAX_GAS_PRICE_GWEI` default `10`)
- per-run logs (`logs/auto_voter/auto_voter_*.log`)

## Required environment

Set in `.env` (or scheduler environment):

```bash
RPC_URL=...
MY_ESCROW_ADDRESS=...
YOUR_VOTING_POWER=...
TEST_WALLET_PK=...
AUTO_VOTE_MAX_GAS_PRICE_GWEI=10
AUTO_VOTE_GAS_LIMIT=3000000
```

Optional:

```bash
AUTO_VOTE_MAX_RETRIES=3
AUTO_VOTE_RETRY_DELAY_SECONDS=20
AUTO_VOTE_SIMULATION_BLOCK=latest
AUTO_VOTE_DRY_RUN=false
AUTO_VOTE_EXTRA_ARGS="--top-k 10 --candidate-pools 20 --min-votes-per-pool 1000"
AUTO_VOTE_ALERT_CMD=/absolute/path/to/alert_hook.sh
```

## Cron example

Run every 10 minutes (wrapper exits quickly if lock is active):

```cron
*/10 * * * * cd /Users/richardjamieson/Documents/GitHub/hydrex-optimiser && /bin/bash scripts/run_auto_voter_safe.sh >> logs/auto_voter/cron.log 2>&1
```

## systemd example

`/etc/systemd/system/hydrex-auto-voter.service`

```ini
[Unit]
Description=Hydrex Auto Voter (guarded)
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/Users/richardjamieson/Documents/GitHub/hydrex-optimiser
EnvironmentFile=/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/.env
ExecStart=/bin/bash scripts/run_auto_voter_safe.sh
StandardOutput=append:/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/logs/auto_voter/service.log
StandardError=append:/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/logs/auto_voter/service.log

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/hydrex-auto-voter.timer`

```ini
[Unit]
Description=Run Hydrex Auto Voter every 10 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
Persistent=true

[Install]
WantedBy=timers.target
```

Enable:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now hydrex-auto-voter.timer
```

## Monitoring checklist

- Check latest logs in `logs/auto_voter/`
- Verify tx hash appears on successful runs
- Alert on non-zero exit status and repeated failures
- Rotate logs periodically (e.g. `logrotate`)
