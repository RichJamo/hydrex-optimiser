"""Check subgraph sync status."""

from src.subgraph_client import SubgraphClient
from config import Config
import requests

# Update to use v0.0.2
client = SubgraphClient("https://api.goldsky.com/api/public/project_cmkc98hfcxxz001rghb23dyed/subgraphs/hydrex-dummy/v0.0.2/gn")

# Query metadata to see sync status
query = """
{
  _meta {
    block {
      number
      hash
    }
    hasIndexingErrors
  }
}
"""

try:
    result = client.query(query)
    meta = result.get("_meta", {})
    block_info = meta.get("block", {})
    
    print(f"Subgraph v0.0.2 sync status:")
    print(f"  Latest indexed block: {block_info.get('number', 'unknown')}")
    print(f"  Has errors: {meta.get('hasIndexingErrors', 'unknown')}")
    print(f"\nVoterV5 deployed at block: 35,273,810")
    
    # Get current chain block
    import requests
    from web3 import Web3
    w3 = Web3(Web3.HTTPProvider(Config.RPC_URL))
    current_block = w3.eth.block_number
    print(f"Current chain block: {current_block:,}")
    
    if block_info.get('number'):
        synced_block = int(block_info['number'])
        start_block = 35273810
        
        if synced_block >= start_block:
            print(f"✓ Subgraph has passed VoterV5 deployment block!")
            blocks_synced = synced_block - start_block
            total_blocks = current_block - start_block
            percent = (blocks_synced / total_blocks) * 100
            print(f"  Progress: {blocks_synced:,} / {total_blocks:,} blocks ({percent:.1f}%)")
        else:
            print(f"⏳ Subgraph hasn't reached VoterV5 yet ({synced_block:,} / {start_block:,})")
except Exception as e:
    print(f"Error checking metadata: {e}")
