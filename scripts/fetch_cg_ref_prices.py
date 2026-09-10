#!/usr/bin/env python3
"""
Fetch CoinGecko reference prices for active reward tokens and persist them.

Designed to be run repeatedly (every 30 min) in the hours before an epoch
boundary.  Each run upserts one row per token in historical_token_prices with
granularity='cg_ref' keyed to the current hour, so the sanity guard in
price_feed.py can use the median of multiple readings as a routing-API-
independent reference.

Usage:
  python scripts/fetch_cg_ref_prices.py
  python scripts/fetch_cg_ref_prices.py --epoch 1781136000   # specific epoch
  python scripts/fetch_cg_ref_prices.py --dry-run            # print only
"""

import argparse
import logging
import os
import sys
import time
from typing import Dict, List

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
load_dotenv()

from config.settings import DATABASE_PATH
from src.database import Database
from src.price_feed import PriceFeed

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

CG_REF_GRANULARITY = "cg_ref"
# Max tokens per CoinGecko /simple/token_price batch request
CG_BATCH_SIZE = 80
# Epoch seconds (Hydrex epochs are weekly)
EPOCH_SECONDS = 604800
# How many epochs back to collect reward tokens from; 0 means every epoch on record.
#
# A token needs a live cg_ref or the sanity guard in price_feed.py anchors it on the routing
# feed's own previous value. That anchor is self-referential: a bad quote puts every later
# correction outside the spike threshold, so the error locks in permanently and the token can
# never recover (RIZE sat at 4.6x its realisable price this way, AZUSD at 8x).
#
# The tokens most exposed are the *dormant* ones, which carry the stalest anchors -- RIZE and
# SPX last bribed in Feb-Apr 2026 -- so a rolling window is the wrong shape here. All-time is
# also free: 119 distinct tokens have ever been reward tokens versus 102 in an 8-epoch window,
# and at CG_BATCH_SIZE=80 both are two batches. Set a positive value only to deliberately
# narrow the sweep.
DEFAULT_LOOKBACK_EPOCHS = 0


def _hour_ts(ts: int) -> int:
    """Round a unix timestamp down to the start of its hour."""
    return ts - (ts % 3600)


def discover_reward_tokens(
    db: Database,
    epoch: int = 0,
    lookback_epochs: int = DEFAULT_LOOKBACK_EPOCHS,
) -> List[str]:
    """Return unique reward token addresses seen in a window of recent epochs.

    Preconditions:
        lookback_epochs >= 0.
    Postconditions:
        Returns lowercase, de-duplicated addresses, none from an epoch later than the anchor.
        The result for lookback_epochs=N is a superset of the result for any M < N over the
        same anchor; lookback_epochs=1 reproduces the historical single-epoch behaviour
        exactly, and lookback_epochs=0 returns every token at or before the anchor.

    The window ends at `epoch` (or the most recent epoch present) and reaches back
    lookback_epochs - 1 further epochs, or unbounded when lookback_epochs is 0. Tokens outside
    the anchor epoch still get a cg_ref, which is what stops the sanity guard in
    price_feed.py from anchoring such a token on the routing feed's own prior value.
    """
    if lookback_epochs < 0:
        raise ValueError("lookback_epochs must be >= 0")

    import sqlite3

    conn = sqlite3.connect(DATABASE_PATH)
    try:
        cur = conn.cursor()
        anchor = epoch
        if not anchor:
            row = cur.execute("SELECT MAX(epoch) FROM boundary_reward_snapshots").fetchone()
            anchor = row[0] if row and row[0] else 0
        if not anchor:
            return []
        if lookback_epochs == 0:
            rows = cur.execute(
                "SELECT DISTINCT lower(reward_token) FROM boundary_reward_snapshots "
                "WHERE epoch <= ? AND active_only=1",
                (anchor,),
            ).fetchall()
        else:
            cutoff = anchor - (lookback_epochs - 1) * EPOCH_SECONDS
            rows = cur.execute(
                "SELECT DISTINCT lower(reward_token) FROM boundary_reward_snapshots "
                "WHERE epoch BETWEEN ? AND ? AND active_only=1",
                (cutoff, anchor),
            ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def fetch_and_store(
    addresses: List[str],
    feed: PriceFeed,
    db: Database,
    hour_ts: int,
    dry_run: bool,
) -> Dict[str, float]:
    """Fetch CG prices for a batch of addresses and store them."""
    prices: Dict[str, float] = {}

    for i in range(0, len(addresses), CG_BATCH_SIZE):
        chunk = addresses[i : i + CG_BATCH_SIZE]
        joined = ",".join(chunk)
        data = feed._coingecko_get(
            "/simple/token_price/base",
            {"contract_addresses": joined, "vs_currencies": "usd"},
        )
        if not data:
            logger.warning("CoinGecko returned no data for batch %d", i // CG_BATCH_SIZE + 1)
            continue
        for addr in chunk:
            entry = data.get(addr) or data.get(addr.lower())
            if entry and "usd" in entry:
                prices[addr] = float(entry["usd"])

        # Respect CG rate limits between batches
        if i + CG_BATCH_SIZE < len(addresses):
            time.sleep(1.5)

    if not prices:
        logger.warning("No CoinGecko prices returned for any of %d tokens", len(addresses))
        return prices

    logger.info(
        "CoinGecko returned %d/%d prices (hour_ts=%d)",
        len(prices), len(addresses), hour_ts,
    )

    if dry_run:
        for addr, price in sorted(prices.items()):
            logger.info("  [dry-run] %s  $%.8f", addr[:12], price)
        return prices

    to_persist = [
        (addr, hour_ts, CG_REF_GRANULARITY, price)
        for addr, price in prices.items()
    ]
    db.save_historical_token_prices(to_persist)
    logger.info("Stored %d cg_ref prices at hour_ts=%d", len(to_persist), hour_ts)
    return prices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--epoch", type=int, default=0, help="Epoch to fetch tokens for (default: most recent)")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and log but do not write to DB")
    parser.add_argument(
        "--lookback-epochs",
        type=int,
        default=DEFAULT_LOOKBACK_EPOCHS,
        help=(
            "How many epochs back to collect reward tokens from, inclusive of the anchor "
            f"epoch (default: {DEFAULT_LOOKBACK_EPOCHS}, meaning every epoch on record). "
            "Pass 1 for the old current-epoch-only behaviour."
        ),
    )
    parser.add_argument("--loglevel", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args()

    logging.getLogger().setLevel(args.loglevel)

    db = Database(DATABASE_PATH)
    api_key = os.getenv("COINGECKO_API_KEY", "")
    feed = PriceFeed(api_key=api_key or None, database=db, allow_coingecko_fallback=True)

    if args.lookback_epochs < 0:
        logger.error("--lookback-epochs must be >= 0 (got %d)", args.lookback_epochs)
        sys.exit(2)

    addresses = discover_reward_tokens(db, epoch=args.epoch, lookback_epochs=args.lookback_epochs)
    if not addresses:
        logger.error("No reward token addresses found — is boundary_reward_snapshots populated?")
        sys.exit(1)
    logger.info(
        "Discovered %d reward tokens across %s epoch(s) ending at %s",
        len(addresses),
        args.lookback_epochs or "all",
        args.epoch or "latest",
    )

    now = int(time.time())
    hour_ts = _hour_ts(now)
    fetch_and_store(addresses, feed, db, hour_ts, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
