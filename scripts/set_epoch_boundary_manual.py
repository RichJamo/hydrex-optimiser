#!/usr/bin/env python3

import argparse
import sqlite3
import time
from pathlib import Path

from config.settings import DATABASE_PATH, WEEK


def ensure_epoch_boundaries_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS epoch_boundaries (
            epoch INTEGER NOT NULL PRIMARY KEY,
            boundary_block INTEGER NOT NULL,
            boundary_timestamp INTEGER NOT NULL,
            vote_epoch INTEGER NOT NULL,
            reward_epoch INTEGER NOT NULL,
            source_tag TEXT,
            computed_at INTEGER NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_epoch_boundaries_block
        ON epoch_boundaries(boundary_block)
        """
    )
    conn.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Set/override epoch boundary row manually (explorer-assisted)")
    parser.add_argument("--epoch", type=int, required=True, help="Epoch timestamp (e.g. 1772668800)")
    parser.add_argument("--boundary-block", type=int, required=True, help="Boundary block number from explorer")
    parser.add_argument("--vote-epoch", type=int, default=0, help="Closed vote epoch; default epoch-WEEK")
    parser.add_argument("--boundary-timestamp", type=int, default=0, help="Boundary timestamp; default epoch")
    parser.add_argument("--reward-epoch", type=int, default=0, help="Reward epoch; default epoch")
    parser.add_argument("--source-tag", default="manual_explorer_boundary", help="Source tag for audit")
    parser.add_argument("--db-path", default=DATABASE_PATH, help="SQLite DB path")
    args = parser.parse_args()

    if args.epoch <= 0:
        raise SystemExit("--epoch must be > 0")
    if args.boundary_block <= 0:
        raise SystemExit("--boundary-block must be > 0")

    vote_epoch = int(args.vote_epoch) if args.vote_epoch > 0 else int(args.epoch - WEEK)
    if vote_epoch <= 0:
        raise SystemExit("Resolved vote_epoch <= 0; pass --vote-epoch explicitly")

    boundary_timestamp = int(args.boundary_timestamp) if args.boundary_timestamp > 0 else int(args.epoch)
    reward_epoch = int(args.reward_epoch) if args.reward_epoch > 0 else int(args.epoch)

    db_path = Path(args.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_epoch_boundaries_table(conn)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT OR REPLACE INTO epoch_boundaries (
                epoch, boundary_block, boundary_timestamp, vote_epoch, reward_epoch, source_tag, computed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(args.epoch),
                int(args.boundary_block),
                int(boundary_timestamp),
                int(vote_epoch),
                int(reward_epoch),
                str(args.source_tag),
                int(time.time()),
            ),
        )
        conn.commit()

        row = cur.execute(
            """
            SELECT epoch, boundary_block, boundary_timestamp, vote_epoch, reward_epoch, source_tag
            FROM epoch_boundaries
            WHERE epoch = ?
            """,
            (int(args.epoch),),
        ).fetchone()

        if not row:
            raise SystemExit("Failed to verify inserted epoch boundary row")

        print("✓ epoch_boundaries upserted")
        print(f"epoch={row[0]}")
        print(f"boundary_block={row[1]}")
        print(f"boundary_timestamp={row[2]}")
        print(f"vote_epoch={row[3]}")
        print(f"reward_epoch={row[4]}")
        print(f"source_tag={row[5]}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
