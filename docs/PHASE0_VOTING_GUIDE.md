# Phase 0 Voting Tools - Quick Start Guide

## Overview

Phase 0 provides two voting modes:

1. **Manual Voting (Phase 0.1)** - Generate voting instructions for manual execution
2. **Automated Voting (Phase 0.2)** - Automated system that votes at optimal time (20 blocks before boundary)

---

## Phase 0.1: Manual Voting

### Generate Voting Instructions

```bash
# Generate voting instructions from latest snapshot
python scripts/generate_voting_instructions.py

# Specify number of pools and voting power
python scripts/generate_voting_instructions.py --top-k 5 --your-voting-power 1183272

# Save to CSV file
python scripts/generate_voting_instructions.py --output-csv voting_instructions.csv
```

**Output includes:**

- Human-readable allocation table
- Contract call format (for Etherscan/web interfaces)
- Python/web3.py code snippet
- CSV format for record-keeping

**Example output:**

```
_poolVote (array of pool addresses):
["0x18156ace9940645ebf3602e2b320a77131a70ad1",
 "0x15951b35d7a8ea6b6ef5718eab2fcdd3ad072451",
 ...]

_voteProportions (array of relative weights - contract normalizes):
[10000, 10000, ...]
(Equal allocation: each pool gets weight 10000)
```

**Note:** Vote proportions are relative weights, not absolute vote amounts. The contract
normalizes them internally. For equal allocation across N pools, use `[10000, 10000, ...]`.
`VOTE_DELAY` is currently 0, so you can re-vote multiple times per epoch (constraint: not
twice in the same block, as each vote requires `currentTime > lastVoted`).

### Execute Vote Manually

1. Copy the pool addresses and vote proportions from the output
2. Go to the Voter contract on Basescan: `0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b`
3. Navigate to "Write Contract" → "vote"
4. Paste the arrays into the corresponding fields
5. Connect your wallet and execute the transaction

---

## Phase 0.2: Automated Voting System

### Components

1. **`auto_voter.py`** - Executes vote transactions with safety checks
2. **`boundary_monitor.py`** - Continuously monitors and triggers auto-voter at optimal time

### Configuration (.env file)

```bash
# Required Settings
YOUR_ADDRESS=0x768a675B8542F23C428C6672738E380176E7635C
YOUR_VOTING_POWER=1183272
MAX_GAUGES_TO_VOTE=10

# Auto-Voting Settings
AUTO_VOTE_ENABLED=false              # Set to true to enable
AUTO_VOTE_DRY_RUN=true               # Set to false for real transactions
AUTO_VOTE_TRIGGER_BLOCKS_BEFORE=20   # Trigger 20 blocks before boundary
AUTO_VOTE_MAX_GAS_PRICE_GWEI=10      # Max gas price limit
AUTO_VOTE_WALLET_KEYFILE=/path/to/keyfile.txt  # Private key file or direct key
```

### Security Guidelines

⚠️ **CRITICAL SECURITY REQUIREMENTS:**

1. **Use a dedicated voting wallet** with only enough ETH for gas fees
2. **Never commit private keys** to git (already in .gitignore)
3. **Store private key in encrypted file** outside the repository
4. **Use environment variable** for private key (preferred over file)
5. **Test with dry-run mode first** before enabling real transactions
6. **Monitor gas prices** - transactions abort if gas exceeds limit

### Option A: Manual Auto-Voter Execution

Execute a vote immediately (useful for testing):

```bash
# Dry-run test (no actual transaction)
python scripts/auto_voter.py --dry-run --skip-fresh-fetch

# Dry-run with fresh snapshot
python scripts/auto_voter.py --dry-run

# Real execution (requires private key)
python scripts/auto_voter.py --private-key-source /path/to/keyfile.txt

# Real execution with custom settings
python scripts/auto_voter.py \
  --private-key-source /path/to/keyfile.txt \
  --top-k 8 \
  --max-gas-price-gwei 15
```

### Option B: Automated Boundary Monitor

Continuously monitor and auto-trigger voting at optimal time:

```bash
# Test monitor in dry-run mode (single check)
python scripts/boundary_monitor.py --once --dry-run

# Start continuous monitoring (dry-run)
python scripts/boundary_monitor.py --dry-run

# Start continuous monitoring (REAL VOTING)
python scripts/boundary_monitor.py \
  --private-key-source /path/to/keyfile.txt \
  --trigger-blocks-before 20

# Run as background process
nohup python scripts/boundary_monitor.py \
  --private-key-source /path/to/keyfile.txt \
  > boundary_monitor.log 2>&1 &
```

**Monitor displays:**

```
🤖 Boundary Monitor Status
┌───────────────────────┬──────────────────────────────────────────┐
│ Current Block         │ 42,613,422                               │
│ Next Boundary (Epoch) │ 1772064000 (2026-02-26 02:00:00 UTC)     │
│ Boundary Block        │ 42,637,332                               │
│ Blocks Until Boundary │ 23,910                                   │
│ Trigger Threshold     │ 20 blocks before                         │
│ Status                │ MONITORING (23,890 blocks until trigger) │
│ Trigger Block         │ 42,637,312                               │
└───────────────────────┴──────────────────────────────────────────┘
```

### Safety Features

✅ **Built-in Safety Checks:**

1. **Dry-run mode** - Test without sending transactions
2. **Transaction simulation** - Validates transaction before sending
3. **Gas price limits** - Aborts if gas exceeds configured maximum
4. **Vote validation** - Ensures votes sum to voting power
5. **Wallet balance check** - Warns if insufficient gas funds
6. **Comprehensive logging** - Full audit trail of all actions
7. **Error handling** - Graceful failure with informative messages

### Testing Workflow

**Step 1: Test Vote Instructions Generator**

```bash
python scripts/generate_voting_instructions.py
```

**Step 2: Test Auto-Voter (Dry-Run)**

```bash
python scripts/auto_voter.py --dry-run --skip-fresh-fetch
```

**Step 3: Test Boundary Monitor (Single Check)**

```bash
python scripts/boundary_monitor.py --once --dry-run
```

**Step 4: Test With Fresh Snapshot (Dry-Run)**

```bash
python scripts/auto_voter.py --dry-run
```

**Step 5: Enable Real Voting (when ready)**

```bash
# Option A: One-time execution
python scripts/auto_voter.py --private-key-source /path/to/key.txt

# Option B: Automated monitoring
python scripts/boundary_monitor.py --private-key-source /path/to/key.txt
```

---

## Troubleshooting

### "No live snapshot found"

```bash
# Fetch a fresh snapshot first
python -m data.fetchers.fetch_live_snapshot
```

### "Failed to connect to RPC"

Check that `RPC_URL` is set correctly in `.env`

### "YOUR_VOTING_POWER must be > 0"

Set `YOUR_VOTING_POWER` in `.env` or pass `--your-voting-power` flag

### "Gas price exceeds limit"

Increase `AUTO_VOTE_MAX_GAS_PRICE_GWEI` or wait for lower gas prices

### "Transaction simulation failed"

- Check if you've already voted in this epoch
- Verify pool addresses are valid
- Ensure wallet has voting power

---

## Monitoring and Logs

### View Auto-Voter Activity

```bash
# If running as background process
tail -f boundary_monitor.log
```

### Check Last Vote On-Chain

```bash
# Query lastVoted for your address
cast call 0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b \
  "lastVoted(address)" <YOUR_ADDRESS>
```

### Verify Transaction

After voting, check transaction on BaseScan:

```
https://basescan.org/tx/<TRANSACTION_HASH>
```

---

## Production Deployment

### Systemd Service (Linux)

Create `/etc/systemd/system/hydrex-auto-voter.service`:

```ini
[Unit]
Description=Hydrex Auto-Voter Boundary Monitor
After=network.target

[Service]
Type=simple
User=hydrex
WorkingDirectory=/path/to/hydrex-optimiser
Environment="PATH=/path/to/venv/bin:/usr/bin"
ExecStart=/path/to/venv/bin/python scripts/boundary_monitor.py \
  --private-key-source /secure/path/keyfile.txt \
  --trigger-blocks-before 20
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
```

**Enable and start:**

```bash
sudo systemctl enable hydrex-auto-voter
sudo systemctl start hydrex-auto-voter
sudo systemctl status hydrex-auto-voter
```

### Cron Job (Alternative)

Run check every 5 minutes:

```bash
# crontab -e
*/5 * * * * cd /path/to/hydrex-optimiser && /path/to/venv/bin/python scripts/boundary_monitor.py --once >> /var/log/hydrex-auto-voter.log 2>&1
```

---

## Next Steps

- **Phase 1:** Optimize allocation strategy beyond equal weighting
- **Phase 2:** Implement marginal ROI optimization with constraints
- **Phase 3:** Add price data for USD-denominated returns
- **Phase 4:** Historical backtesting and performance analysis

---

## Support

For issues or questions:

1. Check error messages in logs
2. Verify configuration in `.env`
3. Test with `--dry-run` flag first
4. Review transaction on BaseScan if vote was sent
5. Consult `OPTIMIZATION_ROADMAP.md` for development status
