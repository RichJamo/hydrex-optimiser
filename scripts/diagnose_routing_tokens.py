"""
Routing diagnostics for latest snapshot tokens.

Performs two passes:
1) Batch path through PriceFeed to capture no-quote denylist behavior.
2) Single-token routing probes to classify failures with status/details.

Usage:
    PYTHONPATH=. venv/bin/python scripts/diagnose_routing_tokens.py
"""

import json
import logging
import sqlite3
import sys
import time
from collections import defaultdict
from typing import Dict, List, Tuple

import requests

from src.database import Database
from src.price_feed import PriceFeed
from src.token_utils import get_token_symbol

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)

logger = logging.getLogger(__name__)

DB_PATH = "data/db/data.db"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _short(addr: str) -> str:
    return f"{addr[:6]}...{addr[-4:]}"


def fetch_snapshot_tokens() -> Tuple[int, List[str]]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    snapshot_ts = cur.execute("SELECT MAX(snapshot_ts) FROM live_reward_token_samples").fetchone()[0]
    tokens = [
        r[0]
        for r in cur.execute(
            "SELECT DISTINCT LOWER(reward_token) FROM live_reward_token_samples"
            " WHERE snapshot_ts=? AND reward_token IS NOT NULL AND TRIM(reward_token)!=''",
            (snapshot_ts,),
        ).fetchall()
    ]
    conn.close()
    return int(snapshot_ts), tokens


def classify_single_token(pf: PriceFeed, token: str) -> Tuple[str, str]:
    token = token.lower()
    if token == USDC:
        return "ok", "usdc_base"

    url = f"{pf.routing_api_url}/quote/multi"
    origin = pf.routing_origin.rstrip("/")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Origin": origin,
        "Referer": f"{origin}/",
    }

    try:
        decimals = pf._get_token_decimals(token)
        payload: Dict[str, object] = {
            "taker": pf.routing_taker,
            "chainId": str(pf.CHAIN_ID_BASE),
            "slippage": str(pf.routing_slippage_bps),
            "origin": pf.routing_origin,
            "swaps": [
                {
                    "fromTokenAddress": token,
                    "toTokenAddress": USDC,
                    "amount": str(10**decimals),
                }
            ],
        }
        if pf.routing_source:
            payload["source"] = pf.routing_source

        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code >= 400:
            body = (resp.text or "")[:220].replace("\n", " ")
            if resp.status_code == 400 and "No valid quotes" in (resp.text or ""):
                return "no_quote_400", body
            return f"http_{resp.status_code}", body

        data = resp.json()
        swaps = data.get("swaps") or []
        if not swaps:
            return "ok_empty_swaps", "200_empty_swaps"

        leg = swaps[0]
        amount_in = int(str(leg.get("amountIn", "0")))
        amount_out = int(str(leg.get("amountOut", "0")))
        if amount_in <= 0 or amount_out <= 0:
            return "ok_zero_amount", f"in={amount_in}, out={amount_out}"

        return "ok", "priced"
    except Exception as err:
        return "exception", str(err)[:220]


def resolve_symbol(token: str, db: Database) -> str:
    try:
        symbol = get_token_symbol(token, database=db)
        if symbol:
            return str(symbol)
    except Exception:
        pass
    return _short(token)


def main() -> None:
    snapshot_ts, tokens = fetch_snapshot_tokens()
    logger.info("Snapshot ts=%s | token_count=%s", snapshot_ts, len(tokens))

    db = Database(DB_PATH)
    pf = PriceFeed(allow_coingecko_fallback=False, database=db)

    # Pass 1: batch behavior through standard pipeline
    start = time.time()
    priced = pf.fetch_batch_prices_by_address(tokens)
    elapsed = time.time() - start

    # Pass 2: single-token routing classification
    classified = []
    for token in tokens:
        status, detail = classify_single_token(pf, token)
        classified.append(
            {
                "token": token.lower(),
                "symbol": resolve_symbol(token.lower(), db),
                "status": status,
                "detail": detail,
            }
        )

    by_status = defaultdict(list)
    for row in classified:
        by_status[row["status"]].append(row)

    # Batch-level misses (what auto-voter effectively sees)
    unpriced_batch = sorted([t for t in tokens if t.lower() not in {k.lower() for k in priced}])

    no_quote = sorted([row["token"] for row in classified if row["status"] == "no_quote_400"])
    soft_fail = sorted(
        [
            row["token"]
            for row in classified
            if row["status"] in {"ok_empty_swaps", "ok_zero_amount", "exception"}
        ]
    )

    print("\n" + "=" * 84)
    print(f"Snapshot:          {snapshot_ts}")
    print(f"Elapsed (batch):   {elapsed:.1f}s")
    print(f"Batch priced:      {len(priced)} / {len(tokens)}")
    print(f"Batch unpriced:    {len(unpriced_batch)}")
    print(f"No-quote (400):    {len(no_quote)}")
    print(f"Soft-fail count:   {len(soft_fail)}")

    print("\nStatus counts (single-token probe):")
    for status in sorted(by_status.keys()):
        print(f"  {status}: {len(by_status[status])}")

    problem_statuses = [s for s in sorted(by_status.keys()) if s not in {"ok"}]
    if problem_statuses:
        print("\nProblem tokens with symbols:")
        for status in problem_statuses:
            print(f"\n[{status}] ({len(by_status[status])})")
            for row in by_status[status]:
                print(f"  {row['token']}  symbol={row['symbol']}  detail={row['detail']}")

    print("\nSuggested env values:")
    print("HYDREX_ROUTING_DEFER_TOKENS=" + ",".join(no_quote))
    print("HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS=" + ",".join(soft_fail))

    print("\nJSON summary:")
    print(
        json.dumps(
            {
                "snapshot_ts": snapshot_ts,
                "batch_priced": len(priced),
                "batch_unpriced": unpriced_batch,
                "status_counts": {k: len(v) for k, v in sorted(by_status.items())},
                "no_quote_400": no_quote,
                "soft_fail": soft_fail,
            },
            indent=2,
        )
    )
    print("=" * 84)


if __name__ == "__main__":
    main()
