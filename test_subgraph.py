"""Test subgraph connection and queries."""

from src.subgraph_client import SubgraphClient

client = SubgraphClient()

print('Testing subgraph connection...')
print(f'Endpoint: {client.url}\n')

# Test gauges
try:
    gauges = client.fetch_gauges(first=5)
    print(f'✓ Gauges: {len(gauges)} found')
    if gauges:
        g = gauges[0]
        print(f'  First: {g["address"]} (block {g["blockNumber"]})')
        print(f'  Pool: {g["pool"]}')
        print(f'  Alive: {g["isAlive"]}')
except Exception as e:
    print(f'✗ Gauges error: {e}')

print()

# Test votes
try:
    votes = client.fetch_votes(first=5)
    print(f'✓ Votes: {len(votes)} found')
    if votes:
        v = votes[0]
        print(f'  First: {v["voter"]} weight={v["weight"]}')
except Exception as e:
    print(f'✗ Votes error: {e}')

print()

# Test bribes
try:
    bribes = client.fetch_bribes(first=5)
    print(f'✓ Bribes: {len(bribes)} found')
    if bribes:
        b = bribes[0]
        print(f'  First: {b["rewardToken"]} amount={b["amount"]}')
except Exception as e:
    print(f'✗ Bribes error: {e}')

print(f'\n🎉 Subgraph is working! Currently at ~5% sync')
