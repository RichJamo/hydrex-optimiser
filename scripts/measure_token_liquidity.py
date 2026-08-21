#!/usr/bin/env python3
"""Measure how much USDC each reward token's Base pools will actually pay out.

Why this exists
---------------
A per-token price says nothing about whether the position behind it can be sold. SPLASH
quotes $5.91e-06 for a 1,000-token trade while its entire Base pool pays out $0.28, so a
million-token bribe reads as $5.91 and realises $0.047. The growth ladder in the price feed
removes quantisation noise but cannot see this: a small probe in a tiny pool is a perfectly
well-formed quote.

This job asks the router a blunt question -- "if I tried to sell $N of this, what would I
actually get?" -- and records the answer in `token_liquidity`. The price feed then reads
that cache and values unsellable tokens at $0 without issuing a single network request
during the vote window.

Run it weekly, well away from the epoch boundary. Pool depth moves slowly, so a reading
taken unhurried is both cheaper and more trustworthy than one taken at T-240s: an earlier
inline version cost 44s of a ~200s phase-1 budget and skipped whole batches whenever the
routing API wobbled -- which is precisely when the check mattered.

Usage
-----
    venv/bin/python scripts/measure_token_liquidity.py
    venv/bin/python scripts/measure_token_liquidity.py --probe-usd 500 --dry-run
    venv/bin/python scripts/measure_token_liquidity.py --only 0xc3679395...,0x55eec20a...
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

import requests  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from config.settings import (  # noqa: E402
    HYDREX_LIQUIDITY_FLOOR_FILL_RATIO,
    HYDREX_LIQUIDITY_FLOOR_USD,
    HYDREX_ROUTING_API_URL,
    HYDREX_ROUTING_ORIGIN,
    HYDREX_ROUTING_PRICE_CHUNK_SIZE,
    HYDREX_ROUTING_SLIPPAGE_BPS,
    HYDREX_ROUTING_SOURCE,
    MY_ESCROW_ADDRESS,
    USDC_ADDRESS,
)
from src.database import Database  # noqa: E402
from src.price_feed import PriceFeed  # noqa: E402

console = Console()
logger = logging.getLogger(__name__)

USDC_DECIMALS = 6


def load_reward_tokens(db_path: str, only: Optional[str]) -> List[str]:
    """Every token registered as a reward on a bribe contract, dormant ones included.

    Dormant tokens matter: a token with no bribe this epoch is not refreshed by the vote
    path at all, so it sits on whatever it last had. The moment somebody deposits a bribe
    in it, it enters the decision carrying that value -- which is how LAOD reached a vote
    with a 49-day-old price. Measuring the full registry means the floor is already in
    place before that happens.
    """
    if only:
        return [t.strip().lower() for t in only.split(",") if t.strip().startswith("0x")]
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT LOWER(reward_token) FROM bribe_reward_tokens "
            "WHERE is_reward_token = 1 AND reward_token IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0]]


def measure(
    tokens: List[str],
    price_feed: PriceFeed,
    probe_usd: float,
    chunk_size: int,
) -> Dict[str, float]:
    """Quote a `probe_usd` sale of each token and return {token: usdc_actually_offered}.

    Preconditions : tokens are lowercased Base addresses.
    Postconditions: returns only tokens the router answered for. A token that could not be
                    quoted is absent, never zero -- the caller must not read silence as
                    illiquidity, since the routing API returns errors under load as well as
                    for genuine dead ends. Zeroing on a transient error would make the
                    optimizer ignore a perfectly good bribe.
    """
    origin = HYDREX_ROUTING_ORIGIN.strip()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0",
        "Origin": origin,
        "Referer": f"{origin}/",
    }
    url = f"{HYDREX_ROUTING_API_URL.rstrip('/')}/quote/multi"

    prices = price_feed.fetch_batch_prices_by_address(tokens)
    out: Dict[str, float] = {}
    swaps = []
    for token in tokens:
        price = prices.get(token)
        if not price or price <= 0:
            continue  # unpriceable: the floor has nothing to compare against
        decimals = price_feed._get_token_decimals(token)
        raw = int((probe_usd / price) * 10**decimals)
        if raw <= 0:
            continue
        swaps.append({
            "fromTokenAddress": token,
            "toTokenAddress": USDC_ADDRESS.lower(),
            "amount": str(raw),
        })

    for start in range(0, len(swaps), chunk_size):
        chunk = swaps[start : start + chunk_size]
        payload = {
            "taker": (MY_ESCROW_ADDRESS or "").strip(),
            "chainId": "8453",
            "slippage": str(HYDREX_ROUTING_SLIPPAGE_BPS),
            "origin": origin,
            "swaps": chunk,
        }
        if HYDREX_ROUTING_SOURCE.strip():
            payload["source"] = HYDREX_ROUTING_SOURCE.strip()

        legs = []
        try:
            response = requests.post(url, json=payload, headers=headers, timeout=45)
            if 200 <= response.status_code < 300:
                legs = response.json().get("swaps") or []
        except Exception as e:
            logger.debug("liquidity batch failed (%s); retrying singly", e)

        if not legs and len(chunk) > 1:
            # Unlike the vote path there is no clock here, so one bad leg should not cost
            # the whole batch its measurement.
            for single in chunk:
                try:
                    r = requests.post(
                        url, json={**payload, "swaps": [single]}, headers=headers, timeout=45
                    )
                    if 200 <= r.status_code < 300:
                        legs.extend(r.json().get("swaps") or [])
                except Exception:
                    continue
                time.sleep(0.1)

        for leg in legs:
            try:
                address = str(leg.get("fromTokenAddress", "")).lower()
                amount_out = int(str(leg.get("amountOut", "0")) or 0)
            except Exception:
                continue
            if address and amount_out >= 0:
                out[address] = amount_out / 10**USDC_DECIMALS
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default="data/db/data.db", help="Database path")
    parser.add_argument(
        "--probe-usd", type=float, default=float(HYDREX_LIQUIDITY_FLOOR_USD),
        help="Sale size to quote, in USD (default: the configured liquidity floor)",
    )
    parser.add_argument("--only", help="Comma-separated token addresses to measure instead of all")
    parser.add_argument("--dry-run", action="store_true", help="Measure and report without writing")
    parser.add_argument("--loglevel", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel), format="%(levelname)s %(message)s")

    tokens = load_reward_tokens(args.db_path, args.only)
    if not tokens:
        console.print("[yellow]No reward tokens found to measure[/yellow]")
        return 0

    console.print(f"[cyan]Measuring pool capacity for {len(tokens)} reward token(s) "
                  f"at a ${args.probe_usd:,.0f} probe[/cyan]")

    database = Database(args.db_path)
    price_feed = PriceFeed(api_key=os.getenv("COINGECKO_API_KEY"), database=database)
    # The floor is what we are measuring; leaving it on would zero prices mid-measurement.
    price_feed.liquidity_floor_usd = 0.0

    started = time.time()
    measured = measure(tokens, price_feed, args.probe_usd, int(HYDREX_ROUTING_PRICE_CHUNK_SIZE))
    elapsed = time.time() - started

    conn = sqlite3.connect(args.db_path)
    symbols = {
        str(a).lower(): s
        for a, s in conn.execute("SELECT token_address, symbol FROM token_metadata").fetchall()
    }
    conn.close()

    required = args.probe_usd * float(HYDREX_LIQUIDITY_FLOOR_FILL_RATIO)
    below = {a: v for a, v in measured.items() if v < required}

    if not args.dry_run:
        for address, capacity in measured.items():
            database.save_token_liquidity(
                address, capacity, args.probe_usd, symbols.get(address)
            )

    table = Table(title=f"Pools that cannot pay ${required:,.0f} (of {len(measured)} measured)")
    table.add_column("Symbol"); table.add_column("Address"); table.add_column("Pool pays", justify="right")
    for address, capacity in sorted(below.items(), key=lambda kv: kv[1]):
        table.add_row(symbols.get(address, "?"), address[:12] + "…", f"${capacity:,.4f}")
    console.print(table)

    unmeasured = len(tokens) - len(measured)
    console.print(
        f"[green]✓ measured {len(measured)}/{len(tokens)} token(s) in {elapsed:.0f}s — "
        f"{len(below)} below the floor, {unmeasured} unquotable "
        f"(left unmeasured, so they stay unchecked rather than being zeroed)[/green]"
    )
    if args.dry_run:
        console.print("[yellow]dry run — nothing written to token_liquidity[/yellow]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
