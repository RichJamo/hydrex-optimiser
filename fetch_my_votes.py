#!/usr/bin/env python3
"""
Fetch actual votes from your escrow account on-chain.
This will query the Voter contract for your historical vote allocations.
"""

import logging
from datetime import datetime
from web3 import Web3
from config import Config
from src.database import Database

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Connect to Base
w3 = Web3(Web3.HTTPProvider("https://mainnet.base.org"))

# Voter contract address and minimal ABI
VOTER_ADDRESS = "0xeBaC9d4Ab86FcA7E89fCa84C38672AfA18e42845"
VOTER_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "tokenId", "type": "address"}],
        "name": "poolVote",
        "outputs": [{"internalType": "address[]", "name": "", "type": "address[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"internalType": "address", "name": "tokenId", "type": "address"},
            {"internalType": "address", "name": "_pool", "type": "address"}
        ],
        "name": "votes",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

voter = w3.eth.contract(address=w3.to_checksum_address(VOTER_ADDRESS), abi=VOTER_ABI)

def get_escrow_votes_for_epoch(escrow_address: str) -> dict:
    """
    Get current vote allocations for an escrow account.
    
    Note: This gets the CURRENT state, not historical per-epoch state.
    To get historical data, we'd need to query events or use archive nodes with block numbers.
    """
    escrow_checksum = w3.to_checksum_address(escrow_address)
    
    try:
        # Get list of pools/gauges this escrow has voted for
        pools = voter.functions.poolVote(escrow_checksum).call()
        
        if not pools:
            logger.info(f"No votes found for {escrow_address}")
            return {}
        
        # Get vote amounts for each pool
        allocations = {}
        total_votes = 0
        
        for pool in pools:
            votes = voter.functions.votes(escrow_checksum, pool).call()
            if votes > 0:
                allocations[pool.lower()] = votes
                total_votes += votes
        
        logger.info(f"Found {len(allocations)} gauge allocations, total {total_votes:,} votes")
        return allocations
        
    except Exception as e:
        logger.error(f"Error fetching votes: {e}")
        return {}

def main():
    print("=" * 80)
    print("FETCHING CURRENT VOTES FROM BLOCKCHAIN")
    print(f"Escrow Address: {Config.YOUR_ADDRESS}")
    print("=" * 80)
    print()
    
    # Get current votes
    allocations = get_escrow_votes_for_epoch(Config.YOUR_ADDRESS)
    
    if allocations:
        print(f"Current vote allocation ({len(allocations)} gauges):")
        print()
        
        # Sort by votes descending
        sorted_allocs = sorted(allocations.items(), key=lambda x: x[1], reverse=True)
        
        total = sum(allocations.values())
        for gauge, votes in sorted_allocs:
            pct = (votes / total * 100) if total > 0 else 0
            print(f"  {gauge[:10]}... : {votes:>12,} votes ({pct:>5.1f}%)")
        
        print()
        print(f"Total votes allocated: {total:,}")
    else:
        print("No current votes found on-chain.")
    
    print()
    print("=" * 80)
    print("NOTE: This shows CURRENT votes only.")
    print("Historical per-epoch votes require event logs or archive node queries.")
    print("The votes table in our database stores TOTAL votes per gauge (all voters),")
    print("not individual voter allocations.")
    print("=" * 80)

if __name__ == "__main__":
    main()
