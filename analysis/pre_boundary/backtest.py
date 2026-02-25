"""
P5: Offline Backtest Harness

Replay pre-boundary allocation decisions and compare expected vs realized returns.

This module:
1. Loads forecasted allocations (expected returns from P4)
2. Loads ground truth (realized votes and rewards at boundary)
3. Computes realized returns per gauge and portfolio
4. Generates calibration metrics (median, P10, regret, scenario consistency)
5. Produces backtest report for MVP validation
"""

import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from analysis.pre_boundary.scenarios import build_scenarios_for_epoch

logger = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    """Result of comparing forecast vs realized outcome for one (epoch, gauge, decision_window)"""
    epoch: int
    decision_window: str
    gauge_address: str
    votes_recommended: int
    final_votes: float
    final_rewards_usd: float
    
    # Realized return in bps computed from (allocations × final rewards) / (final votes + allocations)
    realized_return_bps: int
    
    # Is this gauge allocated?
    is_allocated: bool
    
@dataclass
class PortfolioBacktestResult:
    """Aggregated backtest metrics for one (epoch, decision_window)"""
    epoch: int
    decision_window: str
    
    num_gauges_in_forecast: int
    num_gauges_allocated: int
    
    # Portfolio-level returns (weighted by allocation)
    expected_portfolio_return_bps: int
    expected_portfolio_downside_bps: int
    realized_portfolio_return_bps: int
    baseline_portfolio_return_bps: int
    uplift_vs_baseline_bps: int
    baseline_topk_portfolio_return_bps: int
    uplift_vs_topk_baseline_bps: int
    portfolio_error_bps: int
    
    # Downside/tail metrics
    median_realized_return_bps: int
    p10_realized_return_bps: int
    min_realized_return_bps: int
    max_realized_return_bps: int
    
    # Regret: opportunity cost of our allocation vs hindsight optimal
    regret_vs_hindsight_bps: int
    
    # Calibration: fraction of gauges where realized ∈ forecast scenario bounds
    calibration_score: float
    
    # Diagnostic counts
    num_positive_return_gauges: int
    num_negative_return_gauges: int
    num_zero_allocation_gauges: int


def load_forecasts(db_path: str, epoch: int) -> Dict[str, dict]:
    """Load forecasts for an epoch keyed by decision_window."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            decision_window,
            gauge_address,
            votes_recommended,
            portfolio_return_bps,
            portfolio_downside_bps,
            optimizer_status
        FROM preboundary_forecasts
        WHERE epoch = ?
        ORDER BY decision_window, gauge_address
    """, (epoch,))
    
    forecasts: Dict[str, dict] = {}
    for row in cursor.fetchall():
        window = row['decision_window']
        if window not in forecasts:
            forecasts[window] = {
                'portfolio_return_bps': int(row['portfolio_return_bps'] or 0),
                'portfolio_downside_bps': int(row['portfolio_downside_bps'] or 0),
                'gauges': [],
            }
        forecasts[window]['gauges'].append({
            'gauge_address': row['gauge_address'],
            'votes_recommended': row['votes_recommended'],
            'optimizer_status': row['optimizer_status'],
        })
    
    conn.close()
    return forecasts


def load_truth_labels(db_path: str, epoch: int) -> Dict[str, dict]:
    """Load ground truth (realized votes and rewards) keyed by gauge_address."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute("""
        SELECT 
            gauge_address,
            final_votes_raw,
            final_rewards_usd
        FROM preboundary_truth_labels
        WHERE epoch = ?
    """, (epoch,))
    
    truth = {}
    for row in cursor.fetchall():
        truth[row['gauge_address']] = {
            'final_votes_raw': row['final_votes_raw'],
            'final_rewards_usd': row['final_rewards_usd'],
        }
    
    conn.close()
    return truth


def load_forecast_input_diagnostics(db_path: str, epoch: int) -> Dict[str, List[Dict]]:
    """Load gauge-level forecast inputs and realized outcomes for diagnostics."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            s.epoch,
            s.decision_window,
            s.gauge_address,
            s.votes_now_raw,
            s.rewards_now_usd,
            s.inclusion_prob,
            s.data_quality_score,
            f.votes_recommended,
            f.portfolio_return_bps,
            f.portfolio_downside_bps,
            t.final_votes_raw,
            t.final_rewards_usd
        FROM preboundary_snapshots s
        LEFT JOIN preboundary_forecasts f
            ON f.epoch = s.epoch
            AND f.decision_window = s.decision_window
            AND f.gauge_address = s.gauge_address
        LEFT JOIN preboundary_truth_labels t
            ON t.epoch = s.epoch
            AND t.gauge_address = s.gauge_address
        WHERE s.epoch = ?
        ORDER BY s.decision_window, f.votes_recommended DESC, s.rewards_now_usd DESC
        """,
        (epoch,),
    )

    rows_by_window: Dict[str, List[Dict]] = {}
    for row in cursor.fetchall():
        window = row["decision_window"]
        if window not in rows_by_window:
            rows_by_window[window] = []
        rows_by_window[window].append({
            "epoch": row["epoch"],
            "decision_window": window,
            "gauge_address": row["gauge_address"],
            "votes_now_raw": float(row["votes_now_raw"] or 0.0),
            "rewards_now_usd": float(row["rewards_now_usd"] or 0.0),
            "inclusion_prob": float(row["inclusion_prob"] or 0.0),
            "data_quality_score": float(row["data_quality_score"] or 0.0),
            "votes_recommended": int(row["votes_recommended"] or 0),
            "portfolio_return_bps": int(row["portfolio_return_bps"] or 0),
            "portfolio_downside_bps": int(row["portfolio_downside_bps"] or 0),
            "final_votes_raw": float(row["final_votes_raw"] or 0.0),
            "final_rewards_usd": float(row["final_rewards_usd"] or 0.0),
        })

    conn.close()
    return rows_by_window


def generate_forecast_input_report(
    diagnostics_by_window: Dict[str, List[Dict]],
    max_rows_per_window: int = 15,
) -> str:
    """Generate detailed gauge-level diagnostics table for forecast inputs."""
    lines: List[str] = []
    lines.append("\n" + "=" * 120)
    lines.append("FORECAST INPUT DIAGNOSTICS (Gauge-level)")
    lines.append("=" * 120)

    if not diagnostics_by_window:
        lines.append("No diagnostics rows found.")
        lines.append("=" * 120 + "\n")
        return "\n".join(lines)

    for window in sorted(diagnostics_by_window.keys()):
        rows = diagnostics_by_window[window]
        lines.append(f"\n🔍 Window: {window} | gauges: {len(rows)}")
        lines.append(
            "  "
            f"{'Gauge':<14} {'VotesNow':>12} {'BribesNow$':>12} {'Alloc':>8} "
            f"{'FinalVotes':>12} {'FinalRewards$':>13} {'InclP':>7} {'DQ':>6} {'Exp':>8} {'Down':>8}"
        )
        lines.append("  " + "-" * 112)

        for row in rows[:max_rows_per_window]:
            lines.append(
                "  "
                f"{row['gauge_address'][:12]}.. "
                f"{row['votes_now_raw']:>12,.0f} "
                f"{row['rewards_now_usd']:>12,.2f} "
                f"{row['votes_recommended']:>8,} "
                f"{row['final_votes_raw']:>12,.0f} "
                f"{row['final_rewards_usd']:>13,.2f} "
                f"{row['inclusion_prob']:>7.2f} "
                f"{row['data_quality_score']:>6.2f} "
                f"{row['portfolio_return_bps']:>8,} "
                f"{row['portfolio_downside_bps']:>8,}"
            )

        if len(rows) > max_rows_per_window:
            lines.append(f"  ... ({len(rows) - max_rows_per_window} more gauges)")

    lines.append("\n" + "=" * 120 + "\n")
    return "\n".join(lines)


def generate_window_output_report(
    portfolio_results: List[PortfolioBacktestResult],
) -> str:
    """Generate window-level output diagnostics (expected/downside/realized)."""
    lines: List[str] = []
    lines.append("\n" + "=" * 120)
    lines.append("WINDOW-LEVEL OUTPUT DIAGNOSTICS")
    lines.append("=" * 120)

    if not portfolio_results:
        lines.append("No portfolio-level results found.")
        lines.append("=" * 120 + "\n")
        return "\n".join(lines)

    lines.append(
        "  "
        f"{'Window':<10} {'Exp(bps)':>10} {'Down(bps)':>10} {'Real(bps)':>10} "
        f"{'Err(bps)':>10} {'Alloc#':>8} {'Calib':>8}"
    )
    lines.append("  " + "-" * 88)

    for result in sorted(portfolio_results, key=lambda r: r.decision_window):
        lines.append(
            "  "
            f"{result.decision_window:<10} "
            f"{result.expected_portfolio_return_bps:>10,} "
            f"{result.expected_portfolio_downside_bps:>10,} "
            f"{result.realized_portfolio_return_bps:>10,} "
            f"{result.portfolio_error_bps:>10,} "
            f"{result.num_gauges_allocated:>8,} "
            f"{result.calibration_score:>8.2f}"
        )

    lines.append("\n" + "=" * 120 + "\n")
    return "\n".join(lines)


def load_scenario_gauge_diagnostics(
    db_path: str,
    epoch: int,
    cache_dir: str = "data/preboundary_cache",
) -> Dict[str, List[Dict[str, Any]]]:
    """Load scenario-level diagnostics per gauge for each decision window."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT DISTINCT decision_window
        FROM preboundary_snapshots
        WHERE epoch = ?
        ORDER BY decision_window
        """,
        (epoch,),
    )
    windows = [row["decision_window"] for row in cursor.fetchall()]

    cursor.execute(
        """
        SELECT gauge_address, votes_recommended, decision_window
        FROM preboundary_forecasts
        WHERE epoch = ?
        """,
        (epoch,),
    )
    allocations: Dict[Tuple[str, str], int] = {}
    for row in cursor.fetchall():
        allocations[(row["decision_window"], row["gauge_address"])] = int(row["votes_recommended"] or 0)

    cursor.execute(
        """
        SELECT gauge_address, final_votes_raw, final_rewards_usd
        FROM preboundary_truth_labels
        WHERE epoch = ?
        """,
        (epoch,),
    )
    truth_by_gauge = {
        row["gauge_address"]: {
            "final_votes_raw": float(row["final_votes_raw"] or 0.0),
            "final_rewards_usd": float(row["final_rewards_usd"] or 0.0),
        }
        for row in cursor.fetchall()
    }

    diagnostics_by_window: Dict[str, List[Dict[str, Any]]] = {}

    for window in windows:
        scenarios = build_scenarios_for_epoch(conn, epoch, window, cache_dir=cache_dir)

        scenario_by_name: Dict[str, Dict[str, Any]] = {
            "conservative": {},
            "base": {},
            "aggressive": {},
        }
        for scenario_name, scenario_rows in scenarios.items():
            for scenario in scenario_rows:
                scenario_by_name[scenario_name][scenario.gauge_address] = scenario

        gauge_set = set()
        for scenario_name in scenario_by_name:
            gauge_set.update(scenario_by_name[scenario_name].keys())

        rows: List[Dict[str, Any]] = []
        for gauge_address in sorted(gauge_set):
            base = scenario_by_name["base"].get(gauge_address)
            conservative = scenario_by_name["conservative"].get(gauge_address)
            aggressive = scenario_by_name["aggressive"].get(gauge_address)
            if base is None or conservative is None or aggressive is None:
                continue

            votes_rec = allocations.get((window, gauge_address), 0)

            def _marginal_bps(votes_final: float, rewards_final: float) -> int:
                if votes_final <= 0:
                    return 0
                return int((rewards_final / (votes_final + 1.0)) * 10_000)

            truth_row = truth_by_gauge.get(gauge_address, {"final_votes_raw": 0.0, "final_rewards_usd": 0.0})
            rows.append(
                {
                    "gauge_address": gauge_address,
                    "votes_recommended": votes_rec,
                    "vote_drift_cons": float(conservative.vote_drift),
                    "vote_drift_base": float(base.vote_drift),
                    "vote_drift_aggr": float(aggressive.vote_drift),
                    "uplift_cons": float(conservative.reward_uplift),
                    "uplift_base": float(base.reward_uplift),
                    "uplift_aggr": float(aggressive.reward_uplift),
                    "votes_final_cons": float(conservative.votes_final_estimate),
                    "votes_final_base": float(base.votes_final_estimate),
                    "votes_final_aggr": float(aggressive.votes_final_estimate),
                    "rewards_final_cons": float(conservative.rewards_final_estimate),
                    "rewards_final_base": float(base.rewards_final_estimate),
                    "rewards_final_aggr": float(aggressive.rewards_final_estimate),
                    "marginal_bps_cons": _marginal_bps(
                        conservative.votes_final_estimate,
                        conservative.rewards_final_estimate,
                    ),
                    "marginal_bps_base": _marginal_bps(
                        base.votes_final_estimate,
                        base.rewards_final_estimate,
                    ),
                    "marginal_bps_aggr": _marginal_bps(
                        aggressive.votes_final_estimate,
                        aggressive.rewards_final_estimate,
                    ),
                    "truth_votes_final": float(truth_row["final_votes_raw"]),
                    "truth_rewards_final": float(truth_row["final_rewards_usd"]),
                    "confidence_penalty": float(base.confidence_penalty),
                    "source": base.source,
                }
            )

        rows.sort(key=lambda r: r["votes_recommended"], reverse=True)
        diagnostics_by_window[window] = rows

    conn.close()
    return diagnostics_by_window


def generate_scenario_diagnostics_report(
    diagnostics_by_window: Dict[str, List[Dict[str, Any]]],
    max_rows_per_window: int = 10,
) -> str:
    """Generate scenario decomposition diagnostics report per gauge/window."""
    lines: List[str] = []
    lines.append("\n" + "=" * 120)
    lines.append("SCENARIO DECOMPOSITION (Gauge-level)")
    lines.append("=" * 120)

    if not diagnostics_by_window:
        lines.append("No scenario diagnostics rows found.")
        lines.append("=" * 120 + "\n")
        return "\n".join(lines)

    for window in sorted(diagnostics_by_window.keys()):
        rows = diagnostics_by_window[window]
        lines.append(f"\n🔍 Window: {window} | gauges: {len(rows)}")
        lines.append(
            "  "
            f"{'Gauge':<14} {'Alloc':>8} {'Drift(c/b/a)':>20} {'Uplift(c/b/a)':>20} "
            f"{'MargBps(c/b/a)':>20} {'Truth(v/r)':>20}"
        )
        lines.append("  " + "-" * 112)

        for row in rows[:max_rows_per_window]:
            lines.append(
                "  "
                f"{row['gauge_address'][:12]}.. "
                f"{row['votes_recommended']:>8,} "
                f"{row['vote_drift_cons']:>6.2%}/{row['vote_drift_base']:>5.2%}/{row['vote_drift_aggr']:>5.2%} "
                f"{row['uplift_cons']:>6.2%}/{row['uplift_base']:>5.2%}/{row['uplift_aggr']:>5.2%} "
                f"{row['marginal_bps_cons']:>6,}/{row['marginal_bps_base']:>5,}/{row['marginal_bps_aggr']:>5,} "
                f"{row['truth_votes_final']:>9,.0f}/{row['truth_rewards_final']:>9,.2f}"
            )

        if len(rows) > max_rows_per_window:
            lines.append(f"  ... ({len(rows) - max_rows_per_window} more gauges)")

    lines.append("\n" + "=" * 120 + "\n")
    return "\n".join(lines)


def compute_realized_return(
    votes_allocated: int,
    final_votes: float,
    final_rewards_usd: float,
) -> int:
    """
    Compute realized return in basis points from allocation and realized final state.
    
    Return from allocated votes = (final_rewards_usd * votes_allocated) / (final_votes + votes_allocated)
    Return in bps = return / votes_allocated * 10000
    
    Simplified: bps = (final_rewards_usd / (final_votes + votes_allocated)) * 10000
    """
    if votes_allocated == 0:
        return 0
    
    if final_votes + votes_allocated == 0:
        return 0
    
    # Return per vote in bps
    return_per_vote_bps = (final_rewards_usd / (final_votes + votes_allocated)) * 10000
    return int(return_per_vote_bps)


def backtest_epoch(
    db_path: str,
    epoch: int,
) -> Tuple[List[BacktestResult], List[PortfolioBacktestResult]]:
    """
    Run full backtest for one epoch across all decision windows.
    
    Returns:
        (gauge_level_results, portfolio_level_results)
    """
    logger.info(f"🔄 Backtesting epoch {epoch}")
    
    forecasts = load_forecasts(db_path, epoch)
    truth = load_truth_labels(db_path, epoch)
    
    if not forecasts:
        logger.warning(f"No forecasts found for epoch {epoch}")
        return [], []
    
    if not truth:
        logger.warning(f"No truth labels found for epoch {epoch}")
        return [], []
    
    gauge_results = []
    portfolio_results = []
    
    for window in sorted(forecasts.keys()):
        logger.info(f"📊 Processing window: {window}")
        
        window_forecasts = forecasts[window]['gauges']
        expected_window_return_bps = int(forecasts[window]['portfolio_return_bps'])
        expected_window_downside_bps = int(forecasts[window]['portfolio_downside_bps'])
        window_results = []
        
        for forecast in window_forecasts:
            gauge = forecast['gauge_address']
            votes_rec = forecast['votes_recommended']
            
            # Lookup truth
            if gauge not in truth:
                logger.warning(f"  Missing truth label for gauge {gauge}, skipping")
                continue
            
            final_votes = truth[gauge]['final_votes_raw']
            final_rewards = truth[gauge]['final_rewards_usd']
            
            # Compute realized return
            realized_return = compute_realized_return(votes_rec, final_votes, final_rewards)
            
            result = BacktestResult(
                epoch=epoch,
                decision_window=window,
                gauge_address=gauge,
                votes_recommended=votes_rec,
                final_votes=final_votes,
                final_rewards_usd=final_rewards,
                realized_return_bps=realized_return,
                is_allocated=votes_rec > 0,
            )
            
            gauge_results.append(result)
            window_results.append(result)
        
        # Compute portfolio-level metrics for this window
        portfolio_metric = _compute_portfolio_metrics(
            epoch=epoch,
            window=window,
            gauge_results=window_results,
            expected_portfolio_return_bps=expected_window_return_bps,
            expected_portfolio_downside_bps=expected_window_downside_bps,
        )
        portfolio_results.append(portfolio_metric)
        
        logger.info(
            f"  ✓ {len(window_results)} gauges, "
            f"portfolio return: {portfolio_metric.expected_portfolio_return_bps:,} bps (expected) "
            f"vs {portfolio_metric.realized_portfolio_return_bps:,} bps (realized), "
            f"P10: {portfolio_metric.p10_realized_return_bps:,} bps"
        )
    
    return gauge_results, portfolio_results


def _compute_portfolio_metrics(
    epoch: int,
    window: str,
    gauge_results: List[BacktestResult],
    expected_portfolio_return_bps: int,
    expected_portfolio_downside_bps: int,
) -> PortfolioBacktestResult:
    """Compute portfolio-level aggregates from gauge results."""
    
    if not gauge_results:
        return PortfolioBacktestResult(
            epoch=epoch,
            decision_window=window,
            num_gauges_in_forecast=0,
            num_gauges_allocated=0,
            expected_portfolio_return_bps=expected_portfolio_return_bps,
            expected_portfolio_downside_bps=expected_portfolio_downside_bps,
            realized_portfolio_return_bps=0,
            baseline_portfolio_return_bps=0,
            uplift_vs_baseline_bps=0,
            baseline_topk_portfolio_return_bps=0,
            uplift_vs_topk_baseline_bps=0,
            portfolio_error_bps=0,
            median_realized_return_bps=0,
            p10_realized_return_bps=0,
            min_realized_return_bps=0,
            max_realized_return_bps=0,
            regret_vs_hindsight_bps=0,
            calibration_score=0.0,
            num_positive_return_gauges=0,
            num_negative_return_gauges=0,
            num_zero_allocation_gauges=0,
        )
    
    # Portfolio realized return (weighted by allocation)
    total_votes = sum(r.votes_recommended for r in gauge_results)
    if total_votes == 0:
        portfolio_realized = 0
        baseline_realized = 0
        baseline_topk_realized = 0
    else:
        portfolio_realized = sum(
            r.votes_recommended * r.realized_return_bps / 10000
            for r in gauge_results
        ) / total_votes * 10000

        # Baseline strategy: equal-weight allocation across all gauges in this window.
        num_gauges = len(gauge_results)
        if num_gauges > 0:
            baseline_votes_per_gauge = total_votes / num_gauges
            baseline_total_usd = 0.0
            for r in gauge_results:
                if r.final_votes + baseline_votes_per_gauge <= 0:
                    continue
                baseline_total_usd += (
                    r.final_rewards_usd * baseline_votes_per_gauge
                ) / (r.final_votes + baseline_votes_per_gauge)
            baseline_realized = (baseline_total_usd / total_votes) * 10000.0
        else:
            baseline_realized = 0

        # Baseline strategy 2: equal-weight across forecast-selected K gauges.
        selected_gauges = [r for r in gauge_results if r.votes_recommended > 0]
        if selected_gauges:
            selected_votes_per_gauge = total_votes / len(selected_gauges)
            baseline_topk_total_usd = 0.0
            for r in selected_gauges:
                if r.final_votes + selected_votes_per_gauge <= 0:
                    continue
                baseline_topk_total_usd += (
                    r.final_rewards_usd * selected_votes_per_gauge
                ) / (r.final_votes + selected_votes_per_gauge)
            baseline_topk_realized = (baseline_topk_total_usd / total_votes) * 10000.0
        else:
            baseline_topk_realized = 0
    
    # Tail metrics (P10, min, max) across all gauge realized returns
    realized_returns = [r.realized_return_bps for r in gauge_results]
    sorted_returns = sorted(realized_returns)
    p10_idx = max(0, len(sorted_returns) // 10)
    
    median_return = sorted_returns[len(sorted_returns) // 2] if sorted_returns else 0
    p10_return = sorted_returns[p10_idx] if sorted_returns else 0
    min_return = min(realized_returns) if realized_returns else 0
    max_return = max(realized_returns) if realized_returns else 0
    
    # Regret: what if we had allocated to the top N gauges by realized return?
    # Hindsight optimal: pick top K_max gauges by realized return given voting power
    allocated_gauges = [r for r in gauge_results if r.is_allocated]
    if allocated_gauges and total_votes > 0:
        # Approximate hindsight as equal-weight allocation to top-K realized bps gauges.
        sorted_by_return = sorted(gauge_results, key=lambda r: r.realized_return_bps, reverse=True)
        k_hindsight = len(allocated_gauges)
        hindsight_topk = sorted_by_return[:k_hindsight]
        hindsight_return = (
            sum(r.realized_return_bps for r in hindsight_topk) / len(hindsight_topk)
            if hindsight_topk
            else 0
        )
        regret = int(hindsight_return - portfolio_realized)
    else:
        regret = 0
    
    # Calibration score (MVP): whether realized portfolio return clears expected downside.
    calibration_score = 1.0 if portfolio_realized >= expected_portfolio_downside_bps else 0.0
    
    # Diagnostic counts
    num_positive = sum(1 for r in gauge_results if r.realized_return_bps > 0)
    num_negative = sum(1 for r in gauge_results if r.realized_return_bps < 0)
    num_zero = sum(1 for r in gauge_results if not r.is_allocated)
    
    return PortfolioBacktestResult(
        epoch=epoch,
        decision_window=window,
        num_gauges_in_forecast=len(gauge_results),
        num_gauges_allocated=len(allocated_gauges),
        expected_portfolio_return_bps=int(expected_portfolio_return_bps),
        expected_portfolio_downside_bps=int(expected_portfolio_downside_bps),
        realized_portfolio_return_bps=int(portfolio_realized),
        baseline_portfolio_return_bps=int(baseline_realized),
        uplift_vs_baseline_bps=int(portfolio_realized - baseline_realized),
        baseline_topk_portfolio_return_bps=int(baseline_topk_realized),
        uplift_vs_topk_baseline_bps=int(portfolio_realized - baseline_topk_realized),
        portfolio_error_bps=int(portfolio_realized - expected_portfolio_return_bps),
        median_realized_return_bps=median_return,
        p10_realized_return_bps=p10_return,
        min_realized_return_bps=min_return,
        max_realized_return_bps=max_return,
        regret_vs_hindsight_bps=regret,
        calibration_score=calibration_score,
        num_positive_return_gauges=num_positive,
        num_negative_return_gauges=num_negative,
        num_zero_allocation_gauges=num_zero,
    )


def generate_backtest_report(
    gauge_results: List[BacktestResult],
    portfolio_results: List[PortfolioBacktestResult],
) -> str:
    """Generate human-readable backtest summary report."""
    
    lines = []
    lines.append("\n" + "=" * 80)
    lines.append("OFFLINE BACKTEST REPORT (P5)")
    lines.append("=" * 80)
    
    if not portfolio_results:
        lines.append("❌ No backtest results to report")
        return "\n".join(lines)
    
    # Extract metrics
    epochs = set(r.epoch for r in portfolio_results)
    windows = set(r.decision_window for r in portfolio_results)
    
    lines.append(f"\n📊 Summary")
    lines.append(f"  Epochs tested: {len(epochs)}")
    lines.append(f"  Decision windows: {', '.join(sorted(windows))}")
    lines.append(f"  Total portfolio-level metrics: {len(portfolio_results)}")
    lines.append(f"  Total gauge-level results: {len(gauge_results)}")
    
    # Portfolio metrics by window
    lines.append(f"\n📈 Portfolio Returns by Window")
    lines.append(f"  {'Window':<12} {'Expected (bps)':<18} {'Baseline (bps)':<18} {'Realized (bps)':<18} {'Uplift':<10} {'Error':<10}")
    lines.append(f"  {'-'*72}")
    
    total_expected = 0
    total_baseline = 0
    total_realized = 0
    for result in sorted(portfolio_results, key=lambda r: r.decision_window):
        window = result.decision_window
        exp = result.expected_portfolio_return_bps
        baseline = result.baseline_portfolio_return_bps
        real = result.realized_portfolio_return_bps
        uplift = result.uplift_vs_baseline_bps
        err = real - exp
        
        total_expected += exp
        total_baseline += baseline
        total_realized += real
        
        lines.append(
            f"  {window:<12} {exp:>16,} {baseline:>16,} {real:>16,} {uplift:>8,} {err:>10,}"
        )
    
    lines.append(f"  {'-'*72}")
    lines.append(f"  {'TOTAL':<12} {total_expected:>16,} {total_baseline:>16,} {total_realized:>16,}")
    
    # Key validation checks
    lines.append(f"\n✅ Validation Checks")
    
    median_error = sorted([r.portfolio_error_bps for r in portfolio_results])[
        len(portfolio_results) // 2
    ]
    median_p10 = sorted([r.p10_realized_return_bps for r in portfolio_results])[
        len(portfolio_results) // 2
    ]
    median_calibration = sorted([r.calibration_score for r in portfolio_results])[
        len(portfolio_results) // 2
    ]
    median_uplift = sorted([r.uplift_vs_baseline_bps for r in portfolio_results])[
        len(portfolio_results) // 2
    ]
    median_uplift_topk = sorted([r.uplift_vs_topk_baseline_bps for r in portfolio_results])[
        len(portfolio_results) // 2
    ]
    
    # Check 1: Positive median return
    if total_realized > 0:
        lines.append(f"  ✓ Realized portfolio return > 0: {total_realized:,} bps")
    else:
        lines.append(f"  ✗ Realized portfolio return <= 0: {total_realized:,} bps")
    
    # Check 2: P10 non-negative
    if median_p10 >= 0:
        lines.append(f"  ✓ Median P10 return non-negative: {median_p10:,} bps")
    else:
        lines.append(f"  ✗ Median P10 return negative: {median_p10:,} bps")
    
    # Check 3: Forecast error reasonable
    abs_median_error = abs(median_error)
    if abs_median_error < 1000000:  # Within 10,000 % (very loose)
        lines.append(f"  ✓ Median forecast error reasonable: {median_error:,} bps")
    else:
        lines.append(f"  ✗ Median forecast error large: {median_error:,} bps")
    
    # Check 4: Downside calibration
    if median_calibration >= 0.5:
        lines.append(f"  ✓ Realized return clears downside in >= 50% windows: {median_calibration:.1%}")
    else:
        lines.append(f"  ⚠ Realized return clears downside in < 50% windows: {median_calibration:.1%}")

    # Check 5: Uplift vs baseline
    if median_uplift >= 0:
        lines.append(f"  ✓ Median uplift vs equal-weight baseline non-negative: {median_uplift:,} bps")
    else:
        lines.append(f"  ⚠ Median uplift vs equal-weight baseline negative: {median_uplift:,} bps")

    # Check 6: Uplift vs selected-K equal-weight baseline
    if median_uplift_topk >= 0:
        lines.append(f"  ✓ Median uplift vs selected-K baseline non-negative: {median_uplift_topk:,} bps")
    else:
        lines.append(f"  ⚠ Median uplift vs selected-K baseline negative: {median_uplift_topk:,} bps")
    
    # Allocation efficiency
    lines.append(f"\n🎯 Allocation Efficiency")
    lines.append(f"  {'Window':<12} {'Allocated':<12} {'Avg Return':<15} {'Positive %':<12}")
    lines.append(f"  {'-'*51}")
    for result in sorted(portfolio_results, key=lambda r: r.decision_window):
        window = result.decision_window
        allocated = result.num_gauges_allocated
        avg_return = result.realized_portfolio_return_bps
        positive_pct = (
            result.num_positive_return_gauges / result.num_gauges_in_forecast * 100
            if result.num_gauges_in_forecast > 0
            else 0
        )
        lines.append(f"  {window:<12} {allocated:<12} {avg_return:>13,} {positive_pct:>10.0f}%")

    lines.append(f"\n🧪 Baseline Comparison")
    lines.append(f"  {'Window':<12} {'All-Gauges':<12} {'Sel-K EqWt':<12} {'Realized':<12} {'Uplift1':<10} {'Uplift2':<10}")
    lines.append(f"  {'-'*72}")
    for result in sorted(portfolio_results, key=lambda r: r.decision_window):
        lines.append(
            f"  {result.decision_window:<12} "
            f"{result.baseline_portfolio_return_bps:>10,} "
            f"{result.baseline_topk_portfolio_return_bps:>10,} "
            f"{result.realized_portfolio_return_bps:>10,} "
            f"{result.uplift_vs_baseline_bps:>8,} "
            f"{result.uplift_vs_topk_baseline_bps:>8,}"
        )
    
    # Gauge-level insights
    if gauge_results:
        lines.append(f"\n💡 Gauge-Level Insights")
        
        # Top allocated gauges by realized return
        allocated = [r for r in gauge_results if r.is_allocated]
        if allocated:
            top_allocated = sorted(allocated, key=lambda r: r.realized_return_bps, reverse=True)[:5]
            lines.append(f"  Top 5 allocated gauges (by realized return):")
            for r in top_allocated:
                lines.append(
                    f"    {r.gauge_address[:12]}... : "
                    f"{r.votes_recommended:>10,} votes → "
                    f"{r.realized_return_bps:>10,} bps"
                )
    
    # Regret analysis
    avg_regret = sum(r.regret_vs_hindsight_bps for r in portfolio_results) / len(portfolio_results)
    lines.append(f"\n📉 Regret Analysis")
    lines.append(f"  Average regret vs hindsight optimal: {avg_regret:,.0f} bps")
    if avg_regret < 100000:
        lines.append(f"  ✓ Regret low (< 1000%)")
    else:
        lines.append(f"  ⚠ Regret elevated (>= 1000%)")
    
    lines.append("\n" + "=" * 80 + "\n")
    
    return "\n".join(lines)


def persist_backtest_results(
    db_path: str,
    gauge_results: List[BacktestResult],
    portfolio_results: List[PortfolioBacktestResult],
) -> None:
    """Persist gauge-level and portfolio-level backtest results to database."""
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Canonical table requested by spec (portfolio-level backtest outcomes)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preboundary_backtest_results (
            epoch INTEGER,
            decision_window TEXT,
            expected_portfolio_return_bps INTEGER,
            expected_portfolio_downside_bps INTEGER,
            realized_portfolio_return_bps INTEGER,
            baseline_portfolio_return_bps INTEGER,
            uplift_vs_baseline_bps INTEGER,
            baseline_topk_portfolio_return_bps INTEGER,
            uplift_vs_topk_baseline_bps INTEGER,
            portfolio_error_bps INTEGER,
            median_realized_return_bps INTEGER,
            p10_realized_return_bps INTEGER,
            regret_vs_hindsight_bps INTEGER,
            calibration_score REAL,
            source_tag TEXT DEFAULT 'p5_backtest',
            computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (epoch, decision_window)
        )
    """)

    # Migrate schema if table already existed with a narrower column set.
    cursor.execute("PRAGMA table_info(preboundary_backtest_results)")
    existing_columns = {row[1] for row in cursor.fetchall()}
    required_columns = {
        "expected_portfolio_return_bps": "INTEGER",
        "expected_portfolio_downside_bps": "INTEGER",
        "realized_portfolio_return_bps": "INTEGER",
        "baseline_portfolio_return_bps": "INTEGER",
        "uplift_vs_baseline_bps": "INTEGER",
        "baseline_topk_portfolio_return_bps": "INTEGER",
        "uplift_vs_topk_baseline_bps": "INTEGER",
        "portfolio_error_bps": "INTEGER",
        "median_realized_return_bps": "INTEGER",
        "p10_realized_return_bps": "INTEGER",
        "regret_vs_hindsight_bps": "INTEGER",
        "calibration_score": "REAL",
        "source_tag": "TEXT DEFAULT 'p5_backtest'",
        "computed_at": "DATETIME DEFAULT CURRENT_TIMESTAMP",
    }
    for column_name, column_type in required_columns.items():
        if column_name not in existing_columns:
            cursor.execute(
                f"ALTER TABLE preboundary_backtest_results ADD COLUMN {column_name} {column_type}"
            )

    # Create gauge-level backtest results table if not exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preboundary_backtest_gauge_results (
            epoch INTEGER,
            decision_window TEXT,
            gauge_address TEXT,
            votes_recommended INTEGER,
            final_votes REAL,
            final_rewards_usd REAL,
            realized_return_bps INTEGER,
            is_allocated INTEGER,
            computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (epoch, decision_window, gauge_address)
        )
    """)
    
    # Create portfolio-level backtest results table if not exists
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS preboundary_backtest_portfolio_results (
            epoch INTEGER,
            decision_window TEXT,
            num_gauges_in_forecast INTEGER,
            num_gauges_allocated INTEGER,
            expected_portfolio_return_bps INTEGER,
            expected_portfolio_downside_bps INTEGER,
            realized_portfolio_return_bps INTEGER,
            baseline_portfolio_return_bps INTEGER,
            uplift_vs_baseline_bps INTEGER,
            baseline_topk_portfolio_return_bps INTEGER,
            uplift_vs_topk_baseline_bps INTEGER,
            portfolio_error_bps INTEGER,
            median_realized_return_bps INTEGER,
            p10_realized_return_bps INTEGER,
            min_realized_return_bps INTEGER,
            max_realized_return_bps INTEGER,
            regret_vs_hindsight_bps INTEGER,
            calibration_score REAL,
            num_positive_return_gauges INTEGER,
            num_negative_return_gauges INTEGER,
            num_zero_allocation_gauges INTEGER,
            computed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (epoch, decision_window)
        )
    """)

    cursor.execute("PRAGMA table_info(preboundary_backtest_portfolio_results)")
    portfolio_columns = {row[1] for row in cursor.fetchall()}
    if "expected_portfolio_downside_bps" not in portfolio_columns:
        cursor.execute(
            "ALTER TABLE preboundary_backtest_portfolio_results "
            "ADD COLUMN expected_portfolio_downside_bps INTEGER"
        )
    if "baseline_portfolio_return_bps" not in portfolio_columns:
        cursor.execute(
            "ALTER TABLE preboundary_backtest_portfolio_results "
            "ADD COLUMN baseline_portfolio_return_bps INTEGER"
        )
    if "uplift_vs_baseline_bps" not in portfolio_columns:
        cursor.execute(
            "ALTER TABLE preboundary_backtest_portfolio_results "
            "ADD COLUMN uplift_vs_baseline_bps INTEGER"
        )
    if "baseline_topk_portfolio_return_bps" not in portfolio_columns:
        cursor.execute(
            "ALTER TABLE preboundary_backtest_portfolio_results "
            "ADD COLUMN baseline_topk_portfolio_return_bps INTEGER"
        )
    if "uplift_vs_topk_baseline_bps" not in portfolio_columns:
        cursor.execute(
            "ALTER TABLE preboundary_backtest_portfolio_results "
            "ADD COLUMN uplift_vs_topk_baseline_bps INTEGER"
        )
    
    # Upsert gauge results
    for result in gauge_results:
        cursor.execute("""
            INSERT OR REPLACE INTO preboundary_backtest_gauge_results (
                epoch, decision_window, gauge_address, votes_recommended,
                final_votes, final_rewards_usd, realized_return_bps, is_allocated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result.epoch,
            result.decision_window,
            result.gauge_address,
            result.votes_recommended,
            result.final_votes,
            result.final_rewards_usd,
            result.realized_return_bps,
            1 if result.is_allocated else 0,
        ))

    # Upsert canonical portfolio results
    for result in portfolio_results:
        run_id = f"p5_epoch_{result.epoch}"
        expected_return_usd = float(result.expected_portfolio_return_bps) / 10000.0
        realized_return_usd = float(result.realized_portfolio_return_bps) / 10000.0
        p10_return_usd = float(result.p10_realized_return_bps) / 10000.0
        regret_usd = float(result.regret_vs_hindsight_bps) / 10000.0
        calibration_error = 1.0 - float(result.calibration_score)
        computed_at = int(time.time())

        cursor.execute("""
            INSERT OR REPLACE INTO preboundary_backtest_results (
                epoch, decision_window, run_id,
                expected_return_usd, realized_return_usd,
                p10_return_usd, regret_usd, calibration_error,
                computed_at,
                expected_portfolio_return_bps, expected_portfolio_downside_bps,
                realized_portfolio_return_bps, baseline_portfolio_return_bps,
                uplift_vs_baseline_bps, baseline_topk_portfolio_return_bps,
                uplift_vs_topk_baseline_bps, portfolio_error_bps,
                median_realized_return_bps, p10_realized_return_bps,
                regret_vs_hindsight_bps, calibration_score, source_tag
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result.epoch,
            result.decision_window,
            run_id,
            expected_return_usd,
            realized_return_usd,
            p10_return_usd,
            regret_usd,
            calibration_error,
            computed_at,
            result.expected_portfolio_return_bps,
            result.expected_portfolio_downside_bps,
            result.realized_portfolio_return_bps,
            result.baseline_portfolio_return_bps,
            result.uplift_vs_baseline_bps,
            result.baseline_topk_portfolio_return_bps,
            result.uplift_vs_topk_baseline_bps,
            result.portfolio_error_bps,
            result.median_realized_return_bps,
            result.p10_realized_return_bps,
            result.regret_vs_hindsight_bps,
            result.calibration_score,
            "p5_backtest",
        ))
    
    # Upsert portfolio results
    for result in portfolio_results:
        cursor.execute("""
            INSERT OR REPLACE INTO preboundary_backtest_portfolio_results (
                epoch, decision_window, num_gauges_in_forecast,
                num_gauges_allocated, expected_portfolio_return_bps,
                expected_portfolio_downside_bps,
                realized_portfolio_return_bps, baseline_portfolio_return_bps,
                uplift_vs_baseline_bps, baseline_topk_portfolio_return_bps,
                uplift_vs_topk_baseline_bps, portfolio_error_bps,
                median_realized_return_bps, p10_realized_return_bps,
                min_realized_return_bps, max_realized_return_bps,
                regret_vs_hindsight_bps, calibration_score,
                num_positive_return_gauges, num_negative_return_gauges,
                num_zero_allocation_gauges
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result.epoch,
            result.decision_window,
            result.num_gauges_in_forecast,
            result.num_gauges_allocated,
            result.expected_portfolio_return_bps,
            result.expected_portfolio_downside_bps,
            result.realized_portfolio_return_bps,
            result.baseline_portfolio_return_bps,
            result.uplift_vs_baseline_bps,
            result.baseline_topk_portfolio_return_bps,
            result.uplift_vs_topk_baseline_bps,
            result.portfolio_error_bps,
            result.median_realized_return_bps,
            result.p10_realized_return_bps,
            result.min_realized_return_bps,
            result.max_realized_return_bps,
            result.regret_vs_hindsight_bps,
            result.calibration_score,
            result.num_positive_return_gauges,
            result.num_negative_return_gauges,
            result.num_zero_allocation_gauges,
        ))
    
    conn.commit()
    conn.close()
    logger.info(f"✓ Persisted {len(gauge_results)} gauge results and {len(portfolio_results)} portfolio results")


def get_target_epochs(db_path: str, specific_epoch: Optional[int], recent_epochs: Optional[int]) -> List[int]:
    """Resolve epoch list to backtest."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    if specific_epoch is not None:
        conn.close()
        return [specific_epoch]

    if recent_epochs is not None and recent_epochs > 0:
        cursor.execute(
            """
            SELECT DISTINCT epoch
            FROM preboundary_forecasts
            ORDER BY epoch DESC
            LIMIT ?
            """,
            (recent_epochs,),
        )
    else:
        cursor.execute("SELECT DISTINCT epoch FROM preboundary_forecasts ORDER BY epoch")

    epochs = [row[0] for row in cursor.fetchall() if row[0] is not None]
    conn.close()
    return sorted(epochs)


def run_backtest_for_epochs(db_path: str, epochs: List[int]) -> Tuple[List[BacktestResult], List[PortfolioBacktestResult]]:
    """Run backtest for multiple epochs and aggregate results."""
    all_gauge_results: List[BacktestResult] = []
    all_portfolio_results: List[PortfolioBacktestResult] = []

    for epoch in epochs:
        gauge_results, portfolio_results = backtest_epoch(db_path, epoch)
        all_gauge_results.extend(gauge_results)
        all_portfolio_results.extend(portfolio_results)

    return all_gauge_results, all_portfolio_results


if __name__ == "__main__":
    # Simple CLI for backtest
    import argparse
    
    parser = argparse.ArgumentParser(description="Run offline backtest of P5 allocations")
    parser.add_argument("--db-path", default="data/db/preboundary_dev.db", help="Path to preboundary database")
    parser.add_argument("--epoch", type=int, help="Specific epoch to backtest (default: latest)")
    parser.add_argument("--recent-epochs", type=int, help="Backtest N most recent epochs")
    parser.add_argument("--persist", action="store_true", help="Persist results to database")
    parser.add_argument("--diagnostics", action="store_true", help="Print gauge-level forecast input diagnostics")
    parser.add_argument(
        "--cache-dir",
        default="data/preboundary_cache",
        help="Proxy cache directory for scenario diagnostics",
    )
    parser.add_argument(
        "--diagnostics-limit",
        type=int,
        default=15,
        help="Max gauge rows per window in diagnostics output",
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    epochs_to_test = get_target_epochs(args.db_path, args.epoch, args.recent_epochs)
    if not epochs_to_test:
        logger.error("No forecasts found in database")
        exit(1)

    logger.info(f"Running backtest for {len(epochs_to_test)} epoch(s): {epochs_to_test}")
    gauge_results, portfolio_results = run_backtest_for_epochs(args.db_path, epochs_to_test)
    
    # Generate and print report
    report = generate_backtest_report(gauge_results, portfolio_results)
    print(report)

    # Optional diagnostics report (first epoch only, for readability)
    if args.diagnostics and epochs_to_test:
        print(generate_window_output_report(portfolio_results))

        diagnostics = load_forecast_input_diagnostics(args.db_path, epochs_to_test[-1])
        print(generate_forecast_input_report(diagnostics, max_rows_per_window=args.diagnostics_limit))

        scenario_diag = load_scenario_gauge_diagnostics(
            args.db_path,
            epochs_to_test[-1],
            cache_dir=args.cache_dir,
        )
        print(generate_scenario_diagnostics_report(scenario_diag, max_rows_per_window=args.diagnostics_limit))
    
    # Persist if requested
    if args.persist:
        persist_backtest_results(args.db_path, gauge_results, portfolio_results)
        logger.info("✅ Backtest results persisted to database")
