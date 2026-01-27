"""
Check for RewardAdded events on bribe contracts to understand fee/bribe distribution.
"""

from web3 import Web3
from config import Config
from src.database import Database
import time

# Initialize
w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))
db = Database("data.db")

print("Checking for BribeV2.RewardAdded events on bribe contracts...")
print("=" * 80)

# RewardAdded event signature: RewardAdded(address indexed rewardToken, uint256 amount, uint256 startTimestamp)
reward_added_topic = w3.keccak(text="RewardAdded(address,uint256,uint256)").hex()
print(f"\nRewardAdded topic: {reward_added_topic}")

# Get some gauges from database
gauges = db.get_all_gauges()[:5]  # Check first 5 gauges

print(f"\nChecking first {len(gauges)} gauges...")

for gauge in gauges:
    print(f"\n{'='*80}")
    print(f"Gauge: {gauge.address}")
    print(f"Pool: {gauge.pool}")
    print(f"Internal Bribe: {gauge.internal_bribe}")
    print(f"External Bribe: {gauge.external_bribe}")
    
    # Check both internal and external bribes
    for bribe_type, bribe_addr in [
        ("Internal", gauge.internal_bribe),
        ("External", gauge.external_bribe)
    ]:
        print(f"\n  {bribe_type} Bribe Contract: {bribe_addr}")
        
        # Verify contract exists
        code = w3.eth.get_code(Web3.to_checksum_address(bribe_addr))
        if code == b'' or code == b'0x':
            print(f"    ❌ No contract code found")
            continue
        print(f"    ✓ Contract exists ({len(code)} bytes)")
        
        # Check for RewardAdded events in recent blocks
        try:
            current_block = w3.eth.block_number
            # Check last 1000 blocks in chunks
            from_block = max(35273810, current_block - 10000)
            
            total_events = 0
            for start in range(from_block, current_block, 1000):
                end = min(start + 999, current_block)
                
                filter_params = {
                    'fromBlock': start,
                    'toBlock': end,
                    'address': Web3.to_checksum_address(bribe_addr),
                    'topics': [reward_added_topic]
                }
                
                logs = w3.eth.get_logs(filter_params)
                total_events += len(logs)
                
                if logs:
                    for log in logs[:2]:  # Show first 2 per chunk
                        # Decode: rewardToken (indexed), amount, startTimestamp
                        reward_token = '0x' + log['topics'][1].hex()[26:]  # Remove padding
                        amount = int(log['data'][:66], 16)
                        start_timestamp = int('0x' + log['data'][66:130], 16)
                        
                        # Calculate epoch
                        epoch = (start_timestamp // 604800) * 604800
                        
                        print(f"      Block {log['blockNumber']}:")
                        print(f"        Reward Token: {reward_token}")
                        print(f"        Amount: {amount / 1e18:.4f}")
                        print(f"        Epoch: {epoch} ({time.strftime('%Y-%m-%d', time.gmtime(epoch))})")
                
                time.sleep(0.1)  # Rate limit
            
            print(f"    Searched blocks {from_block} to {current_block}")
            print(f"    Found {total_events} RewardAdded events total")
            
        except Exception as e:
            print(f"    ❌ Error checking events: {e}")

print(f"\n{'='*80}")
print("\nSummary:")
print("If you see RewardAdded events above, your subgraph should track them!")
print("If 0 events found, it means either:")
print("  1. No fees have been distributed yet (distributeFees() not called)")
print("  2. No external bribes deposited")
print("  3. Looking in wrong block range")
