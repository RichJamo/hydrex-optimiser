"""Check database contents."""

from src.database import Database
from config import Config

db = Database(Config.DATABASE_PATH)

# Check what data we have
gauges = db.get_all_gauges()
print(f"📊 Database Status:")
print(f"  Gauges: {len(gauges)}")

if gauges:
    print(f"\n✅ First 5 gauges:")
    for g in gauges[:5]:
        print(f"  • {g.address[:12]}... (pool: {g.pool[:12]}...)")
        print(f"    Internal: {g.internal_bribe[:12]}...")
        print(f"    External: {g.external_bribe[:12]}...")
        print(f"    Alive: {g.is_alive}")
        print()

# Check data counts  
print(f"📈 Data counts:")
print(f"  Gauges: {len(gauges)}")

# Try to get epochs if any
try:
    all_epochs = db.get_all_epochs()
    print(f"  Epochs: {len(all_epochs)}")
except:
    print(f"  Epochs: 0")

print(f"\n⚠️  Note: Historical analysis requires vote data from past epochs")
print(f"⚠️  Bribe data requires subgraph templates to be synced")
