#!/usr/bin/env python3
import sys
sys.path.insert(0, 'src')

from subgraph_client import SubgraphClient
import json

client = SubgraphClient()

# Jan 22 epoch
epoch_ts = 1769040000

print(f"Querying subgraph for bribes in epoch {epoch_ts}...\n")

# Query all bribes for this epoch
query = f"""
{{
  bribes(first: 1000, where: {{epoch: {epoch_ts}}}) {{
    id
    epoch
    bribeContract
    rewardToken
    amount
    blockNumber
    blockTimestamp
  }}
}}
"""

result = client.query(query)
bribes = result.get("bribes", [])

print(f"Found {len(bribes)} bribe records\n")

# Group by token to understand distribution
token_totals = {}
bribe_contract_count = {}

for bribe in bribes:
    token = bribe['rewardToken'].lower()
    bribe_contract = bribe['bribeContract'].lower()
    amount = float(bribe['amount'])
    
    if token not in token_totals:
        token_totals[token] = 0
    token_totals[token] += amount
    
    if bribe_contract not in bribe_contract_count:
        bribe_contract_count[bribe_contract] = 0
    bribe_contract_count[bribe_contract] += 1

print("=" * 100)
print("BRIBES BY TOKEN (with totals):")
print("=" * 100)
for token in sorted(token_totals.keys(), key=lambda x: token_totals[x], reverse=True)[:15]:
    total = token_totals[token]
    print(f"{token[:8]}...{token[-8:]}: {total:>20,.2f} tokens")

print("\n" + "=" * 100)
print("BRIBE CONTRACTS (frequency count):")
print("=" * 100)
for contract in sorted(bribe_contract_count.keys(), key=lambda x: bribe_contract_count[x], reverse=True)[:15]:
    count = bribe_contract_count[contract]
    print(f"{contract[:8]}...{contract[-8:]}: {count:>5} bribes")

# Now check what's in our database
print("\n" + "=" * 100)
print("CHECKING OUR DATABASE:")
print("=" * 100)

from database import Database, Bribe

db = Database('data.db')
session = db.get_session()

db_bribes = session.query(Bribe).filter(Bribe.epoch == epoch_ts).all()
print(f"Our database has {len(db_bribes)} bribe records for this epoch")

# Group by token in DB
db_token_totals = {}
for bribe in db_bribes:
    token = bribe.reward_token.lower()
    if token not in db_token_totals:
        db_token_totals[token] = 0
    db_token_totals[token] += bribe.amount

print("\nOur database bribes by token (top 15):")
for token in sorted(db_token_totals.keys(), key=lambda x: db_token_totals[x], reverse=True)[:15]:
    total = db_token_totals[token]
    print(f"{token[:8]}...{token[-8:]}: {total:>20,.2f} tokens")

# Compare
print("\n" + "=" * 100)
print("COMPARISON:")
print("=" * 100)
all_tokens = set(token_totals.keys()) | set(db_token_totals.keys())
print(f"\nTokens in subgraph: {len(token_totals)}")
print(f"Tokens in our DB: {len(db_token_totals)}")
print(f"Tokens in EITHER: {len(all_tokens)}")

missing_in_db = set(token_totals.keys()) - set(db_token_totals.keys())
if missing_in_db:
    print(f"\n{len(missing_in_db)} tokens in subgraph but NOT in our DB:")
    for token in sorted(missing_in_db)[:10]:
        print(f"  {token} ({token_totals[token]:,.2f} tokens)")
