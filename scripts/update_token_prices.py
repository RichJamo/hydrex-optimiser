#!/usr/bin/env python3
"""
Update token prices for all tokens in the database.

This script:
1. Finds all unique token addresses from live_reward_token_samples and reward_tokens
2. Fetches current USD prices from CoinGecko
3. Updates the token_prices table with fresh data
"""

import argparse
import os
import sqlite3
import sys
import time
from typing import Set, Tuple

from dotenv import load_dotenv
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeRemainingColumn

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.settings import DATABASE_PATH
from src.database import Database
from src.price_feed import PriceFeed

load_dotenv()
console = Console()


def collect_token_addresses(
    conn: sqlite3.Connection,
    missing_only: bool = False,
    max_age_hours: float = 24.0,
) -> Tuple[Set[str], int]:
    """Collect all unique token addresses from the database."""
    cur = conn.cursor()
    token_addresses: Set[str] = set()
    
    # From live_reward_token_samples
    console.print("[cyan]Collecting tokens from live_reward_token_samples...[/cyan]")
    rows = cur.execute("SELECT DISTINCT reward_token FROM live_reward_token_samples").fetchall()
    for (token_addr,) in rows:
        if token_addr:
            token_addresses.add(token_addr.lower())
    console.print(f"  Found {len(token_addresses)} tokens")
    
    # From reward_tokens (if exists)
    try:
        rows = cur.execute("SELECT DISTINCT reward_token FROM reward_tokens").fetchall()
        initial_count = len(token_addresses)
        for (token_addr,) in rows:
            if token_addr:
                token_addresses.add(token_addr.lower())
        console.print(f"[cyan]Collecting tokens from reward_tokens...[/cyan]")
        console.print(f"  Added {len(token_addresses) - initial_count} new tokens")
    except sqlite3.OperationalError:
        pass  # Table doesn't exist
    
    # From historical_token_prices (for completeness)
    try:
        rows = cur.execute("SELECT DISTINCT token_address FROM historical_token_prices").fetchall()
        initial_count = len(token_addresses)
        for (token_addr,) in rows:
            if token_addr:
                token_addresses.add(token_addr.lower())
        console.print(f"[cyan]Collecting tokens from historical_token_prices...[/cyan]")
        console.print(f"  Added {len(token_addresses) - initial_count} new tokens")
    except sqlite3.OperationalError:
        pass  # Table doesn't exist
    
    # Filter to missing-only if requested
    skipped_count = 0

    if missing_only:
        existing_prices = set()
        try:
            rows = cur.execute("SELECT LOWER(token_address) FROM token_prices").fetchall()
            existing_prices = {row[0] for row in rows}
        except sqlite3.OperationalError:
            pass
        
        initial_count = len(token_addresses)
        token_addresses = {addr for addr in token_addresses if addr not in existing_prices}
        skipped_count = initial_count - len(token_addresses)
        console.print(f"[cyan]Filtering to missing tokens only...[/cyan]")
        console.print(f"  Skipped {skipped_count} tokens with existing prices")
        console.print(f"  Targeting {len(token_addresses)} missing tokens")
    else:
        # Default mode: refresh if price is missing or stale
        cutoff_ts = int(time.time() - max(0.0, float(max_age_hours)) * 3600)
        existing_updated_at: dict[str, int] = {}
        try:
            rows = cur.execute("SELECT LOWER(token_address), COALESCE(updated_at, 0) FROM token_prices").fetchall()
            existing_updated_at = {str(addr): int(updated_at or 0) for addr, updated_at in rows if addr}
        except sqlite3.OperationalError:
            existing_updated_at = {}

        target_addresses: Set[str] = set()
        fresh_skipped = 0
        stale_targeted = 0
        missing_targeted = 0

        for addr in token_addresses:
            updated_at = existing_updated_at.get(addr)
            if updated_at is None:
                target_addresses.add(addr)
                missing_targeted += 1
            elif updated_at < cutoff_ts:
                target_addresses.add(addr)
                stale_targeted += 1
            else:
                fresh_skipped += 1

        skipped_count = fresh_skipped
        token_addresses = target_addresses
        console.print(f"[cyan]Filtering to missing-or-stale tokens (max age: {max_age_hours:.1f}h)...[/cyan]")
        console.print(f"  Missing targeted: {missing_targeted}")
        console.print(f"  Stale targeted: {stale_targeted}")
        console.print(f"  Fresh skipped: {fresh_skipped}")
    
    return token_addresses, skipped_count


def update_prices(
    database: Database,
    price_feed: PriceFeed,
    token_addresses: list[str],
    batch_size: int = 50,
    delay_between_batches: float = 2.0,
) -> dict:
    """
    Update prices for all tokens in batches.
    
    Returns:
        Dict with stats: {successful: int, failed: int, skipped: int}
    """
    stats = {"successful": 0, "failed": 0, "skipped": 0}
    total = len(token_addresses)
    
    console.print(f"\n[bold cyan]Updating prices for {total} tokens...[/bold cyan]")
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("[cyan]Fetching prices...", total=total)
        
        # Process in batches to avoid rate limits
        for i in range(0, total, batch_size):
            batch = token_addresses[i : i + batch_size]

            # One API call per batch (much less rate-limit pressure)
            batch_prices = {}
            try:
                batch_prices = price_feed.fetch_batch_prices_by_address(batch)
            except Exception:
                batch_prices = {}

            for token_addr in batch:
                token_addr_l = token_addr.lower()
                try:
                    price = batch_prices.get(token_addr_l)

                    # Fallback to single-token lookup if missing from batch response
                    if price is None:
                        price = price_feed.get_token_price(token_addr_l)

                    if price is not None:
                        stats["successful"] += 1
                        try:
                            database.save_token_price(token_addr_l, float(price))
                        except Exception:
                            pass
                        progress.update(
                            task,
                            advance=1,
                            description=f"[green]✓ {token_addr_l[:10]}... ${price:.6f}",
                        )
                    else:
                        stats["failed"] += 1
                        progress.update(
                            task,
                            advance=1,
                            description=f"[yellow]✗ {token_addr_l[:10]}... (no price)",
                        )

                except Exception as e:
                    stats["failed"] += 1
                    progress.update(
                        task,
                        advance=1,
                        description=f"[red]✗ {token_addr_l[:10]}... Error: {e}",
                    )
            
            # Delay between batches to avoid rate limiting
            if i + batch_size < total:
                time.sleep(delay_between_batches)
    
    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Update token prices for all tokens in the database"
    )
    parser.add_argument(
        "--db-path",
        default=DATABASE_PATH,
        help=f"Path to database (default: {DATABASE_PATH})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Number of tokens to fetch before pausing (default: 50)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay in seconds between batches (default: 2.0)",
    )
    parser.add_argument(
        "--api-key",
        default=os.getenv("COINGECKO_API_KEY"),
        help="CoinGecko API key (or set COINGECKO_API_KEY env var)",
    )
    parser.add_argument(
        "--missing-only",
        action="store_true",
        help="Only fetch prices for tokens not already in token_prices table",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=24.0,
        help="Refresh prices older than this age in hours (default: 24.0). Ignored by --missing-only",
    )
    args = parser.parse_args()
    
    console.print("[bold]Token Price Updater[/bold]\n")
    
    # Initialize database and price feed
    database = Database(args.db_path)
    price_feed = PriceFeed(api_key=args.api_key, database=database)
    
    # Connect to database for queries
    conn = sqlite3.connect(args.db_path)
    
    try:
        # Collect all token addresses
        console.print("[cyan]Step 1: Collecting token addresses...[/cyan]")
        token_addresses, skipped_count = collect_token_addresses(
            conn,
            missing_only=args.missing_only,
            max_age_hours=args.max_age_hours,
        )
        console.print(f"[green]✓ Total unique tokens: {len(token_addresses)}[/green]\n")
        
        if not token_addresses:
            console.print("[yellow]No tokens found in database[/yellow]")
            return
        
        # Convert to sorted list for deterministic processing
        token_list = sorted(list(token_addresses))
        
        # Update prices
        console.print("[cyan]Step 2: Fetching prices from CoinGecko...[/cyan]")
        stats = update_prices(
            database=database,
            price_feed=price_feed,
            token_addresses=token_list,
            batch_size=args.batch_size,
            delay_between_batches=args.delay,
        )
        stats["skipped"] = int(skipped_count)
        
        # Print summary
        console.print(f"\n[bold]Summary:[/bold]")
        console.print(f"  [green]Successful: {stats['successful']}[/green]")
        console.print(f"  [yellow]Failed: {stats['failed']}[/yellow]")
        console.print(f"  [blue]Skipped: {stats['skipped']}[/blue]")
        console.print(f"  [cyan]Total targeted: {len(token_list)}[/cyan]")
        
        success_rate = (stats['successful'] / len(token_list) * 100) if token_list else 0
        console.print(f"\n[bold]Success rate: {success_rate:.1f}%[/bold]")
        
    finally:
        conn.close()


if __name__ == "__main__":
    main()
