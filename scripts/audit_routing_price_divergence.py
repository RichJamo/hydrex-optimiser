#!/usr/bin/env python3
"""Audit routing-quote prices against CoinGecko references.

Thin-liquidity tokens can quote persistently high on the Hydrex router. A stable
overprice is the dangerous case: it sits under PRICE_SANITY_MAX_SPIKE_RATIO and,
because the guard's last-resort anchor is the token's own previous routing price,
it is internally consistent and never trips at any threshold. BETR ran ~2.5x for
three epochs this way; REGENT was worse at ~3.1x.

For every auto_voter_snap reading this compares against the nearest cg_ref within
--max-pair-age-hours and reports the per-token divergence distribution. Tokens
above the thresholds belong on HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS, which
bypasses the router for them entirely.

Read-only. Run after each epoch's post-mortem.

Usage:
  venv/bin/python scripts/audit_routing_price_divergence.py
  venv/bin/python scripts/audit_routing_price_divergence.py --since-days 30 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402
from rich.console import Console  # noqa: E402
from rich.table import Table  # noqa: E402

from config.settings import DATABASE_PATH  # noqa: E402

load_dotenv()
console = Console()


def load_fallback_list() -> set:
    """Addresses already routed via CoinGecko, read from the live env."""
    raw = os.getenv("HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS", "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def load_defer_list() -> set:
    raw = os.getenv("HYDREX_ROUTING_DEFER_TOKENS", "")
    return {t.strip().lower() for t in raw.split(",") if t.strip()}


def collect_ratios(
    conn: sqlite3.Connection, since_ts: int, max_pair_age_seconds: int
) -> Dict[str, List[float]]:
    """routing/cg ratio per token, pairing each snap reading to its nearest cg_ref."""
    snaps = conn.execute(
        "SELECT lower(token_address), timestamp, usd_price FROM historical_token_prices "
        "WHERE granularity='auto_voter_snap' AND usd_price > 0 AND timestamp >= ?",
        (since_ts,),
    ).fetchall()

    cg: Dict[str, List[Tuple[int, float]]] = {}
    for addr, ts, px in conn.execute(
        "SELECT lower(token_address), timestamp, usd_price FROM historical_token_prices "
        "WHERE granularity='cg_ref' AND usd_price > 0 AND timestamp >= ?",
        (since_ts - max_pair_age_seconds,),
    ):
        cg.setdefault(str(addr).lower(), []).append((int(ts), float(px)))
    for addr in cg:
        cg[addr].sort()

    ratios: Dict[str, List[float]] = {}
    for addr, ts, px in snaps:
        addr = str(addr).lower()
        candidates = cg.get(addr)
        if not candidates:
            continue
        near_ts, near_px = min(candidates, key=lambda x: abs(x[0] - int(ts)))
        if abs(near_ts - int(ts)) > max_pair_age_seconds or near_px <= 0:
            continue
        ratios.setdefault(addr, []).append(float(px) / near_px)
    return ratios


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db-path", default=DATABASE_PATH)
    p.add_argument("--since-days", type=float, default=0.0, help="Only readings newer than this (0 = all)")
    p.add_argument("--max-pair-age-hours", type=float, default=6.0,
                   help="Max gap when pairing a snap reading to a cg_ref")
    p.add_argument("--min-readings", type=int, default=3, help="Ignore tokens with fewer paired readings")
    p.add_argument("--median-threshold", type=float, default=1.25,
                   help="Flag when the median routing/cg ratio is at or above this")
    p.add_argument("--spike-threshold", type=float, default=1.5,
                   help="Ratio counted as a divergent reading")
    p.add_argument("--min-spikes", type=int, default=2,
                   help="Flag when at least this many readings exceed --spike-threshold")
    p.add_argument("--json", dest="json_out", default=None, help="Write findings to this JSON path")
    args = p.parse_args()

    conn = sqlite3.connect(args.db_path)
    since_ts = 0
    if args.since_days > 0:
        newest = conn.execute(
            "SELECT MAX(timestamp) FROM historical_token_prices WHERE granularity='auto_voter_snap'"
        ).fetchone()
        if newest and newest[0]:
            since_ts = int(newest[0]) - int(args.since_days * 86400)

    ratios = collect_ratios(conn, since_ts, int(args.max_pair_age_hours * 3600))
    symbols = {
        str(a).lower(): s
        for a, s in conn.execute("SELECT lower(token_address), symbol FROM token_metadata")
    }
    on_fallback = load_fallback_list()
    on_defer = load_defer_list()

    findings = []
    for addr, rs in ratios.items():
        if len(rs) < args.min_readings:
            continue
        median = statistics.median(rs)
        spikes = sum(1 for r in rs if r >= args.spike_threshold)
        if median >= args.median_threshold or spikes >= args.min_spikes:
            findings.append({
                "address": addr,
                "symbol": symbols.get(addr, addr[:10]),
                "readings": len(rs),
                "median_ratio": round(median, 3),
                "max_ratio": round(max(rs), 3),
                "spike_readings": spikes,
                "on_fallback_list": addr in on_fallback,
                "on_defer_list": addr in on_defer,
                "persistent": median >= args.median_threshold,
            })
    findings.sort(key=lambda f: -f["median_ratio"])

    table = Table(title="Routing vs CoinGecko divergence")
    for col in ("Token", "n", "median", "max", f">={args.spike_threshold}x", "Routed via CG", "Verdict"):
        table.add_column(col, justify="right" if col not in ("Token", "Verdict", "Routed via CG") else "left")
    for f in findings:
        if f["on_fallback_list"] or f["on_defer_list"]:
            verdict, style = ("already handled", "dim")
        elif f["persistent"]:
            verdict, style = ("ADD to CG fallback", "bold red")
        else:
            verdict, style = ("episodic — watch", "yellow")
        table.add_row(
            f["symbol"], str(f["readings"]), f"{f['median_ratio']:.2f}", f"{f['max_ratio']:.2f}",
            str(f["spike_readings"]),
            "yes" if f["on_fallback_list"] else ("deferred" if f["on_defer_list"] else "no"),
            verdict, style=style,
        )
    console.print(table)

    todo = [f for f in findings if f["persistent"] and not (f["on_fallback_list"] or f["on_defer_list"])]
    if todo:
        console.print(
            f"\n[bold red]{len(todo)} token(s) persistently overpriced and not yet routed via "
            f"CoinGecko.[/bold red] Append to HYDREX_ROUTING_COINGECKO_FALLBACK_TOKENS in .env:"
        )
        console.print(",".join(f["address"] for f in todo))
    else:
        console.print("\n[green]✓ No unhandled persistent routing overprices.[/green]")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(findings, indent=2))
        console.print(f"Wrote {args.json_out}")

    conn.close()
    return 1 if todo else 0


if __name__ == "__main__":
    raise SystemExit(main())
