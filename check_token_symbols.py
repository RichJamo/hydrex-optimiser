from web3 import Web3
import os
import time
from config import Config

w3 = Web3(Web3.HTTPProvider(Config.RPC_URL, request_kwargs={"timeout": Config.RPC_TIMEOUT}))

ERC20_SYMBOL_ABI = [
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]

# Top token addresses from the bribes
addresses = [
    "0x051024b653e8ec69e72693f776c41c2a9401fb07",
    "0xa9f6d9eca1f803854a13cecad0f21d43e007db07",
    "0x7f6f8bb1aa8206921e80ab6abf1ac5737e39ab07",
    "0xd302a92fb82ea59aa676ae3d5799ac296afa7390",
    "0x77ca224436b132cd83581826669025ed9cfd9b94",
]

for addr in addresses:
    try:
        contract = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_SYMBOL_ABI)
        symbol = contract.functions.symbol().call()
        if isinstance(symbol, bytes):
            symbol = symbol.decode("utf-8").rstrip("\x00")
        print(f"{addr}: {symbol}")
        time.sleep(0.1)
    except Exception as e:
        print(f"{addr}: ERROR - {str(e)[:80]}")
