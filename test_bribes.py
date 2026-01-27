"""Test if subgraph has bribe data."""

from src.subgraph_client import SubgraphClient

client = SubgraphClient()

# Test if bribes exist at all in subgraph
query = """
{
  bribes(first: 5, orderBy: blockTimestamp, orderDirection: desc) {
    id
    bribeContract
    rewardToken
    amount
    from
    blockNumber
    blockTimestamp
  }
}
"""

try:
    result = client.query(query)
    bribes = result.get("bribes", [])
    print(f"Total bribes in subgraph (any block): {len(bribes)}")
    if bribes:
        print("\nFirst bribe:")
        for key, value in bribes[0].items():
            print(f"  {key}: {value}")
    else:
        print("\n⚠️  No bribes found in subgraph at all!")
        print("\nThis likely means:")
        print("1. Bribe contract templates not set up in subgraph")
        print("2. NotifyReward events not being indexed") 
        print("3. Need to add bribe tracking as shown in SUBGRAPH_SETUP.md")
        print("\nThe subgraph needs the 'templates' section to dynamically")
        print("track bribe contracts as they're created from gauges.")
except Exception as e:
    print(f"Error: {e}")
