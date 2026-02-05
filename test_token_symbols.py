#!/usr/bin/env python3
from web3 import Web3
from config import Config

ERC20_SYMBOL_ABI = [{
    "constant": True,
    "inputs": [],
    "name": "symbol",
    "outputs": [{"name": "", "type": "string"}],
    "type": "function",
}]

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL, request_kwargs={"timeout": Config.RPC_TIMEOUT}))

test_tokens = [
    ("0x4200000000000000000000000000000000000006", "WETH"),
    ("0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USDC"),
    ("0xa1136031150e50b015b41f1ca6b2e99e49d8cb78", "oHYDX"),
    ("0x00000e7efa313f4e11bfff432471ed9423ac6b30", "HYDX"),
    ("0xff8104251e7761163fac3211ef5583fb3f8583d6", "REPPO"),
]

print("Testing RPC symbol() calls:")
print()

for addr, expected in test_tokens:
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_SYMBOL_ABI)
        symbol = contract.functions.symbol().call()
        if isinstance(symbol, bytes):
            symbol = symbol.decode("utf-8").rstrip("\x00")
        print(f"✓ {addr}: {symbol} (expected: {expected})")
    except Exception as e:
        print(f"✗ {addr}: ERROR - {str(e)[:80]} (expected: {expected})")
