"""
Record actual post-epoch reward receipts into actual_epoch_rewards.

After each epoch flip, paste or write the received token amounts and prices
into a JSON file (see format below), then run this script to persist them
into the database so the postmortem and realisation views can do a 3-way
comparison against predicted and boundary-snapshot values.

JSON format (actual_rewards_epoch_<ts>.json)
--------------------------------------------
{
  "epoch": 1778112000,
  "notes": {
    "USDC_breakdown": "23.36 + 48.98 = 72.34 tokens / ..."
  },
  "actual_tokens": {
    "HYDX": 302.74,
    "WETH": 0.044782,
    ...
  },
  "token_prices": {
    "HYDX": 0.039374,
    "WETH": 2338.61,
    ...
  },
  "total_usd": 467.93      <-- optional; recomputed and validated if present
}

Usage
-----
  # Record a single epoch from file
  venv/bin/python scripts/record_actual_rewards.py --json actual_rewards_epoch_1778112000.json

  # Auto-find file by epoch timestamp
  venv/bin/python scripts/record_actual_rewards.py --epoch 1778112000

  # Backfill all actual_rewards_epoch_*.json files in the project root
  venv/bin/python scripts/record_actual_rewards.py --all

  # Preview without writing
  venv/bin/python scripts/record_actual_rewards.py --json actual_rewards_epoch_1778112000.json --dry-run

  # List what is already recorded
  venv/bin/python scripts/record_actual_rewards.py --list
"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))


def _fmt_epoch(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


from config.settings import DATABASE_PATH
from src.db import apply_schema, db_conn

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _symbol_to_address_map(conn) -> dict[str, str]:
    """Return lowercased-symbol → token_address.

    Primary source: token_metadata (populated by decimals/price fetcher).
    Fallback: bribes table (populated from subgraph; covers tokens that appeared
    as bribe rewards before the metadata fetcher encountered their address).
    token_metadata takes precedence where both sources have an entry.
    """
    result: dict[str, str] = {}
    # Fallback first so token_metadata wins on conflict
    bribe_rows = conn.execute(
        "SELECT DISTINCT token_symbol, reward_token FROM bribes"
        " WHERE token_symbol IS NOT NULL AND reward_token IS NOT NULL"
    ).fetchall()
    for sym, addr in bribe_rows:
        result[sym.lower()] = addr
    meta_rows = conn.execute(
        "SELECT symbol, token_address FROM token_metadata WHERE symbol IS NOT NULL"
    ).fetchall()
    for sym, addr in meta_rows:
        result[sym.lower()] = addr
    return result


def _compute_total_usd(actual_tokens: dict, token_prices: dict) -> float:
    total = 0.0
    for symbol, amount in actual_tokens.items():
        price = token_prices.get(symbol, 0.0)
        total += float(amount) * float(price)
    return round(total, 6)


def _validate_total(computed: float, provided: Optional[float]) -> None:
    if provided is None:
        return
    diff_pct = abs(computed - provided) / max(provided, 0.01) * 100
    if diff_pct > 1.0:
        logger.warning(
            "Provided total_usd=%.2f differs from recomputed=%.2f (%.1f%%). "
            "Using recomputed value.",
            provided, computed, diff_pct,
        )


def _find_json_for_epoch(epoch: int) -> Path:
    pattern = str(ROOT_DIR / f"actual_rewards_epoch_{epoch}.json")
    matches = glob.glob(pattern)
    if not matches:
        raise FileNotFoundError(
            f"No file found matching: actual_rewards_epoch_{epoch}.json\n"
            f"Create it in the repo root and re-run."
        )
    return Path(matches[0])


def _find_all_json_files() -> list[Path]:
    pattern = str(ROOT_DIR / "actual_rewards_epoch_*.json")
    return sorted(Path(p) for p in glob.glob(pattern))


# ---------------------------------------------------------------------------
# Core write logic
# ---------------------------------------------------------------------------

def record_from_dict(data: dict, *, dry_run: bool = False, db_path: Optional[str] = None) -> int:
    """
    Parse a single actual-rewards dict and upsert rows into actual_epoch_rewards.

    Returns number of rows written (0 in dry-run).
    """
    epoch = int(data["epoch"])
    actual_tokens: dict = data["actual_tokens"]
    token_prices: dict = data["token_prices"]
    notes_json: Optional[str] = json.dumps(data.get("notes")) if data.get("notes") else None
    provided_total: Optional[float] = data.get("total_usd")

    computed_total = _compute_total_usd(actual_tokens, token_prices)
    _validate_total(computed_total, provided_total)

    now = int(time.time())

    # Build rows
    with db_conn(db_path) as conn:
        sym_to_addr = _symbol_to_address_map(conn)

        rows: list[tuple] = []
        missing_addrs: list[str] = []
        for symbol, amount in actual_tokens.items():
            price = float(token_prices.get(symbol, 0.0))
            total_usd = round(float(amount) * price, 6)
            addr = sym_to_addr.get(symbol.lower())
            if addr is None:
                missing_addrs.append(symbol)
            rows.append((epoch, symbol, addr, float(amount), price, total_usd, notes_json, now))

        if missing_addrs:
            logger.warning(
                "No token_address found in token_metadata for: %s  "
                "(token_address will be NULL — add to token_metadata to resolve)",
                ", ".join(missing_addrs),
            )

        # Print preview table
        t = Table(title=f"Actual rewards — epoch {epoch} ({_fmt_epoch(epoch)})", show_lines=False)
        t.add_column("Symbol", style="cyan")
        t.add_column("Amount", justify="right")
        t.add_column("Price", justify="right")
        t.add_column("USD", justify="right")
        t.add_column("Address", style="dim")
        for r in rows:
            _, sym, addr, amt, price, usd, _, _ = r
            t.add_row(
                sym,
                f"{amt:,.4f}",
                f"${price:,.6f}",
                f"${usd:,.2f}",
                (addr[:10] + "…") if addr else "—",
            )
        t.add_section()
        t.add_row("[bold]TOTAL", "", "", f"[bold]${computed_total:,.2f}", "")
        console.print(t)

        if dry_run:
            console.print(f"[yellow]dry-run[/yellow] — {len(rows)} rows not written")
            return 0

        conn.executemany(
            """
            INSERT OR REPLACE INTO actual_epoch_rewards
                (epoch, symbol, token_address, amount_tokens, usd_price, total_usd, notes, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()

    console.print(f"[green]✓[/green] {len(rows)} rows written for epoch {epoch} ({_fmt_epoch(epoch)})  (total ${computed_total:,.2f})")
    return len(rows)


def cmd_list(db_path: Optional[str] = None) -> None:
    """Print a summary of all epochs already recorded."""
    with db_conn(db_path, row_factory=True) as conn:
        rows = conn.execute(
            """
            SELECT epoch, COUNT(*) AS tokens, SUM(total_usd) AS total_usd,
                   datetime(MAX(recorded_at), 'unixepoch') AS recorded_at
            FROM actual_epoch_rewards
            GROUP BY epoch
            ORDER BY epoch
            """
        ).fetchall()

    if not rows:
        console.print("[dim]No actual reward records found in database.[/dim]")
        return

    t = Table(title="Recorded actual rewards by epoch")
    t.add_column("Epoch (UTC)", style="cyan")
    t.add_column("Tokens", justify="right")
    t.add_column("Total USD", justify="right")
    t.add_column("Recorded at")
    for r in rows:
        t.add_row(f"{r['epoch']} ({_fmt_epoch(r['epoch'])})", str(r["tokens"]), f"${r['total_usd']:,.2f}", r["recorded_at"])
    console.print(t)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Record actual post-epoch reward receipts into the database.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--json",
        metavar="PATH",
        help="Path to actual_rewards_epoch_<ts>.json file",
    )
    source.add_argument(
        "--epoch",
        type=int,
        metavar="TS",
        help="Epoch timestamp; auto-locates actual_rewards_epoch_<ts>.json in repo root",
    )
    source.add_argument(
        "--all",
        action="store_true",
        help="Backfill all actual_rewards_epoch_*.json files found in repo root",
    )
    source.add_argument(
        "--list",
        action="store_true",
        help="List epochs already recorded in the database and exit",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview rows without writing to the database",
    )
    parser.add_argument(
        "--db-path",
        default=DATABASE_PATH,
        help=f"Path to SQLite database (default: {DATABASE_PATH})",
    )

    args = parser.parse_args()

    # Ensure schema is current (creates actual_epoch_rewards if missing)
    apply_schema(args.db_path)

    if args.list:
        cmd_list(args.db_path)
        return

    # Collect files to process
    if args.json:
        files = [Path(args.json)]
    elif args.epoch:
        files = [_find_json_for_epoch(args.epoch)]
    elif args.all:
        files = _find_all_json_files()
        if not files:
            console.print("[yellow]No actual_rewards_epoch_*.json files found in repo root.[/yellow]")
            return
    else:
        parser.print_help()
        sys.exit(1)

    total_written = 0
    for path in files:
        console.rule(f"[bold]{path.name}")
        data = _load_json(path)
        total_written += record_from_dict(data, dry_run=args.dry_run, db_path=args.db_path)

    if len(files) > 1:
        console.print(f"\n[bold]Done.[/bold] {total_written} total rows written across {len(files)} files.")


if __name__ == "__main__":
    main()
