#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from database import Vote

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

YOUR_GAUGES = {
    "0x07388f67042bc2dc54876e0c99e543625bd2a9da",
    "0x22f0afdda80fbca0d96e29384814a897cbadab59",
    "0xac396cabf5832a49483b78225d902c0999829993",
    "0xee5f8bf7cdb1ad421993a368b15d06ad58122dab",
}

db = Database('data.db')
session = db.get_session()
client = SubgraphClient()

print(f"Searching for all your votes across epochs...")
print()

# Get all epochs from the database
epochs = session.query(Vote.epoch).distinct().order_by(Vote.epoch.desc()).all()
epochs = [e[0] for e in epochs]

print(f"Found {len(epochs)} epochs in database")
print()

your_votes_by_epoch = {}

for epoch in epochs:
    for gauge in YOUR_GAUGES:
        votes = client.fetch_all_paginated(
            client.fetch_gauge_votes,
            epoch=epoch,
            gauge=gauge,
        )
        your_votes = sum(float(v["weight"]) / 1e18 for v in votes if v["voter"].lower() == YOUR_ESCROW.lower())
        
        if your_votes > 0:
            epoch_date = datetime.utcfromtimestamp(epoch).strftime('%Y-%m-%d')
            if epoch not in your_votes_by_epoch:
                your_votes_by_epoch[epoch] = {}
            your_votes_by_epoch[epoch][gauge] = your_votes

if your_votes_by_epoch:
    print("Epochs where you have votes:")
    print()
    for epoch in sorted(your_votes_by_epoch.keys(), reverse=True):
        epoch_date = datetime.utcfromtimestamp(epoch).strftime('%Y-%m-%d')
        total = sum(your_votes_by_epoch[epoch].values())
        print(f"{epoch_date} (ts={epoch}):")
        for gauge, votes in sorted(your_votes_by_epoch[epoch].items()):
            print(f"  {gauge[:10]}... {votes:,.2f}")
        print(f"  Total: {total:,.2f}")
        print()
else:
    print("No votes found for your escrow address")

session.close()
