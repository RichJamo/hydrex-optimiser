#!/usr/bin/env python3
"""
Fix the database schema to handle large vote numbers.
"""

import sqlite3

DATABASE_PATH = "data.db"

print("Fixing database schema for large vote numbers...")

conn = sqlite3.connect(DATABASE_PATH)
cursor = conn.cursor()

# Check if current_votes column exists and what type it is
cursor.execute("PRAGMA table_info(gauges)")
columns = {col[1]: col[2] for col in cursor.fetchall()}

if 'current_votes' in columns:
    current_type = columns['current_votes']
    print(f"Current type of current_votes: {current_type}")
    
    if current_type != 'TEXT':
        print("Converting current_votes from INTEGER to TEXT...")
        
        # SQLite doesn't support ALTER COLUMN TYPE directly
        # We need to create a new column, copy data, drop old, rename
        
        # 1. Add temporary column as TEXT
        try:
            cursor.execute("ALTER TABLE gauges ADD COLUMN current_votes_text TEXT")
            print("✓ Added temporary TEXT column")
        except:
            print("Temporary column already exists")
        
        # 2. Copy data (convert to string)
        cursor.execute("UPDATE gauges SET current_votes_text = CAST(current_votes AS TEXT)")
        print("✓ Copied data to new column")
        
        # 3. Drop old column (SQLite doesn't support DROP COLUMN directly in older versions)
        # So we'll just leave the old column and use the new one
        # Or better: recreate the table
        
        print("Recreating gauges table with correct schema...")
        
        # Get all data from current table
        cursor.execute("SELECT address, pool, internal_bribe, external_bribe, is_alive, created_at, last_updated, current_votes_text FROM gauges")
        all_data = cursor.fetchall()
        
        # Drop and recreate table
        cursor.execute("DROP TABLE gauges")
        cursor.execute("""
            CREATE TABLE gauges (
                address VARCHAR PRIMARY KEY,
                pool VARCHAR,
                internal_bribe VARCHAR,
                external_bribe VARCHAR,
                is_alive INTEGER,
                created_at INTEGER,
                last_updated INTEGER,
                current_votes TEXT DEFAULT '0'
            )
        """)
        
        # Reinsert data
        for row in all_data:
            cursor.execute("""
                INSERT INTO gauges 
                (address, pool, internal_bribe, external_bribe, is_alive, created_at, last_updated, current_votes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, row)
        
        print(f"✓ Recreated table with {len(all_data)} rows")
    else:
        print("✓ Column already TEXT type")
else:
    print("current_votes column doesn't exist yet")

conn.commit()
conn.close()

print("\n✓ Database schema fixed!")
print("You can now run collect_all_data.py again - it will resume from storage step.")
