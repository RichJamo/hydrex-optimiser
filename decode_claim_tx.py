#!/usr/bin/env python3
"""Decode a VoterV5 claim transaction to extract bribe contract addresses and tokens."""

import json
import os
from web3 import Web3
from dotenv import load_dotenv

load_dotenv()

rpc_url = os.getenv("RPC_URL", "https://mainnet.base.org/")
w3 = Web3(Web3.HTTPProvider(rpc_url))

print(f"Connected to Base: {w3.is_connected()}")

# VoterV5 ABI
with open("voterv5_abi.json", "r") as f:
    voterv5_abi = json.load(f)

voter_addr = os.getenv("VOTER_ADDRESS", "").strip()
if not voter_addr:
    raise SystemExit("VOTER_ADDRESS missing in .env")

voter_addr = w3.to_checksum_address(voter_addr)
contract = w3.eth.contract(address=voter_addr, abi=voterv5_abi)

# Jan 29 claim tx hash from earlier discussion
tx_hash = os.getenv(
    "CLAIM_TX_HASH",
    "0x001c3bbc2c5dd176fbe75214e80f1ea12e9a43f48ee8608558f2108eea72343a",
)

print(f"VoterV5: {voter_addr}")
print(f"Tx: {tx_hash}")

# Fetch tx and decode input
_tx = w3.eth.get_transaction(tx_hash)
fn, args = contract.decode_function_input(_tx.input)

print("\nDecoded function:")
print(f"  {fn.fn_name}")

print("\nDecoded args:")
for key, value in args.items():
    if isinstance(value, list):
        print(f"  {key}: {len(value)} item(s)")
        # Print first few items to avoid noisy output
        for idx, item in enumerate(value[:10]):
            print(f"    [{idx}] {item}")
        if len(value) > 10:
            print("    ...")
    else:
        print(f"  {key}: {value}")

# If bribes array present, print normalized list
bribes = args.get("_bribes")
if bribes:
    print("\nBribe contract addresses:")
    for idx, addr in enumerate(bribes):
        print(f"  [{idx}] {addr}")

# If tokens array present, print shape and first few
_tokens = args.get("_tokens")
if _tokens:
    print("\nTokens per bribe:")
    for idx, tokens in enumerate(_tokens[:10]):
        print(f"  Bribe[{idx}] tokens ({len(tokens)}):")
        for token in tokens:
            print(f"    - {token}")
    if len(_tokens) > 10:
        print("  ...")
