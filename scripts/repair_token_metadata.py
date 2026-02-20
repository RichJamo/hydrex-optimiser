#!/usr/bin/env python3
"""
Repair token metadata (symbol/decimals) in SQLite cache.

Use this to correct stale or incorrect token metadata entries that can skew
reward calculations (e.g., USDC decimals stored as 18 instead of 6).
"""

import argparse
import json
import sqlite3
from pathlib import Path
from datetime import datetime


def load_json_map(path: Path):
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    parser = argparse.ArgumentParser(description="Repair token metadata in SQLite cache")
    parser.add_argument("--database", default="data.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    decimals_file = root / "src" / "token_decimals.json"
    symbols_file = root / "src" / "token_symbols.json"

    decimals_map = {k.lower(): int(v) for k, v in load_json_map(decimals_file).items()}
    symbols_map = {k.lower(): str(v) for k, v in load_json_map(symbols_file).items()}

    hard_overrides = {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": {"symbol": "USDC", "decimals": 6},
        "0x4200000000000000000000000000000000000006": {"symbol": "WETH", "decimals": 18},
        "0x00000e7efa313f4e11bfff432471ed9423ac6b30": {"symbol": "HYDX", "decimals": 18},
        "0x00fbac94fec8d4089d3fe979f39454f48c71a65d": {"symbol": "kVCM", "decimals": 18},
        "0xa1136031150e50b015b41f1ca6b2e99e49d8cb78": {"symbol": "oHYDX", "decimals": 18},
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2": {"symbol": "USDT", "decimals": 6},
    }

    conn = sqlite3.connect(args.database)
    cursor = conn.cursor()

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS token_metadata (
            token_address TEXT PRIMARY KEY,
            symbol TEXT,
            decimals INTEGER,
            updated_at INTEGER
        )
        """
    )

    cursor.execute("SELECT token_address, symbol, decimals FROM token_metadata")
    existing_rows = cursor.fetchall()
    existing = {row[0].lower(): (row[1], row[2]) for row in existing_rows}

    target_addresses = set(existing.keys())
    target_addresses.update(decimals_map.keys())
    target_addresses.update(symbols_map.keys())
    target_addresses.update(hard_overrides.keys())

    updates = []
    inserts = []
    now = int(datetime.utcnow().timestamp())

    for token in sorted(target_addresses):
        old_symbol, old_decimals = existing.get(token, (None, None))

        new_symbol = old_symbol
        new_decimals = old_decimals

        if token in symbols_map:
            new_symbol = symbols_map[token]
        if token in decimals_map:
            new_decimals = decimals_map[token]

        if token in hard_overrides:
            override = hard_overrides[token]
            if "symbol" in override:
                new_symbol = override["symbol"]
            if "decimals" in override:
                new_decimals = override["decimals"]

        if token in existing:
            if new_symbol != old_symbol or new_decimals != old_decimals:
                updates.append((new_symbol, new_decimals, now, token, old_symbol, old_decimals))
        else:
            if new_symbol is not None or new_decimals is not None:
                inserts.append((token, new_symbol, new_decimals, now))

    print(f"Database: {args.database}")
    print(f"Planned updates: {len(updates)}")
    print(f"Planned inserts: {len(inserts)}")

    if updates:
        print("\nSample updates:")
        for new_symbol, new_decimals, _now, token, old_symbol, old_decimals in updates[:10]:
            print(
                f"- {token}: symbol {old_symbol} -> {new_symbol}, decimals {old_decimals} -> {new_decimals}"
            )

    if args.dry_run:
        print("\nDry run only. No changes written.")
        conn.close()
        return

    if updates:
        cursor.executemany(
            "UPDATE token_metadata SET symbol = ?, decimals = ?, updated_at = ? WHERE lower(token_address) = ?",
            [(u[0], u[1], u[2], u[3]) for u in updates],
        )

    if inserts:
        cursor.executemany(
            "INSERT OR REPLACE INTO token_metadata (token_address, symbol, decimals, updated_at) VALUES (?, ?, ?, ?)",
            inserts,
        )

    conn.commit()
    conn.close()

    print("\n✅ token_metadata repair complete")


if __name__ == "__main__":
    main()
