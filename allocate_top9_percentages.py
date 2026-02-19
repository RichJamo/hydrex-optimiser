#!/usr/bin/env python3
"""
Compute vote percentages for top 9 pools by bribes.
Allocation is proportional to expected return using full voting power.
"""

import json
import sqlite3
from web3 import Web3

DATABASE_PATH = "data.db"
RPC_URL = "https://base-mainnet.g.alchemy.com/v2/oFfvEpXYjGo8Nj4QQIkU3kXd6Z0JvfJZ"
VOTER_ADDRESS = "0xc69E3eF39E3fFBcE2A1c570f8d3ADF76909ef17b"
TOP_N = 9
VOTING_POWER = 1_530_896

w3 = Web3(Web3.HTTPProvider(RPC_URL))

with open("voterv5_abi.json", "r") as f:
    voter_abi = json.load(f)

with open("src/token_symbols.json", "r") as f:
    token_symbols = json.load(f)

voter = w3.eth.contract(
    address=Web3.to_checksum_address(VOTER_ADDRESS),
    abi=voter_abi
)

POOL_ABI = [
    {"constant": True, "inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "type": "function"},
]

ERC20_SYMBOL_STRING_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "string"}], "type": "function"}
]

ERC20_SYMBOL_BYTES32_ABI = [
    {"constant": True, "inputs": [], "name": "symbol", "outputs": [{"name": "", "type": "bytes32"}], "type": "function"}
]


def get_symbol(token_addr: str) -> str:
    addr = token_addr.lower()
    if addr in token_symbols:
        return token_symbols[addr]

    # Try string symbol first
    try:
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_SYMBOL_STRING_ABI
        )
        symbol = token_contract.functions.symbol().call()
        if isinstance(symbol, bytes):
            symbol = symbol.decode("utf-8").rstrip("\x00")
        return symbol
    except Exception:
        pass

    # Fallback to bytes32 symbol
    try:
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_addr),
            abi=ERC20_SYMBOL_BYTES32_ABI
        )
        symbol = token_contract.functions.symbol().call()
        if isinstance(symbol, bytes):
            symbol = symbol.decode("utf-8").rstrip("\x00")
        return symbol
    except Exception:
        return "UNKNOWN"


def get_pair_name(pool_addr: str) -> str:
    pool_contract = w3.eth.contract(address=Web3.to_checksum_address(pool_addr), abi=POOL_ABI)
    try:
        token0 = pool_contract.functions.token0().call()
        token1 = pool_contract.functions.token1().call()
        sym0 = get_symbol(token0)
        sym1 = get_symbol(token1)
        return f"{sym0}/{sym1}"
    except Exception:
        return "UNKNOWN/UNKNOWN"


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT MAX(epoch) FROM bribes")
    current_epoch = cursor.fetchone()[0]

    query = """
        SELECT 
            g.address as gauge_address,
            g.pool as pool_address,
            g.current_votes,
            COALESCE(SUM(b.usd_value), 0) as total_usd
        FROM gauges g
        LEFT JOIN bribes b ON b.gauge_address = g.address AND b.epoch = ?
        WHERE g.is_alive = 1 OR g.is_alive IS NULL
        GROUP BY g.address, g.pool, g.current_votes
        HAVING total_usd > 0
        ORDER BY total_usd DESC
        LIMIT ?
    """

    cursor.execute(query, (current_epoch, TOP_N))
    rows = cursor.fetchall()

    pools = []
    for gauge, pool, votes_str, total_usd in rows:
        try:
            current_votes = int(votes_str) if votes_str else 0
        except Exception:
            current_votes = 0

        # Expected return if we put all voting power into this pool
        new_total = current_votes + VOTING_POWER
        our_share = VOTING_POWER / new_total if new_total > 0 else 0
        expected_return = total_usd * our_share

        pools.append({
            "pool": pool,
            "pair": get_pair_name(pool),
            "current_votes": current_votes,
            "total_usd": total_usd,
            "expected_return": expected_return,
        })

    total_expected = sum(p["expected_return"] for p in pools)

    print(f"Top {TOP_N} pools for epoch {current_epoch}")
    print(f"Allocation: proportional to expected return (voting power {VOTING_POWER:,})")
    print("-")

    for i, p in enumerate(pools, 1):
        weight = p["expected_return"] / total_expected if total_expected > 0 else 0
        pct = weight * 100
        votes = int(VOTING_POWER * weight)
        print(
            f"{i:2d}. {p['pair']:16s}  {pct:6.2f}%  votes={votes:,}  "
            f"bribes=${p['total_usd']:,.2f}  pool={p['pool']}"
        )

    conn.close()


if __name__ == "__main__":
    main()
