"""
Diagnostic: probe routing API /quote/multi for each wallet token individually
to find which token(s) cause the '400 Could not extract router calldata' error.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Load .env from repo root
_env_path = Path(__file__).parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from web3 import Web3

# Constants
CHAIN_ID = 8453
HYDREX_ROUTING_API_URL = os.environ.get("HYDREX_ROUTING_API_URL", "https://router.api.hydrex.fi")
HYDREX_ROUTING_SLIPPAGE_BPS = int(os.environ.get("HYDREX_ROUTING_SLIPPAGE_BPS", "50"))
HYDREX_ROUTING_SOURCE = os.environ.get("HYDREX_ROUTING_SOURCE", "KYBERSWAP")
HYDREX_ROUTING_ORIGIN = os.environ.get("HYDREX_ROUTING_ORIGIN", "hydrex-optimiser")
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BASE_RPC_URL = os.environ.get("RPC_URL", "https://mainnet.base.org")

WALLET = "0xAB75E66C63307396FE8456Ea7c42CBBF3CF36298"

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "symbol",
     "outputs": [{"type": "string"}], "stateMutability": "view", "type": "function"},
]

# The 25 tokens that still failed (all except the 4 that were swapped by direct mode)
TOKENS = [
    "0x00000e7efa313F4E11Bfff432471eD9423AC6B30",  # HYDX
    "0x4200000000000000000000000000000000000006",  # WETH
    "0x9Cb41FD9dC6891BAe8187029461bfAADF6CC0C69",  # 0x9cb4
    "0x2E6C05f1f7D1f4Eb9A088bf12257f1647682b754",  # axlREGEN
    "0x0CEAC003B0d2479BebeC9f4b2EBAd0a803759bbf",  # WFRAX
    "0xE57E601c06689D3e2BF7DB7bebb14B4ff28400C6",  # 0xe57e
    "0xC0D3700000c0e32716863323bFd936b54a1633d1",  # 0xc0d3
    "0xFf8104251E7761163faC3211eF5583FB3F8583d6",  # 0xff81
    "0xA4A2E2ca3fBfE21aed83471D28b6f65A233C6e00",  # 0xa4a2
    "0x05B1266DDCeE093cE060DBF697e230EA9B453633",  # 0x05b1
    "0x15D0e0c55a3E7eE67152aD7E89acf164253Ff68d",  # 0x15d0
    "0xc0634090F2Fe6c6d75e61BE2b949464aBB498973",  # 0xc063
    "0xFAC77f01957ed1B3DD1cbEa992199B8f85B6E886",  # 0xfac7
    "0xd85c31854c2B0Fb40aaA9E2Fc4Da23C21f829d46",  # 0xd85c
    "0x323ac72a3a6267D97427944989b896fB411fdCbb",  # 0x323a
    "0x696F9436B67233384889472Cd7cD58A6fB5DF4f1",  # 0x696f
    "0x11dC28D01984079b7efE7763b533e6ed9E3722B9",  # 0x11dc
    "0xD262A4c7108C8139b2B189758e8D17c3DFC91a38",  # 0xd262
    "0x1111111111166b7FE7bd91427724B487980aFc69",  # 0x1111
    "0xD080eD3c74a20250a2c9821885203034ACD2D5ae",  # 0xd080
    "0xF732A566121Fa6362E9E0FBdd6D66E5c8C925E49",  # 0xf732
    "0x6f89bcA4eA5931EdFCB09786267b251DeE752b07",  # 0x6f89
    "0x7F6F8bB1AA8206921e80Ab6aBf1ac5737E39Ab07",  # 0x7f6f
    "0xB69bBB15095C0949489FBB43951d2b750Fa7fA89",  # 0xb69b
    "0x27f6c8289550fCE67f6B50BeD1F519966aFE5287",  # tGBP
]


def call_routing_api(swaps, taker):
    payload = {
        "taker": taker,
        "chainId": str(CHAIN_ID),
        "slippage": str(HYDREX_ROUTING_SLIPPAGE_BPS),
        "origin": HYDREX_ROUTING_ORIGIN,
        "swaps": swaps,
    }
    if HYDREX_ROUTING_SOURCE:
        payload["source"] = HYDREX_ROUTING_SOURCE

    body = json.dumps(payload).encode("utf-8")
    url = f"{HYDREX_ROUTING_API_URL}/quote/multi"
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0",
            "Origin": "https://router.api.hydrex.fi",
            "Referer": "https://router.api.hydrex.fi/",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return True, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return False, e.read().decode("utf-8", errors="replace")
    except Exception as e:
        return False, str(e)


def main():
    w3 = Web3(Web3.HTTPProvider(BASE_RPC_URL))
    usdc = Web3.to_checksum_address(USDC_ADDRESS)

    print(f"Checking {len(TOKENS)} tokens for routing API compatibility...\n")

    routable = []
    unroutable = []

    for addr in TOKENS:
        cs = Web3.to_checksum_address(addr)
        try:
            contract = w3.eth.contract(address=cs, abi=ERC20_ABI)
            bal = contract.functions.balanceOf(WALLET).call()
            sym = contract.functions.symbol().call()
        except Exception as e:
            print(f"  {addr[:10]}... - ERR reading token: {e}")
            continue

        if bal == 0:
            print(f"  {sym:12s} ({addr[:10]}...) - ZERO balance, skipping")
            continue

        swap = [{"fromTokenAddress": cs, "toTokenAddress": usdc, "amount": str(bal)}]
        ok, resp = call_routing_api(swap, WALLET)
        if ok:
            print(f"  {sym:12s} ({addr[:10]}...) - OK  bal={bal}")
            routable.append((sym, addr, bal))
        else:
            print(f"  {sym:12s} ({addr[:10]}...) - FAIL  bal={bal}  err={str(resp)[:120]}")
            unroutable.append((sym, addr, bal))

    print(f"\n{'='*60}")
    print(f"Routable: {len(routable)}")
    print(f"Unroutable: {len(unroutable)}")
    if unroutable:
        print("\nTokens that block the batch:")
        for sym, addr, bal in unroutable:
            print(f"  {sym} ({addr}) bal={bal}")


if __name__ == "__main__":
    main()
