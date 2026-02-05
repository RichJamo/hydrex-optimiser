#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from config import Config
from subgraph_client import SubgraphClient

load_dotenv()

escrow = "0x768a675B8542F23C428C6672738E380176E7635C"
client = SubgraphClient()

# List of epochs from the analysis output
epochs_to_check = [
    1764201600,  # around 2025-12-04
    1764806400,  # around 2025-12-11
    1765411200,  # around 2025-12-18
    1766016000,  # around 2025-12-25
    1766620800,  # around 2026-01-01
    1767225600,  # around 2026-01-08
    1767830400,  # around 2026-01-15
    1768435200,  # around 2026-01-22
    1769040000,  # around 2026-01-29
]

print(f"Checking actual votes for {escrow} across epochs:")
print(f"{'Date':<12} {'Timestamp':<12} {'Vote Count':<12} {'Total Wei':<30} {'Normalized':<15}")
print("-" * 80)

for epoch_ts in epochs_to_check:
    date = datetime.utcfromtimestamp(epoch_ts).strftime('%Y-%m-%d')
    actual_votes = client.fetch_all_paginated(
        client.fetch_gauge_votes,
        epoch=epoch_ts,
        voter=escrow,
    )
    
    if actual_votes:
        total_wei = sum(float(v["weight"]) for v in actual_votes)
        total_norm = total_wei / 1e18
        print(f"{date:<12} {epoch_ts:<12} {len(actual_votes):<12} {total_wei:<30.0f} {total_norm:<15,.2f}")
    else:
        print(f"{date:<12} {epoch_ts:<12} {'0':<12} {'0':<30} {'0':<15}")

print("-" * 80)
