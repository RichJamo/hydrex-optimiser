#!/usr/bin/env python3
"""Refresh cached routing prices in `token_prices`, outside the vote window.

Why this exists
---------------
Phase 1 of the boundary monitor used to price every snapshot token inline at T-240s. On
2026-08-20 that was 88 tokens taking ~70s of a ~200s budget, and the work is only that slow
because the routing API is: the same refresh has been observed anywhere between 70s and
283s. Spending a third of the vote window, with that variance, on data that barely moves in
an hour is a poor trade.

auto_voter already knows how to skip fresh prices -- `--price-max-age-hours` leaves any
token refreshed within the window untouched. What was missing was anything to refresh them
*beforehand*: the existing prefetch writes CoinGecko references into
`historical_token_prices`, not the `token_prices` rows the freshness check reads. So every
vote found all 88 tokens stale and repriced the lot.

This job fills that gap. Run on a cadence ahead of the boundary, it leaves phase 1 with
almost nothing to do; anything genuinely new or stale still gets repriced inline, which is
a handful of tokens rather than all of them.

It also covers tokens with no live bribe, which the vote path never touches. That matters:
a dormant token is not refreshed by voting at all, so it sits on whatever it last had until
somebody deposits a bribe in it -- which is how LAOD reached a vote carrying a 49-day-old
price and cost ~$8 in misallocated votes.

Usage
-----
    venv/bin/python scripts/refresh_token_prices.py
    venv/bin/python scripts/refresh_token_prices.py --max-age-hours 1
    venv/bin/python scripts/refresh_token_prices.py --active-only
"""

import argparse
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import List

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from rich.console import Console  # noqa: E402

from src.database import Database  # noqa: E402
from src.price_feed import PriceFeed  # noqa: E402

console = Console()
logger = logging.getLogger(__name__)


def load_tokens(db_path: str, active_only: bool) -> List[str]:
    """Reward tokens to price: every registered one, or only those with a live bribe."""
    conn = sqlite3.connect(db_path)
    try:
        if active_only:
            rows = conn.execute(
                """
                SELECT DISTINCT LOWER(reward_token)
                FROM live_reward_token_samples
                WHERE snapshot_ts = (SELECT MAX(snapshot_ts) FROM live_reward_token_samples)
                  AND rewards_raw > 0
                  AND reward_token IS NOT NULL
                """
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT DISTINCT LOWER(reward_token) FROM bribe_reward_tokens "
                "WHERE is_reward_token = 1 AND reward_token IS NOT NULL"
            ).fetchall()
    finally:
        conn.close()
    return sorted({r[0] for r in rows if r and r[0]})


def stale_tokens(db_path: str, tokens: List[str], max_age_hours: float) -> List[str]:
    """Drop tokens already refreshed within max_age_hours. 0 means refresh everything."""
    if max_age_hours <= 0:
        return tokens
    cutoff = int(time.time() - max_age_hours * 3600)
    conn = sqlite3.connect(db_path)
    try:
        fresh = {
            str(a).lower()
            for (a,) in conn.execute(
                "SELECT token_address FROM token_prices WHERE COALESCE(updated_at, 0) >= ?",
                (cutoff,),
            ).fetchall()
        }
    finally:
        conn.close()
    return [t for t in tokens if t not in fresh]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", default="data/db/data.db", help="Database path")
    parser.add_argument(
        "--max-age-hours", type=float, default=0.0,
        help="Skip tokens refreshed within this many hours (default 0 = refresh all)",
    )
    parser.add_argument(
        "--active-only", action="store_true",
        help="Only tokens with a live non-zero bribe, rather than every registered reward token",
    )
    parser.add_argument("--batch-size", type=int, default=50, help="Tokens per price-feed batch")
    parser.add_argument("--loglevel", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.basicConfig(level=getattr(logging, args.loglevel), format="%(levelname)s %(message)s")

    tokens = load_tokens(args.db_path, args.active_only)
    if not tokens:
        console.print("[yellow]No reward tokens found to refresh[/yellow]")
        return 0

    targets = stale_tokens(args.db_path, tokens, args.max_age_hours)
    if not targets:
        console.print(
            f"[green]✓ all {len(tokens)} token(s) refreshed within "
            f"{args.max_age_hours:.2f}h — nothing to do[/green]"
        )
        return 0

    console.print(
        f"[cyan]Refreshing {len(targets)}/{len(tokens)} reward token price(s)[/cyan]"
    )

    database = Database(args.db_path)
    price_feed = PriceFeed(api_key=os.getenv("COINGECKO_API_KEY"), database=database)

    started = time.time()
    updated = failed = 0
    for i in range(0, len(targets), args.batch_size):
        batch = targets[i : i + args.batch_size]
        try:
            prices = price_feed.fetch_batch_prices_by_address(batch)
        except Exception as e:
            logger.warning("batch price fetch failed (%s)", e)
            prices = {}
        for token in batch:
            price = prices.get(token)
            if price is None:
                failed += 1
                continue
            try:
                # A $0 price is a real verdict from the liquidity floor, not a failure:
                # the token exists but cannot be sold, and recording that is the point.
                database.save_token_price(token, float(price))
                updated += 1
            except Exception:
                failed += 1

    elapsed = time.time() - started
    console.print(
        f"[green]✓ refreshed {updated}/{len(targets)} in {elapsed:.0f}s "
        f"({failed} unpriced, left on their previous value)[/green]"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
