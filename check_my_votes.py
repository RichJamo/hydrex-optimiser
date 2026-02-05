#!/usr/bin/env python3
"""Check actual votes from your escrow account."""

from src.database import Database
from src.database import Vote
from config import Config

db = Database(Config.DATABASE_PATH)
escrow_addr = Config.YOUR_ADDRESS.lower()

print(f"Checking votes for: {escrow_addr}")
print("=" * 80)

with db.get_session() as session:
    # Get votes for your address
    votes = session.query(Vote).filter_by(voter=escrow_addr).order_by(Vote.epoch.desc()).all()
    
    if votes:
        print(f"\nFound {len(votes)} vote records for your escrow account")
        print("\nMost recent votes:")
        for v in votes[:10]:
            print(f"  Epoch {v.epoch}: Gauge {v.gauge[:10]}... = {v.votes:,.0f} votes")
        
        # Group by epoch
        epochs = {}
        for v in votes:
            if v.epoch not in epochs:
                epochs[v.epoch] = []
            epochs[v.epoch].append(v)
        
        print(f"\nYou voted in {len(epochs)} epochs")
        print("\nVotes per epoch:")
        for epoch in sorted(epochs.keys(), reverse=True)[:5]:
            gauge_count = len(epochs[epoch])
            total_votes = sum(v.votes for v in epochs[epoch])
            print(f"  Epoch {epoch}: {gauge_count} gauges, {total_votes:,.0f} total votes")
    else:
        print("\nNo votes found for your escrow account.")
        print("The votes table contains aggregated data (total_votes per gauge).")
        print("We need to fetch individual voter data from the blockchain.\n")
        
        # Check what we have in the votes table
        total = session.query(Vote).count()
        print(f"Total vote records in database: {total}")
        
        # Show sample
        sample = session.query(Vote).first()
        if sample:
            print(f"\nSample vote record structure:")
            print(f"  voter: {sample.voter[:20]}...")
            print(f"  gauge: {sample.gauge[:20]}...")
            print(f"  votes: {sample.votes:,.0f}")
            print(f"  epoch: {sample.epoch}")
