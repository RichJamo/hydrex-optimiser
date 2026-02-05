#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
import requests

load_dotenv()

RPC_URL = os.getenv("RPC_URL")
EPOCH_DURATION = 604800  # 1 week in seconds

block_number = 41412921

print(f"Fetching block {block_number} from RPC...")
print()

# Query the block
response = requests.post(
    RPC_URL,
    json={
        "jsonrpc": "2.0",
        "method": "eth_getBlockByNumber",
        "params": [hex(block_number), False],
        "id": 1,
    },
    timeout=30
)

result = response.json()

if "result" in result and result["result"]:
    block = result["result"]
    timestamp = int(block["timestamp"], 16)
    block_date = datetime.utcfromtimestamp(timestamp).strftime('%Y-%m-%d %H:%M:%S UTC')
    
    print(f"Block {block_number} timestamp: {timestamp}")
    print(f"Block {block_number} date: {block_date}")
    print()
    
    # Calculate which epoch this belongs to
    # Epochs start at different times; let's find the epoch boundary
    # We know some epochs: 1767830400 (2026-01-08), 1768435200 (2026-01-15), etc.
    
    # Find the epoch start
    epoch_start = (timestamp // EPOCH_DURATION) * EPOCH_DURATION
    epoch_end = epoch_start + EPOCH_DURATION
    
    epoch_start_date = datetime.utcfromtimestamp(epoch_start).strftime('%Y-%m-%d')
    epoch_end_date = datetime.utcfromtimestamp(epoch_end).strftime('%Y-%m-%d')
    
    print(f"Block falls in epoch: {epoch_start_date} to {epoch_end_date}")
    print(f"Epoch timestamp: {epoch_start}")
    print()
    
    # Check if this is the 2026-01-22 to 2026-01-29 epoch
    expected_epoch = 1769040000  # 2026-01-22
    expected_date = datetime.utcfromtimestamp(expected_epoch).strftime('%Y-%m-%d')
    
    print(f"Expected epoch for Jan 28 vote: {expected_date} (ts={expected_epoch})")
    print(f"Calculated epoch: {epoch_start_date} (ts={epoch_start})")
    
    if epoch_start == expected_epoch:
        print("✓ MATCH - Vote should be in epoch 1769040000")
    else:
        print(f"✗ MISMATCH - Vote is in epoch {epoch_start}, not {expected_epoch}")

else:
    print("Error fetching block:", result)
