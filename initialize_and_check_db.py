#!/usr/bin/env python3
"""
Initialize database and check what gauge data we have.
"""

import sys
sys.path.insert(0, '/Users/richardjamieson/Documents/GitHub/hydrex-optimiser')

from src.database import Database

# Initialize database
db = Database('/Users/richardjamieson/Documents/GitHub/hydrex-optimiser/hydrex_data.db')
db.create_tables()
print("✓ Database tables created")

# Check what gauge data exists
session = db.get_session()

from src.database import Gauge

gauges = session.query(Gauge).all()
print(f"\nTotal gauges in database: {len(gauges)}")

if len(gauges) > 0:
    print("\nFirst 5 gauges:")
    for gauge in gauges[:5]:
        print(f"  Gauge: {gauge.address}")
        print(f"  Pool:  {gauge.pool}")
        print(f"  Internal: {gauge.internal_bribe}")
        print(f"  External: {gauge.external_bribe}")
        print()

# Check for the specific gauge the user mentioned
KNOWN_GAUGE = "0x632f2D41Ba9e6E80035D578DDD48b019e4403F86"
specific_gauge = session.query(Gauge).filter(Gauge.address == KNOWN_GAUGE.lower()).first()

print(f"Looking for gauge {KNOWN_GAUGE}:")
if specific_gauge:
    print(f"  ✓ Found in database!")
    print(f"  Pool:     {specific_gauge.pool}")
    print(f"  Internal: {specific_gauge.internal_bribe}")
    print(f"  External: {specific_gauge.external_bribe}")
else:
    print(f"  ❌ NOT found in database")

session.close()
