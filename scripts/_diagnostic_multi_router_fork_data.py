"""
_diagnostic_multi_router_fork_data.py

Fetches a live quote from the Hydrex routing API for a simple single-leg swap
and prints everything needed to reproduce it in a Solidity fork test:

  - The raw calldata (hex) for executeSwaps(SwapData[], deadline)
  - The decoded SwapData[] struct contents
  - The deadline
  - The current Base block number (for --fork-block-number)
  - A Solidity-ready struct literal

Usage:
    venv/bin/python scripts/_diagnostic_multi_router_fork_data.py
    venv/bin/python scripts/_diagnostic_multi_router_fork_data.py --from-token 0x4200... --amount-ether 0.01
    venv/bin/python scripts/_diagnostic_multi_router_fork_data.py --from-token 0xTOKEN --amount-raw 1000000000000000000
"""

import argparse
import json
import logging
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from typing import Optional

from web3 import Web3

# ---------------------------------------------------------------------------
# Inline config (avoids import path issues when run directly)
# ---------------------------------------------------------------------------
MULTI_ROUTER_ADDRESS = "0x599bFa1039C9e22603F15642B711D56BE62071f4"
USDC_ADDRESS = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ROUTING_API_URL = "https://router.api.hydrex.fi"
CHAIN_ID = 8453  # Base mainnet

# Well-known liquid tokens on Base — reliable routing test candidates
KNOWN_TOKENS = {
    "WETH":  {"address": "0x4200000000000000000000000000000000000006", "decimals": 18},
    "cbETH": {"address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22", "decimals": 18},
    "DAI":   {"address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb", "decimals": 18},
    "HYDX":  {"address": "0x55FE94D2CB2BaFb28B3a21Eb6020c45aed7cA0C3", "decimals": 18},
}

# Default: 0.01 WETH (reliably routable, good liquidity)
DEFAULT_TOKEN = "WETH"
DEFAULT_AMOUNT_ETH = "0.01"

MULTI_ROUTER_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"internalType": "address", "name": "router",          "type": "address"},
                    {"internalType": "address", "name": "inputAsset",      "type": "address"},
                    {"internalType": "address", "name": "outputAsset",     "type": "address"},
                    {"internalType": "uint256", "name": "inputAmount",     "type": "uint256"},
                    {"internalType": "uint256", "name": "minOutputAmount", "type": "uint256"},
                    {"internalType": "bytes",   "name": "callData",        "type": "bytes"},
                    {"internalType": "address", "name": "recipient",       "type": "address"},
                    {"internalType": "string",  "name": "origin",          "type": "string"},
                    {"internalType": "address", "name": "referral",        "type": "address"},
                    {"internalType": "uint256", "name": "referralFeeBps",  "type": "uint256"},
                ],
                "internalType": "struct HydrexMultiRouter.SwapData[]",
                "name": "swaps",
                "type": "tuple[]",
            },
            {"internalType": "uint256", "name": "deadline", "type": "uint256"},
        ],
        "name": "executeSwaps",
        "outputs": [],
        "stateMutability": "payable",
        "type": "function",
    }
]

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger(__name__)


def get_rpc_url() -> str:
    """Load RPC URL from .env / environment."""
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    import os
    url = os.getenv("RPC_URL", "")
    if not url:
        logger.warning("RPC_URL not set; block number will not be fetched")
    return url


def fetch_multi_quote(from_token: str, amount_raw: int, taker: str) -> dict:
    """POST /quote/multi for a single leg: from_token → USDC."""
    payload = {
        "taker": taker,
        "chainId": str(CHAIN_ID),
        "slippage": "50",
        "origin": "hydrex-optimiser-fork-test",
        "source": "KYBERSWAP",
        "swaps": [
            {
                "fromTokenAddress": from_token,
                "toTokenAddress": USDC_ADDRESS,
                "amount": str(amount_raw),
            }
        ],
    }
    body = json.dumps(payload).encode("utf-8")
    url = f"{ROUTING_API_URL}/quote/multi"
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Origin": "https://router.api.hydrex.fi",
            "Referer": "https://router.api.hydrex.fi/",
        },
        method="POST",
    )
    logger.info("POST %s  from=%s  amount=%d", url, from_token, amount_raw)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Routing API HTTP {exc.code}: {body_text[:500]}") from exc


def decode_execute_swaps(calldata_hex: str) -> tuple:
    """
    ABI-decode the executeSwaps(SwapData[], deadline) calldata.
    Returns (swaps_list, deadline).
    """
    w3 = Web3()
    contract = w3.eth.contract(abi=MULTI_ROUTER_ABI)
    # Strip the 4-byte selector
    selector = calldata_hex[:10]  # "0x" + 8 hex chars
    raw_params = bytes.fromhex(calldata_hex[10:])
    decoded = contract.decode_function_input(calldata_hex)
    fn, params = decoded
    swaps = params["swaps"]
    deadline = params["deadline"]
    return swaps, deadline, selector


def to_sol_bytes(b: bytes) -> str:
    """Format bytes as a Solidity hex literal."""
    return "hex\"" + b.hex() + "\""


def print_fork_data(quote: dict, from_token: str, amount_raw: int, block_number: Optional[int]) -> None:
    """Print all fork-test-relevant information."""
    tx = quote["transaction"]
    calldata_hex = tx["data"]
    router_returned = tx.get("to", "?")
    swaps_raw = quote.get("swaps", [])

    print("\n" + "=" * 80)
    print("HYDREX MULTI-ROUTER — FORK TEST DATA")
    print("=" * 80)
    print(f"  Multi-router:  {MULTI_ROUTER_ADDRESS}")
    print(f"  Router API to: {router_returned}")
    if router_returned.lower() != MULTI_ROUTER_ADDRESS.lower():
        print("  ⚠  WARNING: returned router != expected MULTI_ROUTER_ADDRESS")
    print(f"  Chain ID:      {CHAIN_ID}")
    if block_number:
        print(f"  Fork block:    {block_number}  ← use this as --fork-block-number")
    print(f"  Fetched at:    {datetime.now(timezone.utc).isoformat()}")

    # ── Routing API swap summaries ──────────────────────────────────────────
    print("\n── Routing API swap summaries ──")
    for i, s in enumerate(swaps_raw):
        print(
            f"  [{i}] {s.get('fromTokenAddress')} → {s.get('toTokenAddress')}"
            f"  amountIn={s.get('amountIn')}  amountOut={s.get('amountOut')}"
            f"  source={s.get('source')}"
        )
    total_usd = quote.get("totalAmountUsd")
    if total_usd:
        print(f"  totalAmountUsd: {total_usd}")

    # ── Raw calldata ────────────────────────────────────────────────────────
    print("\n── Raw calldata (pass directly to multi-router) ──")
    print(f"  {calldata_hex}")

    # ── Decoded SwapData[] ──────────────────────────────────────────────────
    try:
        swaps_decoded, deadline, selector = decode_execute_swaps(calldata_hex)
    except Exception as exc:
        print(f"\n  WARNING: ABI decode failed: {exc}")
        print("  (Use raw calldata above — it is still valid)")
        return

    print(f"\n── Decoded executeSwaps parameters ──")
    print(f"  Selector:  {selector}")
    print(f"  Deadline:  {deadline}  ({datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat()})")
    print(f"  Swaps:     {len(swaps_decoded)} leg(s)")

    for i, s in enumerate(swaps_decoded):
        print(f"\n  SwapData[{i}]:")
        print(f"    router:          {s['router']}")
        print(f"    inputAsset:      {s['inputAsset']}")
        print(f"    outputAsset:     {s['outputAsset']}")
        print(f"    inputAmount:     {s['inputAmount']}")
        print(f"    minOutputAmount: {s['minOutputAmount']}")
        print(f"    callData:        0x{s['callData'].hex()[:80]}{'…' if len(s['callData']) > 40 else ''}")
        print(f"    recipient:       {s['recipient']}")
        print(f"    origin:          {s['origin']}")
        print(f"    referral:        {s['referral']}")
        print(f"    referralFeeBps:  {s['referralFeeBps']}")

    # ── Solidity struct literal ─────────────────────────────────────────────
    print("\n── Solidity struct literal (copy into your fork test) ──")
    print(f"  // Fork block: {block_number or 'see above'}")
    print(f"  // Deadline warp: vm.warp({deadline});")
    print()
    for i, s in enumerate(swaps_decoded):
        cd_hex = "hex\"" + s['callData'].hex() + "\""
        print(f"  IHydrexMultiRouter.SwapData memory swap{i} = IHydrexMultiRouter.SwapData({{")
        print(f"      router:          {s['router']},")
        print(f"      inputAsset:      {s['inputAsset']},")
        print(f"      outputAsset:     {s['outputAsset']},")
        print(f"      inputAmount:     {s['inputAmount']},")
        print(f"      minOutputAmount: {s['minOutputAmount']},")
        print(f"      callData:        {cd_hex},")
        print(f"      recipient:       {s['recipient']},")
        print(f"      origin:          \"{s['origin']}\",")
        print(f"      referral:        {s['referral']},")
        print(f"      referralFeeBps:  {s['referralFeeBps']}")
        print(f"  }});")

    if len(swaps_decoded) > 1:
        arr_items = ", ".join(f"swap{i}" for i in range(len(swaps_decoded)))
        print(f"\n  IHydrexMultiRouter.SwapData[] memory swaps = new IHydrexMultiRouter.SwapData[]({len(swaps_decoded)});")
        for i in range(len(swaps_decoded)):
            print(f"  swaps[{i}] = swap{i};")
    else:
        print(f"\n  IHydrexMultiRouter.SwapData[] memory swaps = new IHydrexMultiRouter.SwapData[](1);")
        print(f"  swaps[0] = swap0;")

    print(f"\n  IHydrexMultiRouter(MULTI_ROUTER).executeSwaps(swaps, {deadline});")

    # ── Interface snippet ───────────────────────────────────────────────────
    print("\n── Minimal Solidity interface (paste into your test file) ──")
    print("""
  interface IHydrexMultiRouter {
      struct SwapData {
          address router;
          address inputAsset;
          address outputAsset;
          uint256 inputAmount;
          uint256 minOutputAmount;
          bytes   callData;
          address recipient;
          string  origin;
          address referral;
          uint256 referralFeeBps;
      }
      function executeSwaps(SwapData[] calldata swaps, uint256 deadline) external payable;
  }""")

    print("\n" + "=" * 80)
    print("NOTE: deadline is embedded in callData for some DEX aggregators.")
    print("      In your fork test call vm.warp(deadline) before executeSwaps,")
    print("      or re-fetch a fresh quote just before running the test.")
    print("=" * 80 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch a Hydrex multi-router quote and emit fork-test-ready SwapData."
    )
    parser.add_argument(
        "--from-token",
        default=None,
        help=f"Input token address. Defaults to WETH ({KNOWN_TOKENS['WETH']['address']}). "
             f"Known symbols: {', '.join(KNOWN_TOKENS)}",
    )
    parser.add_argument(
        "--amount-ether",
        default=None,
        help="Human-readable amount (e.g. 0.01 for 0.01 WETH). "
             "Assumes 18 decimals unless --decimals is set.",
    )
    parser.add_argument(
        "--amount-raw",
        type=int,
        default=None,
        help="Raw integer amount (wei). Overrides --amount-ether.",
    )
    parser.add_argument(
        "--decimals",
        type=int,
        default=18,
        help="Token decimals (default 18).",
    )
    parser.add_argument(
        "--taker",
        default="0x000000000000000000000000000000000000dEaD",
        help="Taker address for the routing API quote (any valid address; "
             "the actual sender in fork test is different). Default: dead address.",
    )
    args = parser.parse_args()

    # Resolve token address
    from_token = args.from_token
    decimals = args.decimals
    if from_token is None:
        token_info = KNOWN_TOKENS[DEFAULT_TOKEN]
        from_token = token_info["address"]
        decimals = token_info["decimals"]
        logger.info("Using default token: WETH (%s)", from_token)
    elif from_token.upper() in KNOWN_TOKENS:
        token_info = KNOWN_TOKENS[from_token.upper()]
        from_token = token_info["address"]
        decimals = token_info["decimals"]
    # else treat as raw address

    # Resolve amount
    if args.amount_raw is not None:
        amount_raw = args.amount_raw
    elif args.amount_ether is not None:
        amount_raw = int(float(args.amount_ether) * (10 ** decimals))
    else:
        amount_raw = int(float(DEFAULT_AMOUNT_ETH) * (10 ** decimals))
        logger.info("Using default amount: %s ETH = %d raw", DEFAULT_AMOUNT_ETH, amount_raw)

    if amount_raw <= 0:
        logger.error("Amount must be > 0")
        sys.exit(1)

    # Optionally fetch current block number
    block_number = None
    rpc_url = get_rpc_url()
    if rpc_url:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc_url))
            block_number = w3.eth.block_number
            logger.info("Current Base block: %d", block_number)
        except Exception as exc:
            logger.warning("Could not fetch block number: %s", exc)

    # Fetch quote
    try:
        quote = fetch_multi_quote(from_token, amount_raw, args.taker)
    except RuntimeError as exc:
        logger.error("Routing API call failed: %s", exc)
        sys.exit(1)

    print_fork_data(quote, from_token, amount_raw, block_number)


if __name__ == "__main__":
    main()
