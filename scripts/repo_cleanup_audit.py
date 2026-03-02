#!/usr/bin/env python3
"""
Repository cleanup audit utility.

Produces a cleanup report for:
- legacy script artifacts
- database table row counts
- database table usage across Python source

Optional explicit table drops are supported via --drop-table with --apply.
"""

import argparse
import shutil
import sqlite3
from pathlib import Path
from typing import Dict, List, Sequence, Set, Tuple


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_archived_duplicates(root: Path) -> List[str]:
    root_scripts = {f.name for f in root.glob("*.py") if f.is_file()}
    archived_scripts = {
        f.name for f in (root / "scripts" / "archive").glob("*.py") if f.is_file()
    }
    return sorted(root_scripts.intersection(archived_scripts))


def find_backup_artifacts(root: Path) -> List[str]:
    return sorted(str(p.relative_to(root)) for p in root.glob("*.bak") if p.is_file())


def list_db_tables(conn: sqlite3.Connection) -> List[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def table_row_count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
    return int(row[0] if row else 0)


def python_files(root: Path) -> List[Path]:
    return [
        path
        for path in root.rglob("*.py")
        if "venv" not in path.parts and ".git" not in path.parts
    ]


def scan_table_usage(root: Path, tables: Sequence[str]) -> Dict[str, List[str]]:
    usage: Dict[str, List[str]] = {table: [] for table in tables}
    py_files = python_files(root)

    for file_path in py_files:
        text = file_path.read_text(encoding="utf-8", errors="ignore").lower()
        rel = str(file_path.relative_to(root))
        for table in tables:
            token = table.lower()
            if token in text:
                usage[table].append(rel)

    for table in tables:
        usage[table] = sorted(set(usage[table]))

    return usage


def weak_reference_tables(usage: Dict[str, List[str]]) -> List[str]:
    result: List[str] = []
    for table_name, refs in usage.items():
        if refs and set(refs).issubset({"src/database.py"}):
            result.append(table_name)
    return sorted(result)


def drop_tables(conn: sqlite3.Connection, table_names: Sequence[str]) -> Tuple[List[str], List[str]]:
    existing = set(list_db_tables(conn))
    dropped: List[str] = []
    skipped: List[str] = []
    for table in table_names:
        if table not in existing:
            skipped.append(table)
            continue
        conn.execute(f"DROP TABLE IF EXISTS {table}")
        dropped.append(table)
    conn.commit()
    return dropped, skipped


def backup_database(db_path: Path, backup_path: Path) -> None:
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(db_path, backup_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Repository cleanup audit")
    parser.add_argument("--db-path", default="data/db/data.db", help="SQLite DB path")
    parser.add_argument(
        "--drop-table",
        action="append",
        default=[],
        help="Table to drop explicitly (repeat flag for multiple tables)",
    )
    parser.add_argument("--apply", action="store_true", help="Apply explicit table drops")
    parser.add_argument(
        "--backup-path",
        default="data/db/backups/data_cleanup_backup.db",
        help="Backup path before applying drops",
    )
    args = parser.parse_args()

    root = repo_root()
    db_path = (root / args.db_path).resolve()

    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    duplicates = find_archived_duplicates(root)
    backup_artifacts = find_backup_artifacts(root)

    conn = sqlite3.connect(str(db_path))
    try:
        tables = list_db_tables(conn)
        usage = scan_table_usage(root, tables)
        rows = {table: table_row_count(conn, table) for table in tables}

        print("=== Cleanup Audit ===")
        print(f"Repo: {root}")
        print(f"DB:   {db_path}")
        print()

        print("[Legacy script artifacts]")
        print(f"Archived duplicates at repo root: {len(duplicates)}")
        for name in duplicates:
            print(f"  - {name}")
        print(f"Backup artifacts (*.bak at repo root): {len(backup_artifacts)}")
        for name in backup_artifacts:
            print(f"  - {name}")
        print()

        print("[Database tables]")
        for table in tables:
            ref_count = len(usage.get(table, []))
            print(f"  - {table:<32} rows={rows[table]:>8}  python_refs={ref_count:>3}")
        print()

        unreferenced = sorted([t for t in tables if not usage.get(t)])
        weak_refs = weak_reference_tables(usage)
        print("[Candidates]")
        print(f"Unreferenced in Python: {len(unreferenced)}")
        for table in unreferenced:
            print(f"  - {table}")
        print(f"Referenced only by src/database.py: {len(weak_refs)}")
        for table in weak_refs:
            print(f"  - {table}")
        print()

        explicit_drops: List[str] = [str(t).strip() for t in args.drop_table if str(t).strip()]
        if explicit_drops and not args.apply:
            print("[Dry-run explicit drops]")
            for table in explicit_drops:
                print(f"  DROP TABLE IF EXISTS {table};")
            print("Re-run with --apply to execute.")

        if explicit_drops and args.apply:
            backup_path = (root / args.backup_path).resolve()
            backup_database(db_path=db_path, backup_path=backup_path)
            dropped, skipped = drop_tables(conn, explicit_drops)
            print("[Applied explicit drops]")
            print(f"Backup written: {backup_path}")
            print(f"Dropped: {dropped}")
            print(f"Skipped (not found): {skipped}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
