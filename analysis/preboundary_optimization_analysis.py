"""
Pre-Boundary Optimization Analysis

Analyzes whether we can predict optimal pool selection using pre-boundary data
(1 block and 20 blocks before boundary) versus actual boundary values.

For each epoch:
1. Calculate optimal 5-pool allocation at actual boundary
2. Calculate optimal 5-pool allocation at 1-block-before and 20-blocks-before
3. Compare predicted returns vs actual returns
4. Measure prediction accuracy
"""

import sqlite3
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple, Optional
from pathlib import Path
import logging
from dataclasses import dataclass

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    """Results of an optimization run"""
    epoch: int
    blocks_before: int  # 0 = actual boundary, 1, 20
    selected_gauges: List[str]
    allocation: Dict[str, float]  # gauge -> vote allocation
    expected_return_usd: float
    roi_percent: float


class PreBoundaryAnalyzer:
    """Analyzes pre-boundary optimization strategies"""
    
    def __init__(self, db_path: str, voting_power: float = 1183272):
        self.db_path = db_path
        self.voting_power = voting_power
        self.conn = sqlite3.connect(db_path)
        
    def __del__(self):
        if hasattr(self, 'conn'):
            self.conn.close()
    
    def get_epochs(self) -> List[int]:
        """Get list of epochs with complete data"""
        query = """
        SELECT DISTINCT epoch 
        FROM boundary_gauge_values 
        WHERE epoch >= 1758153600
        ORDER BY epoch
        """
        df = pd.read_sql_query(query, self.conn)
        return df['epoch'].tolist()
    
    def get_boundary_data(self, epoch: int) -> pd.DataFrame:
        """Get actual boundary data (votes and rewards) for an epoch"""
        query = """
        SELECT 
            bgv.gauge_address,
            bgv.pool_address,
            CAST(bgv.votes_raw AS REAL) as votes,
            COALESCE(SUM(b.usd_value), 0) as total_rewards_usd
        FROM boundary_gauge_values bgv
        LEFT JOIN bribes b ON b.gauge_address = bgv.gauge_address 
            AND b.epoch = bgv.epoch
        WHERE bgv.epoch = ?
        GROUP BY bgv.gauge_address, bgv.pool_address, bgv.votes_raw
        """
        df = pd.read_sql_query(query, self.conn, params=(epoch,))
        return df
    
    def get_preboundary_data(self, epoch: int, blocks_before: int) -> pd.DataFrame:
        """Get pre-boundary snapshot data"""
        # Get boundary block for this epoch
        boundary_query = "SELECT boundary_block FROM epoch_boundaries WHERE epoch = ?"
        boundary_block = pd.read_sql_query(boundary_query, self.conn, params=(epoch,)).iloc[0]['boundary_block']
        query_block = boundary_block - blocks_before
        
        # Get vote data
        vote_query = """
        SELECT 
            gauge_address,
            CAST(votes_raw AS REAL) as votes
        FROM boundary_vote_samples
        WHERE epoch = ? AND blocks_before_boundary = ?
        """
        votes_df = pd.read_sql_query(vote_query, self.conn, params=(epoch, blocks_before))
        
        # Get reward data - aggregate by gauge
        reward_query = """
        SELECT 
            gauge_address,
            SUM(CASE 
                WHEN usd_price IS NOT NULL AND usd_price > 0 
                THEN (CAST(rewards_raw AS REAL) / POWER(10, token_decimals)) * usd_price
                ELSE 0 
            END) as total_rewards_usd
        FROM boundary_reward_samples
        WHERE epoch = ? AND blocks_before_boundary = ?
        GROUP BY gauge_address
        """
        rewards_df = pd.read_sql_query(reward_query, self.conn, params=(epoch, blocks_before))
        
        # Merge votes and rewards
        df = votes_df.merge(rewards_df, on='gauge_address', how='outer')
        df['votes'] = df['votes'].fillna(0)
        df['total_rewards_usd'] = df['total_rewards_usd'].fillna(0)
        
        return df
    
    def optimize_allocation(
        self, 
        df: pd.DataFrame, 
        n_pools: int = 5,
        min_votes_filter: float = 0
    ) -> Tuple[List[str], Dict[str, float], float]:
        """
        Optimize vote allocation across top pools.
        
        Strategy: Equal allocation to top 5 pools by ROI (rewards/votes ratio)
        
        Returns:
            - List of selected gauge addresses
            - Dict of gauge -> vote allocation (absolute votes)
            - Expected total return in USD
        """
        # Filter out pools with no votes (can't calculate ROI)
        df = df[df['votes'] > min_votes_filter].copy()
        
        if len(df) == 0:
            return [], {}, 0.0
        
        # Calculate ROI for each pool
        df['roi'] = df['total_rewards_usd'] / df['votes']
        df['roi'] = df['roi'].replace([np.inf, -np.inf], 0)
        
        # Select top N pools by ROI
        top_pools = df.nlargest(n_pools, 'roi')
        
        if len(top_pools) == 0:
            return [], {}, 0.0
        
        # Equal allocation strategy
        votes_per_pool = self.voting_power / len(top_pools)
        
        allocation = {}
        total_return = 0.0
        
        for _, row in top_pools.iterrows():
            gauge = row['gauge_address']
            allocation[gauge] = votes_per_pool
            
            # Calculate expected return:
            # Our votes / (existing votes + our votes) * total rewards
            existing_votes = row['votes']
            our_share = votes_per_pool / (existing_votes + votes_per_pool)
            pool_return = our_share * row['total_rewards_usd']
            total_return += pool_return
        
        selected_gauges = top_pools['gauge_address'].tolist()
        
        return selected_gauges, allocation, total_return
    
    def simulate_strategy_on_actual(
        self,
        epoch: int,
        predicted_gauges: List[str],
        predicted_allocation: Dict[str, float]
    ) -> float:
        """
        Simulate what returns would have been if we used predicted strategy
        but applied it to actual boundary data
        """
        actual_df = self.get_boundary_data(epoch)
        
        total_return = 0.0
        for gauge, votes_allocated in predicted_allocation.items():
            gauge_data = actual_df[actual_df['gauge_address'] == gauge]
            
            if len(gauge_data) == 0:
                # Gauge didn't exist at boundary (rare)
                continue
            
            actual_votes = gauge_data.iloc[0]['votes']
            actual_rewards = gauge_data.iloc[0]['total_rewards_usd']
            
            # Calculate our share with actual boundary values
            our_share = votes_allocated / (actual_votes + votes_allocated)
            pool_return = our_share * actual_rewards
            total_return += pool_return
        
        return total_return
    
    def analyze_epoch(self, epoch: int) -> Dict:
        """Perform complete analysis for one epoch"""
        logger.info(f"Analyzing epoch {epoch}")
        
        results = {}
        
        # Get actual boundary optimization
        actual_df = self.get_boundary_data(epoch)
        actual_gauges, actual_alloc, actual_return = self.optimize_allocation(actual_df)
        
        results['actual_boundary'] = OptimizationResult(
            epoch=epoch,
            blocks_before=0,
            selected_gauges=actual_gauges,
            allocation=actual_alloc,
            expected_return_usd=actual_return,
            roi_percent=(actual_return / self.voting_power * 100) if self.voting_power > 0 else 0
        )
        
        # Analyze pre-boundary predictions
        for blocks_before in [1, 20]:
            try:
                pre_df = self.get_preboundary_data(epoch, blocks_before)
                pred_gauges, pred_alloc, pred_return = self.optimize_allocation(pre_df)
                
                # Simulate what actual return would have been with this prediction
                actual_return_from_pred = self.simulate_strategy_on_actual(
                    epoch, pred_gauges, pred_alloc
                )
                
                results[f'{blocks_before}_blocks_before'] = {
                    'predicted': OptimizationResult(
                        epoch=epoch,
                        blocks_before=blocks_before,
                        selected_gauges=pred_gauges,
                        allocation=pred_alloc,
                        expected_return_usd=pred_return,
                        roi_percent=(pred_return / self.voting_power * 100) if self.voting_power > 0 else 0
                    ),
                    'actual_return_usd': actual_return_from_pred,
                    'actual_roi_percent': (actual_return_from_pred / self.voting_power * 100) if self.voting_power > 0 else 0
                }
                
            except Exception as e:
                logger.warning(f"Could not analyze {blocks_before} blocks before for epoch {epoch}: {e}")
                results[f'{blocks_before}_blocks_before'] = None
        
        return results
    
    def analyze_all_epochs(self) -> pd.DataFrame:
        """Run analysis across all epochs and compile results"""
        epochs = self.get_epochs()
        logger.info(f"Analyzing {len(epochs)} epochs")
        
        all_results = []
        
        for epoch in epochs:
            try:
                epoch_results = self.analyze_epoch(epoch)
                
                # Extract summary metrics
                actual = epoch_results['actual_boundary']
                
                row = {
                    'epoch': epoch,
                    'actual_return_usd': actual.expected_return_usd,
                    'actual_roi_pct': actual.roi_percent,
                    'actual_pools': ','.join(actual.selected_gauges[:5])
                }
                
                for blocks in [1, 20]:
                    key = f'{blocks}_blocks_before'
                    if epoch_results.get(key):
                        pred_data = epoch_results[key]
                        pred = pred_data['predicted']
                        actual_from_pred = pred_data['actual_return_usd']
                        
                        # Calculate gaps
                        return_gap_usd = actual.expected_return_usd - actual_from_pred
                        return_gap_pct = (return_gap_usd / actual.expected_return_usd * 100) if actual.expected_return_usd > 0 else 0
                        
                        # Pool selection overlap
                        overlap = len(set(actual.selected_gauges) & set(pred.selected_gauges))
                        
                        row[f'pred_{blocks}b_return_usd'] = pred.expected_return_usd
                        row[f'actual_from_pred_{blocks}b_usd'] = actual_from_pred
                        row[f'gap_{blocks}b_usd'] = return_gap_usd
                        row[f'gap_{blocks}b_pct'] = return_gap_pct
                        row[f'pool_overlap_{blocks}b'] = overlap
                        row[f'pred_{blocks}b_pools'] = ','.join(pred.selected_gauges[:5])
                    else:
                        row[f'pred_{blocks}b_return_usd'] = None
                        row[f'actual_from_pred_{blocks}b_usd'] = None
                        row[f'gap_{blocks}b_usd'] = None
                        row[f'gap_{blocks}b_pct'] = None
                        row[f'pool_overlap_{blocks}b'] = None
                        row[f'pred_{blocks}b_pools'] = None
                
                all_results.append(row)
                
            except Exception as e:
                logger.error(f"Error analyzing epoch {epoch}: {e}")
                continue
        
        df = pd.DataFrame(all_results)
        return df


def main():
    """Main analysis entry point"""
    db_path = Path(__file__).parent.parent / 'data' / 'db' / 'data.db'
    
    if not db_path.exists():
        logger.error(f"Database not found at {db_path}")
        return
    
    analyzer = PreBoundaryAnalyzer(str(db_path))
    
    logger.info("Starting pre-boundary optimization analysis")
    results_df = analyzer.analyze_all_epochs()
    
    # Save results
    output_path = Path(__file__).parent.parent / 'analysis' / 'preboundary_optimization_results.csv'
    results_df.to_csv(output_path, index=False)
    logger.info(f"Results saved to {output_path}")
    
    # Print summary statistics
    print("\n" + "="*80)
    print("PRE-BOUNDARY OPTIMIZATION ANALYSIS SUMMARY")
    print("="*80)
    
    print(f"\nEpochs analyzed: {len(results_df)}")
    print(f"Voting power: {analyzer.voting_power:,.0f}")
    
    for blocks in [1, 20]:
        valid_data = results_df[results_df[f'gap_{blocks}b_pct'].notna()]
        
        if len(valid_data) > 0:
            print(f"\n--- {blocks} Blocks Before Boundary ---")
            print(f"Mean return gap: ${valid_data[f'gap_{blocks}b_usd'].mean():,.2f} ({valid_data[f'gap_{blocks}b_pct'].mean():.2f}%)")
            print(f"Median return gap: ${valid_data[f'gap_{blocks}b_usd'].median():,.2f} ({valid_data[f'gap_{blocks}b_pct'].median():.2f}%)")
            print(f"Mean pool overlap: {valid_data[f'pool_overlap_{blocks}b'].mean():.1f} / 5 pools")
            print(f"Perfect predictions (overlap=5): {(valid_data[f'pool_overlap_{blocks}b'] == 5).sum()} / {len(valid_data)}")
    
    print("\n" + "="*80)
    print(f"\nDetailed results: {output_path}")


if __name__ == '__main__':
    main()
