#!/usr/bin/env python3
"""
Analyze different vote sources to understand scale factors.
"""

import json
from src.database import Database
from src.subgraph_client import SubgraphClient
from config import Config

db = Database(Config.DATABASE_PATH)
subgraph = SubgraphClient(Config.SUBGRAPH_URL)
voter = "0x768a675B8542F23C428C6672738E380176E7635C"

# Check 2025-12-04 (epoch 1764806400)
epoch = 1764806400
next_epoch = epoch + Config.EPOCH_DURATION

print(f"\n{'='*100}")
print(f"ANALYZING EPOCH {epoch} (next_epoch {next_epoch})")
print(f"{'='*100}\n")

# 1. Get subgraph votes
print("1. SUBGRAPH VOTES (GaugeVote events):")
subgraph_votes = subgraph.fetch_all_paginated(
    subgraph.fetch_gauge_votes,
    epoch=epoch,
    voter=voter,
)

subgraph_by_gauge = {}
for vote in subgraph_votes:
    gauge = vote["gauge"]["address"].lower()
    weight_wei = int(vote["weight"])
    subgraph_by_gauge[gauge] = weight_wei
    print(f"  {gauge[:10]}...: {weight_wei:>25} wei")

total_subgraph_wei = sum(subgraph_by_gauge.values())
print(f"  Total subgraph wei: {total_subgraph_wei}\n")

# 2. Get database votes
print("2. DATABASE VOTES (Vote table for this epoch):")
db_votes = db.get_votes_for_epoch(epoch)

db_by_gauge = {}
for vote in db_votes:
    gauge = vote.gauge.lower()
    db_by_gauge[gauge] = vote.total_votes
    if gauge in subgraph_by_gauge:
        print(f"  {gauge[:10]}...: {vote.total_votes:>20.2f} (subgraph: {subgraph_by_gauge[gauge]:>25} wei)")
    else:
        print(f"  {gauge[:10]}...: {vote.total_votes:>20.2f} (NO SUBGRAPH DATA)")

total_db_votes = sum(v.total_votes for v in db_votes)
print(f"  Total DB votes: {total_db_votes:>20.2f}\n")

# 3. Get bribes for next epoch
print(f"3. BRIBES FOR NEXT EPOCH {next_epoch}:")
bribes = db.get_bribes_for_epoch(next_epoch)

# Build bribe->gauge mapping
bribe_to_gauge = {}
for gauge in db.get_all_gauges():
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = gauge.address.lower()
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = gauge.address.lower()

bribes_by_gauge = {}
for bribe in bribes:
    gauge = bribe_to_gauge.get(bribe.bribe_contract.lower())
    if gauge:
        if gauge not in bribes_by_gauge:
            bribes_by_gauge[gauge] = 0
        bribes_by_gauge[gauge] += 1

print(f"  Total bribes: {len(bribes)}")
print(f"  Gauges with bribes: {len(bribes_by_gauge)}\n")

# 4. Calculate scale factors
print("4. SCALE FACTOR ANALYSIS:")
scale_factors = []
for gauge in db_by_gauge:
    if gauge in subgraph_by_gauge:
        db_val = db_by_gauge[gauge]
        sg_wei = subgraph_by_gauge[gauge]
        if db_val > 0:
            scale = sg_wei / db_val
            scale_factors.append(scale)
            print(f"  {gauge[:10]}...: {sg_wei:>25} wei / {db_val:>20.2f} = {scale:>15.2f}")

if scale_factors:
    scale_factors.sort()
    median_scale = scale_factors[len(scale_factors)//2]
    mean_scale = sum(scale_factors) / len(scale_factors)
    min_scale = scale_factors[0]
    max_scale = scale_factors[-1]
    
    print(f"\n  Scale factor statistics:")
    print(f"    Min:    {min_scale:>15.2f}")
    print(f"    Median: {median_scale:>15.2f}")
    print(f"    Mean:   {mean_scale:>15.2f}")
    print(f"    Max:    {max_scale:>15.2f}")
    print(f"    Range:  {(max_scale/min_scale):.2f}x difference\n")

# 5. Show what your actual allocation would be at different scales
print("5. YOUR ACTUAL SHARE AT DIFFERENT SCALES:")
print(f"  (assuming you voted equally on {len(subgraph_by_gauge)} gauges)")

for gauge in list(subgraph_by_gauge.keys())[:3]:
    if gauge in db_by_gauge:
        print(f"\n  Gauge {gauge[:10]}...:")
        sg_wei = subgraph_by_gauge[gauge]
        db_total = db_by_gauge[gauge]
        
        print(f"    Subgraph weight: {sg_wei:>25} wei")
        print(f"    DB total votes:  {db_total:>20.2f}")
        
        print(f"    Your share (no normalization): {sg_wei / db_total:.2%}")
        print(f"    Your share (median scale):     {(sg_wei / median_scale) / db_total:.2%}")
        print(f"    Your share (mean scale):       {(sg_wei / mean_scale) / db_total:.2%}")

print(f"\n{'='*100}\n")
