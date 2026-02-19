#!/usr/bin/env python3
"""
Test the new analytics subgraph and explore its schema
"""

import sys
sys.path.insert(0, '/Users/richardjamieson/Documents/GitHub/hydrex-optimiser')

from src.subgraph_client import SubgraphClient

client = SubgraphClient(subgraph_url="https://api.goldsky.com/api/public/project_cmafph25ltm5g01yv3vr7bsoe/subgraphs/classic/v0.0.2/gn")

print("Testing connection with introspection query...")
print("=" * 80)

# Try to fetch schema info
introspection_query = """
{
  __schema {
    types {
      name
      kind
    }
  }
}
"""

try:
    result = client.query(introspection_query)
    types = result.get('__schema', {}).get('types', [])
    
    # Filter to just entity types (not built-ins)
    entity_types = [t['name'] for t in types if not t['name'].startswith('_') and t['kind'] == 'OBJECT' and t['name'][0].isupper()]
    
    print(f"✓ Connection successful!")
    print(f"\nFound {len(entity_types)} entity types:")
    for entity in sorted(entity_types):
        print(f"  - {entity}")
        
except Exception as e:
    print(f"❌ Error: {e}")
    sys.exit(1)

# Try fetching gauges with the old query structure
print("\n" + "=" * 80)
print("Testing gauge query (old schema)...")
print("=" * 80)

try:
    gauges = client.fetch_gauges(first=5)
    print(f"✓ Got {len(gauges)} gauges")
    if gauges:
        print(f"\nSample gauge:")
        for key, value in gauges[0].items():
            print(f"  {key}: {value}")
except Exception as e:
    print(f"❌ Error: {e}")
    print("\nSchema might be different. Let's try alternative queries...")
    
    # Try alternative query structures
    alternatives = [
        "gauges",
        "Gauge", 
        "gauge",
        "GaugeCreated",
        "gaugeCreateds"
    ]
    
    for entity_name in alternatives:
        print(f"\nTrying '{entity_name}'...", end=' ')
        query = f"""
        {{
          {entity_name}(first: 1) {{
            id
          }}
        }}
        """
        try:
            result = client.query(query)
            if entity_name in result:
                print(f"✓ Found!")
                print(f"  Fields available: {list(result[entity_name][0].keys()) if result[entity_name] else 'none'}")
            else:
                print("not found")
        except Exception as err:
            print(f"error: {err}")
