#!/usr/bin/env python3
"""
Repair token metadata (symbol/decimals) in SQLite cache.

Use this to correct stale or incorrect token metadata entries that can skew
reward calculations (e.g., USDC decimals stored as 18 instead of 6).

--backfill-onchain additionally reads symbol()/decimals() for every row still missing
them. The static JSON maps only cover tokens somebody curated by hand, so new reward
tokens land with decimals and a NULL symbol and stay that way: 112 of 229 rows on
2026-09-17, including 10 of the 16 tokens that epoch actually paid in. Symbols do not
affect pricing (that is keyed on address throughout), but the post-mortem reconciliation
is read by symbol, and a missing one used to split a token into a phantom +/- pair.
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


ERC20_METADATA_ABI = [
    {"name": "symbol", "inputs": [], "outputs": [{"type": "string"}],
     "stateMutability": "view", "type": "function"},
    {"name": "decimals", "inputs": [], "outputs": [{"type": "uint8"}],
     "stateMutability": "view", "type": "function"},
]


def backfill_symbols_onchain(addresses):
    """Read symbol()/decimals() for `addresses`.

    Returns ({address: (symbol, decimals)}, [unreadable addresses]). A token whose
    symbol() reverts or returns bytes we cannot decode is reported, never guessed —
    a wrong symbol is worse than a missing one, because it would silently mis-join
    the reconciliation instead of showing up as unmatched.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))
    from dotenv import load_dotenv
    from web3 import Web3

    load_dotenv()
    from config.settings import RPC_URL

    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    resolved, failed = {}, []
    for address in addresses:
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(address), abi=ERC20_METADATA_ABI
            )
            symbol = contract.functions.symbol().call()
            if isinstance(symbol, bytes):
                symbol = symbol.decode("utf-8").rstrip("\x00")
            symbol = str(symbol).strip()
            if not symbol:
                failed.append(address)
                continue
            try:
                decimals = int(contract.functions.decimals().call())
            except Exception:
                decimals = None
            resolved[address] = (symbol, decimals)
        except Exception:
            failed.append(address)
    return resolved, failed


def main():
    parser = argparse.ArgumentParser(description="Repair token metadata in SQLite cache")
    parser.add_argument("--database", default="data/db/data.db", help="Path to SQLite database")
    parser.add_argument("--dry-run", action="store_true", help="Show changes without writing")
    parser.add_argument(
        "--backfill-onchain",
        action="store_true",
        help="Read symbol()/decimals() on-chain for rows still missing them",
    )
    parser.add_argument(
        "--backfill-limit",
        type=int,
        default=0,
        help="Cap on-chain lookups (0 = no cap)",
    )
    parser.add_argument(
        "--backfill-only",
        action="store_true",
        help=(
            "Only fill symbols that are missing, from chain; never re-apply the curated "
            "JSON maps or hard overrides. Implies --backfill-onchain. Safe to run on "
            "every post-mortem — it cannot change an existing symbol or any decimals."
        ),
    )
    args = parser.parse_args()
    if args.backfill_only:
        args.backfill_onchain = True

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
    if not args.backfill_only:
        # The curated maps name tokens that may have no row. --backfill-only must not
        # consult them at all: it would query and insert every token listed there,
        # which is the opposite of the narrow step the post-mortem relies on.
        target_addresses.update(decimals_map.keys())
        target_addresses.update(symbols_map.keys())
        target_addresses.update(hard_overrides.keys())
    # Reward tokens are the ones the post-mortem reconciles, so a new one must be
    # covered even if no other writer has created its token_metadata row yet.
    try:
        cursor.execute("SELECT DISTINCT lower(reward_token) FROM boundary_reward_snapshots")
        target_addresses.update(row[0] for row in cursor.fetchall() if row[0])
    except sqlite3.Error as e:
        # A fresh DB has no snapshots table yet, and nothing is lost by skipping it.
        # Anything else (a lock, a schema change) silently drops reward-token coverage
        # for this run, so it must be visible.
        if "no such table" not in str(e):
            print(
                f"WARNING: could not read reward tokens from boundary_reward_snapshots "
                f"({e}); new reward tokens without a token_metadata row will not be "
                f"backfilled this run"
            )

    updates = []
    inserts = []
    now = int(datetime.utcnow().timestamp())

    for token in ([] if args.backfill_only else sorted(target_addresses)):
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

    if args.backfill_onchain:
        needs = [
            token
            for token in sorted(target_addresses)
            if not (existing.get(token, (None, None))[0] or "").strip()
            and not any(u[3] == token and u[0] for u in updates)
            and not any(i[0] == token and i[1] for i in inserts)
        ]
        if args.backfill_limit > 0:
            needs = needs[: args.backfill_limit]
        print(f"On-chain backfill: {len(needs)} row(s) missing a symbol")
        if needs:
            resolved, failed = backfill_symbols_onchain(needs)
            for token, (symbol, decimals) in resolved.items():
                old_symbol, old_decimals = existing.get(token, (None, None))
                merged_decimals = old_decimals if old_decimals is not None else decimals
                if token in existing:
                    updates.append(
                        (symbol, merged_decimals, now, token, old_symbol, old_decimals)
                    )
                else:
                    inserts.append((token, symbol, merged_decimals, now))
            print(f"On-chain backfill: resolved {len(resolved)}, unreadable {len(failed)}")
            for token in failed[:5]:
                print(f"  unreadable: {token}")

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
