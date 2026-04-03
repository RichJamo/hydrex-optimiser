#!/usr/bin/env python3
"""Quick diagnostic: show current on-chain epoch and time until next boundary."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
from web3 import Web3
from config.settings import VOTER_ADDRESS, WEEK

load_dotenv()

w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL")))
abi = [
    {"inputs": [], "name": "_epochTimestamp", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "epochTimestamp", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
]
voter = w3.eth.contract(address=Web3.to_checksum_address(VOTER_ADDRESS), abi=abi)
try:
    epoch_ts = voter.functions._epochTimestamp().call()
except Exception:
    epoch_ts = voter.functions.epochTimestamp().call()

next_boundary = epoch_ts + WEEK
now_utc = int(time.time())
secs_until = next_boundary - now_utc

print(f"Current epoch timestamp : {epoch_ts}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(epoch_ts))})")
print(f"Next boundary           : {next_boundary}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(next_boundary))})")
print(f"Now (UTC)               : {now_utc}  ({time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(now_utc))})")
print(f"Seconds until boundary  : {secs_until:,}  ({secs_until // 3600}h {(secs_until % 3600) // 60}m {secs_until % 60}s)")
