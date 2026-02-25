#!/usr/bin/env python3
"""
One-time extraction of gauge→bribe mapping.

This is a one-time setup step:
- Extract all gauges that ever had bribe activity (from bribes table)
- Map each gauge to its internal/external bribe contracts (from gauges table)
- Store in gauge_bribe_mapping table for reuse across all epochs

Usage:
  python -m data.fetchers.fetch_gauge_bribe_mapping
"""

import sqlite3
from pathlib import Path
from typing import List, Tuple

from rich.console import Console

from config.settings import DATABASE_PATH

console = Console()


def ensure_mapping_table(conn: sqlite3.Connection) -> None:
    """Create gauge_bribe_mapping table if it doesn't exist."""
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS gauge_bribe_mapping (
            gauge_address TEXT PRIMARY KEY,
            internal_bribe TEXT,
            external_bribe TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.commit()


def extract_historically_active_gauges(conn: sqlite3.Connection) -> List[str]:
    """Extract all gauges that ever had bribe activity."""
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT LOWER(gauge_address) FROM bribes")
    return [row[0] for row in cur.fetchall() if row and row[0]]


def load_gauge_mappings(conn: sqlite3.Connection) -> List[Tuple[str, str, str]]:
    """Load gauge→(internal_bribe, external_bribe) mappings from gauges table."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 
            LOWER(address),
            LOWER(COALESCE(internal_bribe, '')),
            LOWER(COALESCE(external_bribe, ''))
        FROM gauges
        WHERE address IS NOT NULL
        """
    )
    return [(g, ib, eb) for g, ib, eb in cur.fetchall()]


def populate_mapping_table(
    conn: sqlite3.Connection,
    active_gauges: List[str],
    mappings: List[Tuple[str, str, str]],
) -> int:
    """Populate gauge_bribe_mapping table with active gauges."""
    cur = conn.cursor()
    now_ts = int(__import__("time").time())
    
    # Build mapping dict
    mapping_dict = {g.lower(): (ib, eb) for g, ib, eb in mappings}
    
    # Filter to only historically-active gauges with mappings
    rows_inserted = 0
    for gauge in active_gauges:
        gauge_lower = gauge.lower()
        if gauge_lower in mapping_dict:
            ib, eb = mapping_dict[gauge_lower]
            cur.execute(
                """
                INSERT OR REPLACE INTO gauge_bribe_mapping (gauge_address, internal_bribe, external_bribe, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (gauge_lower, ib, eb, now_ts),
            )
            rows_inserted += 1
    
    conn.commit()
    return rows_inserted


def main() -> None:
    conn = sqlite3.connect(DATABASE_PATH)
    
    console.print("[cyan]Phase 1/4: Creating gauge_bribe_mapping table[/cyan]")
    ensure_mapping_table(conn)
    
    console.print("[cyan]Phase 2/4: Extracting historically-active gauges from bribes table[/cyan]")
    active_gauges = extract_historically_active_gauges(conn)
    console.print(f"[green]Found {len(active_gauges)} historically-active gauges[/green]")
    
    console.print("[cyan]Phase 3/4: Loading gauge→bribe mappings from gauges table[/cyan]")
    mappings = load_gauge_mappings(conn)
    console.print(f"[green]Loaded {len(mappings)} gauge mappings[/green]")
    
    console.print("[cyan]Phase 4/4: Populating gauge_bribe_mapping table[/cyan]")
    rows_inserted = populate_mapping_table(conn, active_gauges, mappings)
    console.print(f"[green]Inserted {rows_inserted} rows into gauge_bribe_mapping[/green]")
    
    # Verify
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM gauge_bribe_mapping")
    count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(DISTINCT internal_bribe) + COUNT(DISTINCT external_bribe) FROM gauge_bribe_mapping WHERE internal_bribe != '' OR external_bribe != ''")
    unique_bribes = cur.fetchone()[0]
    
    console.print()
    console.print(f"[bold green]✅ Gauge→bribe mapping complete[/bold green]")
    console.print(f"   {count} historically-active gauges with bribe mappings")
    console.print(f"   {unique_bribes} unique bribe contracts across all gauges")
    
    conn.close()


if __name__ == "__main__":
    main()
