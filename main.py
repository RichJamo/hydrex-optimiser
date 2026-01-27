"""
Main CLI entry point for Hydrex Vote Optimizer.
"""

import logging

import click
from rich.console import Console

from analysis.historical import HistoricalAnalyzer
from analysis.live_monitor import LiveMonitor
from analysis.recommender import VoteRecommender
from config import Config
from src.bribe_tracker import BribeTracker
from src.database import Database
from src.indexer import HydrexIndexer
from src.price_feed import PriceFeed
from src.utils import setup_logging

console = Console()


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging")
def cli(debug):
    """Hydrex Vote Optimizer - Maximize your bribe returns."""
    log_level = "DEBUG" if debug else Config.LOG_LEVEL
    setup_logging(log_level, Config.LOG_FILE)


@cli.command()
def setup():
    """Initialize database and test connection."""
    console.print("[bold]Setting up Hydrex Vote Optimizer...[/bold]\n")

    # Validate configuration
    errors = Config.validate()
    if errors:
        console.print("[bold red]Configuration errors:[/bold red]")
        for error in errors:
            console.print(f"  ❌ {error}")
        return

    console.print("✅ Configuration valid")

    # Initialize database
    db = Database(Config.DATABASE_PATH)
    db.create_tables()
    console.print(f"✅ Database initialized: {Config.DATABASE_PATH}")

    # Test RPC connection
    try:
        indexer = HydrexIndexer(Config.RPC_URL, Config.VOTER_ADDRESS)
        block = indexer.get_latest_block()
        console.print(f"✅ Connected to Linea RPC (block: {block})")
    except Exception as e:
        console.print(f"[red]❌ RPC connection failed: {e}[/red]")
        return

    console.print("\n[bold green]Setup complete! Ready to use.[/bold green]")


@cli.command()
@click.option("--epochs", default=12, help="Number of epochs to analyze")
def historical(epochs):
    """Analyze historical performance."""
    console.print(f"[bold]Analyzing last {epochs} epochs...[/bold]\n")

    db = Database(Config.DATABASE_PATH)
    analyzer = HistoricalAnalyzer(db, Config.YOUR_VOTING_POWER)

    results = analyzer.analyze_recent_epochs(epochs)
    analyzer.display_summary(results)


@cli.command()
@click.option("--interval", default=3600, help="Update interval in seconds")
def monitor(interval):
    """Start live monitoring of current epoch."""
    db = Database(Config.DATABASE_PATH)
    indexer = HydrexIndexer(Config.RPC_URL, Config.VOTER_ADDRESS)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY)
    bribe_tracker = BribeTracker(indexer, db, price_feed)

    monitor = LiveMonitor(indexer, db, bribe_tracker)
    monitor.monitor(update_interval=interval)


@cli.command()
@click.option("--format", type=click.Choice(["rich", "json"]), default="rich")
def recommend(format):
    """Get vote recommendation for current epoch."""
    db = Database(Config.DATABASE_PATH)
    indexer = HydrexIndexer(Config.RPC_URL, Config.VOTER_ADDRESS)

    recommender = VoteRecommender(indexer, db, Config.YOUR_VOTING_POWER)

    try:
        recommendation = recommender.generate_recommendation()
        if recommendation:
            output = recommender.display_recommendation(recommendation, format)
            if output:
                console.print(output)
        else:
            console.print("[yellow]No recommendation available (insufficient data)[/yellow]")
    except Exception as e:
        console.print(f"[red]Error generating recommendation: {e}[/red]")
        logging.exception("Recommendation error")


@cli.command()
@click.option("--start-block", type=int, help="Starting block number")
@click.option("--epochs", default=12, help="Number of epochs to backfill")
def backfill(start_block, epochs):
    """Backfill historical data from blockchain."""
    console.print(f"[bold]Backfilling {epochs} epochs of data...[/bold]\n")

    db = Database(Config.DATABASE_PATH)
    indexer = HydrexIndexer(Config.RPC_URL, Config.VOTER_ADDRESS)
    price_feed = PriceFeed(Config.COINGECKO_API_KEY)
    bribe_tracker = BribeTracker(indexer, db, price_feed)

    # Calculate block range
    latest_block = indexer.get_latest_block()
    if start_block is None:
        # Estimate blocks for epochs (assuming ~2 sec block time on Linea)
        blocks_per_epoch = Config.EPOCH_DURATION // 2
        start_block = latest_block - (blocks_per_epoch * epochs)

    console.print(f"Fetching data from block {start_block} to {latest_block}")

    # Fetch gauge created events
    console.print("\n[cyan]Indexing gauges...[/cyan]")
    gauge_events = indexer.fetch_gauge_created_events(start_block, latest_block)

    for event in gauge_events:
        db.save_gauge(
            address=event["gauge"],
            pool=event["gauge"],  # Pool info if available
            internal_bribe=event["internal_bribe"],
            external_bribe=event["external_bribe"],
        )

    console.print(f"✅ Indexed {len(gauge_events)} gauges")

    # Fetch per-gauge votes (GaugeVote entities)
    console.print("\n[cyan]Indexing per-gauge votes...[/cyan]")
    gauge_votes = indexer.subgraph_client.fetch_all_paginated(
        indexer.subgraph_client.fetch_gauge_votes,
        block_gte=start_block,
        block_lte=latest_block
    ) if indexer.subgraph_client else []
    
    # Group votes by epoch and gauge, aggregate by gauge
    from collections import defaultdict
    votes_by_epoch_gauge = defaultdict(lambda: defaultdict(int))
    epochs_seen = set()
    
    for gv in gauge_votes:
        epoch_timestamp = int(gv["epoch"])
        gauge_address = gv["gauge"]["address"]
        weight = int(gv["weight"]) / 1e18  # Convert from wei to tokens
        
        votes_by_epoch_gauge[epoch_timestamp][gauge_address] += weight
        epochs_seen.add(epoch_timestamp)
    
    # Save epochs
    for epoch_ts in epochs_seen:
        if not db.get_epoch(epoch_ts):
            db.save_epoch(epoch_ts)
    
    # Save aggregated votes
    vote_count = 0
    for epoch_ts, gauges in votes_by_epoch_gauge.items():
        for gauge_addr, total_weight in gauges.items():
            db.save_vote(epoch_ts, gauge_addr, total_weight)
            vote_count += 1
    
    console.print(f"✅ Indexed {len(gauge_votes)} individual votes → {vote_count} gauge totals across {len(epochs_seen)} epochs")

    # Index bribes from subgraph
    console.print("\n[cyan]Indexing bribes (RewardAdded events)...[/cyan]")
    bribes = indexer.subgraph_client.fetch_all_paginated(
        indexer.subgraph_client.fetch_bribes,
        block_gte=start_block,
        block_lte=latest_block
    ) if indexer.subgraph_client else []
    
    bribe_count = 0
    for bribe in bribes:
        epoch_timestamp = int(bribe["epoch"])
        bribe_contract = bribe["bribeContract"]
        reward_token = bribe["rewardToken"]
        amount = int(bribe["amount"])
        timestamp = int(bribe["blockTimestamp"])
        
        db.save_bribe(
            epoch=epoch_timestamp,
            bribe_contract=bribe_contract,
            reward_token=reward_token,
            amount=amount / 1e18,  # Convert from wei to token amount
            timestamp=timestamp
        )
        bribe_count += 1
    
    console.print(f"✅ Indexed {bribe_count} bribe/reward events")

    console.print("\n[bold green]Backfill complete![/bold green]")


if __name__ == "__main__":
    cli()
