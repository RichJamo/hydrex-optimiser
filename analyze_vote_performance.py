#!/usr/bin/env python3
"""
Analyze vote performance and find optimal gauges for next vote.
Compares your last vote results with current opportunities.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from web3 import Web3

from config import Config
from src.database import Database
from src.indexer import HydrexIndexer
from src.optimizer import VoteOptimizer
from src.price_feed import PriceFeed

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
console = Console()


class VotePerformanceAnalyzer:
    """Analyzes vote performance and recommends optimizations."""

    def __init__(self):
        """Initialize analyzer with web3 and database."""
        self.w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))
        self.voting_power = Config.YOUR_VOTING_POWER
        self.your_address = Config.YOUR_ADDRESS
        
        # Load VoterV5 ABI
        with open("voterv5_abi.json", "r") as f:
            self.voter_abi = json.load(f)
        
        # Initialize voter contract
        self.voter = self.w3.eth.contract(
            address=Web3.to_checksum_address(Config.VOTER_ADDRESS),
            abi=self.voter_abi
        )
        
        # Initialize components
        try:
            self.database = Database(Config.DATABASE_PATH)
            self.indexer = HydrexIndexer(self.w3, self.voter, self.database)
            self.optimizer = VoteOptimizer(self.voting_power)
            self.price_feed = PriceFeed()
        except Exception as e:
            logger.warning(f"Could not initialize all components: {e}")
            self.database = None
            self.indexer = None
            self.optimizer = VoteOptimizer(self.voting_power)

    def analyze_last_vote(
        self, 
        pools_voted: List[str], 
        rewards_received: Dict[str, float]
    ) -> Dict:
        """
        Analyze performance of last vote.
        
        Args:
            pools_voted: List of pool addresses voted for
            rewards_received: Dict mapping pool address to USD reward value
            
        Returns:
            Analysis results
        """
        console.print("\n[bold cyan]Your Last Vote Performance[/bold cyan]")
        console.print("=" * 80)
        
        # Calculate metrics
        total_rewards = sum(rewards_received.values())
        votes_per_pool = self.voting_power // len(pools_voted)
        reward_per_vote = total_rewards / self.voting_power
        reward_per_1k_votes = reward_per_vote * 1000
        
        # Create results table
        table = Table(title="Pool Performance", show_header=True)
        table.add_column("Pool", style="cyan")
        table.add_column("Rewards", justify="right", style="green")
        table.add_column("$/1K Votes", justify="right", style="yellow")
        table.add_column("% of Total", justify="right", style="magenta")
        
        for pool, reward in sorted(
            rewards_received.items(), 
            key=lambda x: x[1], 
            reverse=True
        ):
            pct = (reward / total_rewards) * 100
            per_1k = (reward / votes_per_pool) * 1000
            table.add_row(
                pool[:10] + "..." + pool[-6:],
                f"${reward:.2f}",
                f"${per_1k:.2f}",
                f"{pct:.1f}%"
            )
        
        console.print(table)
        
        # Summary stats
        console.print(f"\n[bold]Summary:[/bold]")
        console.print(f"  Total Rewards: [green]${total_rewards:.2f}[/green]")
        console.print(f"  Voting Power Used: [cyan]{self.voting_power:,}[/cyan]")
        console.print(f"  Pools Voted: [cyan]{len(pools_voted)}[/cyan]")
        console.print(f"  Votes per Pool: [cyan]{votes_per_pool:,}[/cyan]")
        console.print(f"  Return per Vote: [yellow]${reward_per_vote:.6f}[/yellow]")
        console.print(f"  Return per 1K Votes: [yellow]${reward_per_1k_votes:.3f}[/yellow]")
        
        return {
            "total_rewards": total_rewards,
            "reward_per_vote": reward_per_vote,
            "reward_per_1k": reward_per_1k_votes,
            "pools": len(pools_voted),
            "votes_per_pool": votes_per_pool
        }

    def get_current_gauge_data(self) -> List[Dict]:
        """
        Fetch current gauge data including votes and bribes.
        
        Returns:
            List of gauge data dictionaries
        """
        console.print("\n[bold cyan]Fetching Current Gauge Data[/bold cyan]")
        console.print("=" * 80)
        
        gauge_data = []
        
        # Try to get data from database first
        if self.database:
            try:
                gauges = self.database.get_all_gauges(alive_only=True)
                current_epoch = Config.get_current_epoch_timestamp()
                
                console.print(f"Found {len(gauges)} active gauges in database")
                
                for gauge in gauges[:50]:  # Limit to top 50 to avoid rate limits
                    try:
                        # Get current votes from blockchain
                        votes = self.voter.functions.weights(
                            Web3.to_checksum_address(gauge.pool)
                        ).call()
                        
                        # Get bribes from database
                        bribes = self.database.get_bribes_for_gauge(
                            current_epoch, 
                            gauge.address
                        )
                        total_bribes = sum(b.usd_value for b in bribes)
                        
                        if total_bribes > 0:  # Only include gauges with bribes
                            gauge_data.append({
                                "address": gauge.address,
                                "pool": gauge.pool,
                                "current_votes": votes,
                                "bribes_usd": total_bribes,
                            })
                    except Exception as e:
                        logger.debug(f"Error fetching gauge {gauge.address}: {e}")
                        continue
                        
            except Exception as e:
                logger.error(f"Error querying database: {e}")
        
        # Sort by bribes descending
        gauge_data.sort(key=lambda x: x["bribes_usd"], reverse=True)
        
        console.print(f"\n[green]Found {len(gauge_data)} gauges with active bribes[/green]")
        
        return gauge_data

    def calculate_expected_returns(
        self, 
        gauge_data: List[Dict],
        allocation: Dict[str, int]
    ) -> Tuple[float, List[Dict]]:
        """
        Calculate expected returns for a given allocation.
        
        Args:
            gauge_data: List of gauge data
            allocation: Vote allocation mapping
            
        Returns:
            Tuple of (total_return, details_list)
        """
        total_return = 0.0
        details = []
        
        for gauge in gauge_data:
            address = gauge["address"]
            if address not in allocation:
                continue
                
            your_votes = allocation[address]
            current_votes = gauge["current_votes"]
            bribes = gauge["bribes_usd"]
            
            # Calculate expected return
            total_votes = current_votes + your_votes
            your_share = your_votes / total_votes if total_votes > 0 else 0
            expected_return = bribes * your_share
            
            total_return += expected_return
            
            details.append({
                "address": address,
                "pool": gauge["pool"],
                "your_votes": your_votes,
                "current_votes": current_votes,
                "bribes": bribes,
                "expected_return": expected_return,
                "vote_share": your_share * 100
            })
        
        # Sort by expected return
        details.sort(key=lambda x: x["expected_return"], reverse=True)
        
        return total_return, details

    def generate_recommendations(
        self, 
        last_performance: Dict,
        gauge_data: List[Dict]
    ):
        """
        Generate voting recommendations based on current opportunities.
        
        Args:
            last_performance: Results from analyze_last_vote
            gauge_data: Current gauge data
        """
        console.print("\n[bold cyan]Optimization Analysis[/bold cyan]")
        console.print("=" * 80)
        
        if not gauge_data:
            console.print("[red]No gauge data available for optimization[/red]")
            return
        
        # Strategy 1: Concentrate all votes on single best gauge
        best_gauge = gauge_data[0]
        single_allocation = {best_gauge["address"]: self.voting_power}
        single_return, single_details = self.calculate_expected_returns(
            gauge_data, single_allocation
        )
        
        # Strategy 2: Quadratic optimization (multiple gauges)
        multi_allocation = self.optimizer.quadratic_optimization(gauge_data)
        multi_return, multi_details = self.calculate_expected_returns(
            gauge_data, multi_allocation
        )
        
        # Strategy 3: Top 4 gauges equally (matching last vote structure)
        top4_allocation = {}
        votes_per_gauge = self.voting_power // 4
        for gauge in gauge_data[:4]:
            top4_allocation[gauge["address"]] = votes_per_gauge
        top4_return, top4_details = self.calculate_expected_returns(
            gauge_data, top4_allocation
        )
        
        # Comparison table
        table = Table(title="Strategy Comparison", show_header=True)
        table.add_column("Strategy", style="cyan")
        table.add_column("Expected Return", justify="right", style="green")
        table.add_column("vs Last Vote", justify="right", style="yellow")
        table.add_column("Gauges", justify="right", style="magenta")
        
        last_return = last_performance["total_rewards"]
        
        strategies = [
            ("Last Vote (Actual)", last_return, 0, last_performance["pools"]),
            ("Single Best Gauge", single_return, single_return - last_return, 1),
            ("Top 4 Equal Split", top4_return, top4_return - last_return, 4),
            ("Optimized Multi-Gauge", multi_return, multi_return - last_return, len(multi_allocation)),
        ]
        
        for name, expected, diff, gauges in strategies:
            diff_pct = (diff / last_return * 100) if last_return > 0 else 0
            diff_str = f"+${diff:.2f} ({diff_pct:+.1f}%)" if diff != 0 else "-"
            table.add_row(
                name,
                f"${expected:.2f}",
                diff_str,
                str(gauges)
            )
        
        console.print(table)
        
        # Recommend best strategy
        best_strategy = max(
            [
                ("single", single_return, single_details),
                ("multi", multi_return, multi_details),
                ("top4", top4_return, top4_details)
            ],
            key=lambda x: x[1]
        )
        
        strategy_name, best_return, best_details = best_strategy
        improvement = ((best_return - last_return) / last_return * 100) if last_return > 0 else 0
        
        console.print(f"\n[bold green]Recommended Strategy:[/bold green]")
        if strategy_name == "single":
            console.print("  [bold]Concentrate all votes on single best gauge[/bold]")
        elif strategy_name == "top4":
            console.print("  [bold]Split equally across top 4 gauges[/bold]")
        else:
            console.print("  [bold]Optimized allocation across multiple gauges[/bold]")
        
        console.print(f"  Expected Return: [green]${best_return:.2f}[/green]")
        console.print(f"  Improvement: [yellow]{improvement:+.1f}%[/yellow] (${best_return - last_return:+.2f})")
        
        # Show detailed allocation
        console.print(f"\n[bold]Recommended Allocation:[/bold]")
        detail_table = Table(show_header=True)
        detail_table.add_column("Pool", style="cyan")
        detail_table.add_column("Votes", justify="right", style="white")
        detail_table.add_column("Current Votes", justify="right", style="white")
        detail_table.add_column("Your Share", justify="right", style="yellow")
        detail_table.add_column("Bribes", justify="right", style="green")
        detail_table.add_column("Expected Return", justify="right", style="green")
        
        for detail in best_details:
            detail_table.add_row(
                detail["pool"][:10] + "..." + detail["pool"][-6:],
                f"{detail['your_votes']:,}",
                f"{detail['current_votes']:,}",
                f"{detail['vote_share']:.1f}%",
                f"${detail['bribes']:.2f}",
                f"${detail['expected_return']:.2f}"
            )
        
        console.print(detail_table)
        
        # Show top opportunities
        console.print(f"\n[bold cyan]Top Opportunities (by total bribes):[/bold cyan]")
        opp_table = Table(show_header=True)
        opp_table.add_column("Rank", justify="right", style="cyan")
        opp_table.add_column("Pool", style="cyan")
        opp_table.add_column("Current Votes", justify="right", style="white")
        opp_table.add_column("Total Bribes", justify="right", style="green")
        opp_table.add_column("$/Vote Ratio", justify="right", style="yellow")
        
        for i, gauge in enumerate(gauge_data[:10], 1):
            ratio = gauge["bribes_usd"] / gauge["current_votes"] if gauge["current_votes"] > 0 else float('inf')
            opp_table.add_row(
                str(i),
                gauge["pool"][:10] + "..." + gauge["pool"][-6:],
                f"{gauge['current_votes']:,}",
                f"${gauge['bribes_usd']:.2f}",
                f"${ratio:.6f}" if ratio != float('inf') else "∞"
            )
        
        console.print(opp_table)


def main():
    """Main execution function."""
    console.print(Panel.fit(
        "[bold cyan]Hydrex Vote Performance Analyzer[/bold cyan]\n"
        "Analyze past performance and find optimal voting opportunities",
        border_style="cyan"
    ))
    
    # Your last vote data
    pools_voted = [
        "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44",  # WETH/cbBTC
        "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c",  # USDC/cbBTC
        "0x680581725840958141Bb328666D8Fc185aC4FA49",  # BNKR/WETH
        "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33",  # kVCM/USDC
    ]
    
    rewards_received = {
        "0x3f9b863EF4B295d6Ba370215bcCa3785FCC44f44": 246.75,
        "0x0BA69825c4C033e72309F6AC0Bde0023b15Cc97c": 236.11,
        "0xEf96Ec76eEB36584FC4922e9fA268e0780170f33": 245.80,
        "0x680581725840958141Bb328666D8Fc185aC4FA49": 227.67,
    }
    
    # Initialize analyzer
    analyzer = VotePerformanceAnalyzer()
    
    # Analyze last vote
    last_performance = analyzer.analyze_last_vote(pools_voted, rewards_received)
    
    # Get current opportunities
    gauge_data = analyzer.get_current_gauge_data()
    
    # Generate recommendations
    if gauge_data:
        analyzer.generate_recommendations(last_performance, gauge_data)
    else:
        console.print("\n[yellow]⚠ Could not fetch current gauge data.[/yellow]")
        console.print("Try running the indexer to populate the database first:")
        console.print("  [cyan]python -m src.indexer[/cyan]")


if __name__ == "__main__":
    main()
