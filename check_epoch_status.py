#!/usr/bin/env python3
"""
Check current epoch status and vote timing.
"""

import json
from web3 import Web3
from datetime import datetime, timezone, timedelta

# Configuration
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"

# Setup Web3
w3 = Web3(Web3.HTTPProvider(RPC_URL))
print(f"Connected to Base: {w3.is_connected()}\n")

# Load ABI
with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_ADDRESS),
    abi=voter_abi
)

print("=" * 80)
print("EPOCH STATUS CHECK")
print("=" * 80)

# Get current epoch timestamp from contract
epoch_timestamp = voter.functions._epochTimestamp().call()
epoch_dt = datetime.fromtimestamp(epoch_timestamp, tz=timezone.utc)

print(f"\nContract's current epoch timestamp: {epoch_timestamp}")
print(f"Epoch date/time: {epoch_dt.strftime('%Y-%m-%d %H:%M:%S UTC')} ({epoch_dt.strftime('%A')})")

# Calculate when this epoch started and ends
now = datetime.now(timezone.utc)
print(f"\nCurrent time: {now.strftime('%Y-%m-%d %H:%M:%S UTC')} ({now.strftime('%A')})")

time_since_epoch = now - epoch_dt
print(f"\nTime since epoch started: {time_since_epoch}")

# Calculate next epoch
epoch_duration = timedelta(weeks=1)
next_epoch = epoch_dt + epoch_duration
print(f"Next epoch flip: {next_epoch.strftime('%Y-%m-%d %H:%M:%S UTC')} ({next_epoch.strftime('%A')})")

time_until_flip = next_epoch - now
print(f"Time until next flip: {time_until_flip}")

# Get total weight for current and previous epochs
total_weight_current = voter.functions.totalWeight().call()
print(f"\n✓ Total votes in CURRENT epoch ({epoch_timestamp}): {total_weight_current:,}")

# Try previous epoch (1 week ago)
previous_epoch = epoch_timestamp - 604800  # 1 week in seconds
try:
    total_weight_previous = voter.functions.totalWeightAt(previous_epoch).call()
    print(f"✓ Total votes in PREVIOUS epoch ({previous_epoch}): {total_weight_previous:,}")
    
    previous_epoch_dt = datetime.fromtimestamp(previous_epoch, tz=timezone.utc)
    print(f"  Previous epoch was: {previous_epoch_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
except Exception as e:
    print(f"Could not get previous epoch weight: {e}")

print("\n" + "=" * 80)
print("VOTING MECHANICS SUMMARY")
print("=" * 80)
print("""
If you vote NOW:
  - Votes apply to CURRENT epoch (earning bribes at next epoch flip)
  - Bribes locked in at the moment of epoch flip (Wednesday 00:00 UTC)
  - You can claim rewards in the NEXT epoch (after Wednesday flip)

Current situation:
  - We just flipped to a new epoch (or very recent flip)
  - Most voters haven't voted yet for this epoch
  - Current epoch has almost no votes yet
  - This is actually IDEAL timing - you can see accumulated fees!
""")
print("=" * 80)
