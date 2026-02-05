#!/usr/bin/env python3
"""
Debug script to check actual votes from subgraph.
"""

import json
from src.subgraph_client import SubgraphClient
from config import Config

voter = "0x768a675B8542F23C428C6672738E380176E7635C"
subgraph_client = SubgraphClient(Config.SUBGRAPH_URL)

# Epochs to check
epochs = [
    1764806400,  # 2025-12-04
    1765411200,  # 2025-12-11
    1766016000,  # 2025-12-18
    1766620800,  # 2025-12-25
    1767225600,  # 2026-01-01
    1767830400,  # 2026-01-08
    1768435200,  # 2026-01-15
]

for epoch in epochs:
    print(f"\n=== Epoch {epoch} (votes cast) ===")
    results = subgraph_client.fetch_all_paginated(
        subgraph_client.fetch_gauge_votes,
        epoch=epoch,
        voter=voter,
    )
    
    if results:
        print(f"Found {len(results)} gauge votes:")
        total_votes = 0
        for vote in results:
            weight_wei = int(vote["weight"])
            weight_normalized = weight_wei / 1e18
            gauge = vote["gauge"]["address"]
            print(f"  Gauge {gauge[:8]}...: {weight_wei} wei = {weight_normalized:.6f} normalized")
            total_votes += weight_normalized
        print(f"  Total normalized: {total_votes:.6f}")
    else:
        print("  No votes found")
