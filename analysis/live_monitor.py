"""
Live monitoring of current epoch bribe accumulation and vote allocation.
"""

import logging
import time
from datetime import datetime, timezone

from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from config import Config
from src.bribe_tracker import BribeTracker
from src.database import Database
from src.indexer import HydrexIndexer
from src.utils import time_until

logger = logging.getLogger(__name__)
console = Console()


class LiveMonitor:
    """Monitors current epoch in real-time."""

    def __init__(
        self,
        indexer: HydrexIndexer,
        database: Database,
        bribe_tracker: BribeTracker,
    ):
        """
        Initialize live monitor.

        Args:
            indexer: Blockchain indexer
            database: Database instance
            bribe_tracker: Bribe tracker
        """
        self.indexer = indexer
        self.database = database
        self.bribe_tracker = bribe_tracker
        logger.info("Live monitor initialized")

    def get_current_epoch(self) -> int:
        """Get current epoch timestamp."""
        return Config.get_current_epoch_timestamp()

    def get_epoch_end(self, epoch: int) -> int:
        """Get epoch end timestamp."""
        return epoch + Config.EPOCH_DURATION

    def create_status_display(self, epoch_data: dict) -> Panel:
        """
        Create rich display for current epoch status.

        Args:
            epoch_data: Dictionary with epoch information

        Returns:
            Rich Panel for display
        """
        epoch = epoch_data["epoch"]
        epoch_end = self.get_epoch_end(epoch)
        time_remaining = time_until(epoch_end)

        # Create bribes table
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Gauge", style="cyan")
        table.add_column("Pool", style="white")
        table.add_column("Votes", justify="right", style="yellow")
        table.add_column("Bribes", justify="right", style="green")
        table.add_column("$/Vote", justify="right", style="blue")

        for gauge_info in sorted(
            epoch_data["gauges"], key=lambda x: x["bribes"], reverse=True
        )[:10]:
            votes_per_dollar = (
                gauge_info["bribes"] / gauge_info["votes"]
                if gauge_info["votes"] > 0
                else 0
            )

            table.add_row(
                gauge_info["address"][:10] + "...",
                gauge_info["pool"][:20] + "...",
                f"{gauge_info['votes']:,}",
                f"${gauge_info['bribes']:,.2f}",
                f"${votes_per_dollar:.4f}",
            )

        # Status info
        status_text = f"""
[bold]Epoch:[/bold] {epoch}
[bold]Ends In:[/bold] {time_remaining}
[bold]Total Bribes:[/bold] ${epoch_data['total_bribes']:,.2f}
[bold]Total Votes:[/bold] {epoch_data['total_votes']:,}
[bold]Active Gauges:[/bold] {len(epoch_data['gauges'])}

[bold]Safe to Vote:[/bold] {"✅ YES" if Config.is_in_safe_voting_window() else "❌ NO"}

[dim]Last Updated: {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")}[/dim]
"""

        panel = Panel.fit(
            f"{status_text}\n{table}",
            title="🔴 LIVE: Current Epoch Monitor",
            border_style="red",
        )

        return panel

    def fetch_current_epoch_data(self) -> dict:
        """
        Fetch current epoch data from blockchain.

        Returns:
            Dictionary with current epoch information
        """
        epoch = self.get_current_epoch()

        # Get all gauges
        gauges = self.database.get_all_gauges(alive_only=True)

        gauge_data = []
        total_votes = 0
        total_bribes = 0.0

        for gauge in gauges:
            # Get current votes
            votes = self.indexer.get_gauge_weight(gauge.address)

            # Get bribes for this epoch
            bribes = self.database.get_bribes_for_gauge(epoch, gauge.address)
            total_bribe_value = sum(b.usd_value for b in bribes)

            gauge_data.append(
                {
                    "address": gauge.address,
                    "pool": gauge.pool,
                    "votes": votes,
                    "bribes": total_bribe_value,
                }
            )

            total_votes += votes
            total_bribes += total_bribe_value

        return {
            "epoch": epoch,
            "total_votes": total_votes,
            "total_bribes": total_bribes,
            "gauges": gauge_data,
        }

    def monitor(self, update_interval: int = 3600) -> None:
        """
        Start live monitoring (runs continuously).

        Args:
            update_interval: Seconds between updates (default 1 hour)
        """
        console.print("[bold green]Starting live monitor...[/bold green]")
        console.print(f"Updates every {update_interval} seconds")
        console.print("Press Ctrl+C to stop\n")

        try:
            with Live(console=console, refresh_per_second=0.1) as live:
                while True:
                    try:
                        # Fetch current data
                        epoch_data = self.fetch_current_epoch_data()

                        # Update display
                        live.update(self.create_status_display(epoch_data))

                        # Wait for next update
                        time.sleep(update_interval)

                    except KeyboardInterrupt:
                        raise
                    except Exception as e:
                        logger.error(f"Error in monitor loop: {e}")
                        time.sleep(60)  # Wait 1 minute on error

        except KeyboardInterrupt:
            console.print("\n[yellow]Monitor stopped by user[/yellow]")
