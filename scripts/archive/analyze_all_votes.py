#!/usr/bin/env python3
import os
import sys
from datetime import datetime
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from database import Database
from subgraph_client import SubgraphClient
from database import Vote, Bribe, Gauge
from price_feed import PriceFeed
from token_utils import get_token_decimals, get_token_symbol, prefetch_token_metadata
from web3 import Web3

load_dotenv()

YOUR_ESCROW = "0x768a675B8542F23C428C6672738E380176E7635C"

# Initialize Web3 for contract calls
RPC_URL = os.getenv("RPC_URL", "https://base.llamarpc.com")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

# Gauge contract ABI - just need stakeToken() function
GAUGE_ABI = [
    {
        "inputs": [],
        "name": "stakeToken",
        "outputs": [{"internalType": "address", "name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    }
]

client = SubgraphClient()
db = Database('data.db')
db.create_tables()
session = db.get_session()
price_feed = PriceFeed(database=db)

unresolved_token_addresses = set()

print(f"Building comprehensive analysis of your votes across ALL epochs and gauges...")
print()

# Step 1: Find all epochs where you voted
print("[DEBUG] Fetching votes from subgraph...", flush=True)
query = """
{
  gaugeVotes(first: 1000, where: {voter: "%s"}) {
    epoch
    gauge {
      address
    }
    weight
  }
}
""" % YOUR_ESCROW.lower()

import requests
subgraph_url = os.getenv("SUBGRAPH_URL")
print(f"[DEBUG] Using subgraph URL: {subgraph_url}", flush=True)

response = requests.post(
    subgraph_url,
    json={"query": query},
    timeout=30
)

print("[DEBUG] Subgraph response received, parsing...", flush=True)
result = response.json()

print(f"[DEBUG] Response parsed successfully", flush=True)

if not ("data" in result and result["data"] and result["data"].get("gaugeVotes")):
    print("Error fetching votes:", result)
    sys.exit(1)

all_votes = result["data"]["gaugeVotes"]

print(f"[DEBUG] Got {len(all_votes)} votes from subgraph", flush=True)
your_votes_by_epoch = {}
for vote in all_votes:
    epoch = int(vote["epoch"])
    gauge = vote["gauge"]["address"].lower()
    weight = float(vote["weight"]) / 1e18
    
    if epoch not in your_votes_by_epoch:
        your_votes_by_epoch[epoch] = {}
    your_votes_by_epoch[epoch][gauge] = weight

# Helper function - in the subgraph, the "gauge" field is actually the pool address
# We use gauge addresses directly since they are the pool identifiers for voting
def normalize_gauge_address(gauge_address):
    """Normalize gauge address to lowercase."""
    return gauge_address.lower() if gauge_address else None

# Helper function to fetch total votes per pool for an epoch from subgraph
def get_gauge_total_votes(epoch, gauges):
    """Fetch ALL votes (from all voters) per gauge for a given epoch from subgraph.
    
    The 'gauge' field in gaugeVotes is actually the pool address in the subgraph.
    """
    print(f"[DEBUG] Fetching total votes per gauge for epoch {epoch}...", flush=True)
    
    gauge_totals = {}
    
    for gauge_addr in gauges:
        if gauge_addr is None:
            continue
        
        query = """
        {
          gaugeVotes(first: 1000, where: {epoch: %d, gauge: "%s"}) {
            weight
          }
        }
        """ % (epoch, gauge_addr)
        
        print(f"[DEBUG] Query for gauge {gauge_addr[:16]}...: epoch={epoch}, gauge={gauge_addr}", flush=True)
        
        try:
            response = requests.post(
                subgraph_url,
                json={"query": query},
                timeout=30
            )
            result = response.json()
            
            if "errors" in result:
                print(f"[DEBUG] ERROR for gauge {gauge_addr[:16]}...: {result['errors']}", flush=True)
                gauge_totals[gauge_addr] = 0
            elif "data" in result and result["data"] and result["data"].get("gaugeVotes"):
                total_weight = sum(float(vote["weight"]) / 1e18 for vote in result["data"]["gaugeVotes"])
                gauge_totals[gauge_addr] = total_weight
                if total_weight > 0:
                    print(f"[DEBUG] Gauge {gauge_addr[:16]}...: {total_weight:,.2f} total votes", flush=True)
            else:
                print(f"[DEBUG] No votes found for gauge {gauge_addr[:16]}...", flush=True)
                gauge_totals[gauge_addr] = 0
        except Exception as e:
            print(f"[DEBUG] Error fetching votes for gauge {gauge_addr[:16]}...: {e}", flush=True)
            gauge_totals[gauge_addr] = 0
    
    print(f"[DEBUG] Got total votes for {len(gauge_totals)} gauges", flush=True)
    return gauge_totals

print(f"Found {len(your_votes_by_epoch)} voting epochs with {len(all_votes)} total votes")
print()

# Step 2: For each voting epoch, calculate returns from bribes in that epoch
# (since bribes from epoch N apply to returns in epoch N+1)

print("=" * 100)
print("COMPREHENSIVE RETURN ANALYSIS")
print("=" * 100)
print()

# Build bribe_contract -> (gauge, bribe_type) mapping
bribe_to_gauge = {}
for gauge in session.query(Gauge).all():
    if gauge.internal_bribe:
        bribe_to_gauge[gauge.internal_bribe.lower()] = (gauge.address.lower(), "fee")
    if gauge.external_bribe:
        bribe_to_gauge[gauge.external_bribe.lower()] = (gauge.address.lower(), "bribe")

total_returns = 0.0

for vote_epoch in sorted(your_votes_by_epoch.keys(), reverse=True):
    print(f"[DEBUG] ========== Processing epoch {vote_epoch} ==========", flush=True)
    bribe_epoch = vote_epoch  # Bribes from vote_epoch apply to returns in reward_epoch
    reward_epoch = vote_epoch + 604800  # Rewards earned in the next epoch (1 week later)
    
    vote_epoch_date = datetime.utcfromtimestamp(vote_epoch).strftime('%Y-%m-%d')
    reward_epoch_date = datetime.utcfromtimestamp(reward_epoch).strftime('%Y-%m-%d')
    
    your_gauges_this_epoch = your_votes_by_epoch[vote_epoch]
    total_votes_cast = sum(your_gauges_this_epoch.values())
    
    # Fetch total votes per gauge for this epoch from subgraph
    # (gauge addresses are the pool identifiers in the subgraph)
    gauge_total_votes = get_gauge_total_votes(bribe_epoch, your_gauges_this_epoch.keys())
    
    print(f"\nVoting Epoch: {vote_epoch_date} (ts={vote_epoch})")
    print(f"Reward Epoch: {reward_epoch_date} (ts={reward_epoch})")
    print(f"Total votes cast: {total_votes_cast:,.2f} across {len(your_gauges_this_epoch)} gauges")
    print(f"(Bribes submitted in {vote_epoch_date} → rewards earned in {reward_epoch_date})")
    print()
    
    print(f"[DEBUG] Fetching bribes from database for epoch {bribe_epoch}...", flush=True)
    # Fetch bribes for this epoch
    bribes = session.query(Bribe).filter(
        Bribe.epoch == bribe_epoch
    ).all()
    
    print(f"[DEBUG] Got {len(bribes)} bribes", flush=True)
    
    if not bribes:
        print(f"  No bribes found for epoch {bribe_epoch}")
        print()
        continue
    
    # Pre-fetch token metadata once per epoch (caches in DB for future runs)
    bribes_list = [{"token_address": b.reward_token} for b in bribes]
    prefetch_token_metadata(db, bribes_list)
    
    print(f"[DEBUG] Fetching prices for {len(set(b.reward_token for b in bribes))} unique tokens...", flush=True)
    # Get prices for this epoch
    unique_tokens = list(set(b.reward_token for b in bribes))
    token_prices = price_feed.get_batch_prices_for_timestamp(unique_tokens, bribe_epoch, granularity="day")
    
    print(f"[DEBUG] Got prices for {len(token_prices)} tokens", flush=True)
    
    # Calculate bribes per gauge with token breakdown
    bribes_by_gauge = {}
    tokens_by_gauge = {}  # Track individual token amounts and values
    token_totals_by_type = {}  # Track totals by token and type (fee/bribe)
    print(f"[DEBUG] Processing {len(bribes)} bribes...", flush=True)
    bribe_count = 0
    for bribe in bribes:
        bribe_count += 1
        if bribe_count % 100 == 0:
            print(f"[DEBUG] Processed {bribe_count} bribes...", flush=True)
        gauge_entry = bribe_to_gauge.get(bribe.bribe_contract.lower())
        if gauge_entry:
            gauge_addr, bribe_type = gauge_entry
            if gauge_addr not in bribes_by_gauge:
                bribes_by_gauge[gauge_addr] = 0.0
                tokens_by_gauge[gauge_addr] = []
            
            # Convert raw amount to token units using decimals
            raw_amount = None
            if bribe.amount_wei:
                try:
                    raw_amount = int(bribe.amount_wei)
                except (ValueError, TypeError):
                    raw_amount = None
            decimals = get_token_decimals(bribe.reward_token, database=db)
            if raw_amount is not None:
                token_amount = raw_amount / (10**decimals)
            else:
                token_amount = float(bribe.amount)
            
            symbol = get_token_symbol(bribe.reward_token, database=db)
            if "..." in symbol:
                unresolved_token_addresses.add(bribe.reward_token.lower())
            
            price = token_prices.get(bribe.reward_token.lower(), 0.0)
            value = token_amount * price
            bribes_by_gauge[gauge_addr] += value
            
            # Store token info for detailed breakdown
            tokens_by_gauge[gauge_addr].append({
                'token': bribe.reward_token.lower(),
                'symbol': symbol,
                'type': bribe_type,
                'amount': token_amount,
                'price': price,
                'value': value
            })
            
            # Totals by token/type are calculated after applying your share per gauge
    
    print(f"[DEBUG] Finished processing all {bribe_count} bribes", flush=True)
    print(f"[DEBUG] Calculating your returns from {len(your_gauges_this_epoch)} gauges...", flush=True)
    epoch_returns = 0.0
    print(f"{'Gauge':<12} {'Your Votes':<18} {'DB Total':<18} {'Your Share':<12} {'Your Return':<15}")
    print("-" * 105)
    
    for gauge in sorted(your_gauges_this_epoch.keys()):
        your_votes = your_gauges_this_epoch[gauge]
        
        # Get total votes for this gauge from subgraph (not from Vote table)
        gauge_total = gauge_total_votes.get(gauge, 0)
        
        your_share = your_votes / gauge_total if gauge_total > 0 else 0
        gauge_bribes = bribes_by_gauge.get(gauge, 0.0)
        your_return = gauge_bribes * your_share
        
        epoch_returns += your_return
        
        print(f"{gauge[:10]}... {your_votes:>17,.2f} {gauge_total:>17,.2f} {your_share:>10.2%} ${your_return:>13,.2f}")
        
        # Show token breakdown for this gauge if you have a return
        if gauge in tokens_by_gauge:
            print(f"  Token breakdown:")
            # Group by token and sum amounts
            token_summary = {}
            for token_info in tokens_by_gauge[gauge]:
                token_key = (token_info['token'], token_info['type'])
                if token_key not in token_summary:
                    token_summary[token_key] = {
                        'symbol': token_info['symbol'],
                        'type': token_info['type'],
                        'amount': 0,
                        'price': token_info['price'],
                        'value': 0,
                    }
                token_summary[token_key]['amount'] += token_info['amount']
                token_summary[token_key]['value'] += token_info['value']
            
            # Sort by value descending
            sorted_tokens = sorted(token_summary.items(), key=lambda x: x[1]['value'], reverse=True)
            for _, info in sorted_tokens[:10]:  # Show top 10 tokens
                token_label = info['symbol']
                token_type = info['type']
                your_token_amount = info['amount'] * your_share
                your_token_value = info['value'] * your_share
                if token_label not in token_totals_by_type:
                    token_totals_by_type[token_label] = {"fee": 0.0, "bribe": 0.0}
                token_totals_by_type[token_label][token_type] += your_token_amount
                if your_token_value > 0.01:  # Only show tokens worth > $0.01
                    print(
                        f"    {token_label:<10} {your_token_amount:>15,.4f} × ${info['price']:<12.6f} "
                        f"= ${your_token_value:>10,.2f} ({token_type})"
                    )
            print()
    
    print()
    print(f"Epoch return: ${epoch_returns:,.2f}")
    print()

    if token_totals_by_type:
        print("Token totals (fee vs bribe):")
        print(f"{'Token':12s} {'Fee':>20s} {'Bribe':>20s}")
        print("-" * 60)
        for symbol in sorted(token_totals_by_type.keys()):
            fee_amount = token_totals_by_type[symbol]["fee"]
            bribe_amount = token_totals_by_type[symbol]["bribe"]
            print(f"{symbol:12s} {fee_amount:>20,.4f} {bribe_amount:>20,.4f}")
        print()
    
    total_returns += epoch_returns

print("\n[DEBUG] Analysis complete!", flush=True)
print("=" * 100)
print(f"TOTAL CALCULATED RETURNS: ${total_returns:,.2f}")
print("=" * 100)

# Show unresolved token addresses, if any
if unresolved_token_addresses:
    print("\nUnresolved token symbols (please provide symbols if known):")
    for addr in sorted(unresolved_token_addresses):
        print(f"  {addr}")

session.close()
