# Local DB Backup Runbook

This project keeps local SQLite files out of Git (best practice for binary DBs).
Use this runbook to protect local data from accidental deletion or disk issues.

## What is backed up

- Primary local DB: `DATABASE_PATH` from `.env` (currently `data/db/data.db`)
- Script: `scripts/backup_local_db.sh`
- Default backup target (outside repo): `$HOME/Backups/hydrex-optimiser`

## 1) Create a backup

From repo root:

```bash
bash scripts/backup_local_db.sh
```

Optional flags:

```bash
bash scripts/backup_local_db.sh --label before-refactor --keep 30
bash scripts/backup_local_db.sh --source data/db/preboundary_dev.db --no-gzip
bash scripts/backup_local_db.sh --dest-dir "$HOME/Backups/hydrex-optimiser"
```

What it does:

- Uses SQLite native `.backup` for a consistent snapshot
- Runs `PRAGMA integrity_check`
- Compresses to `.db.gz` by default
- Keeps only the newest `N` backups (`--keep`, default `14`)

## 2) Restore a backup

1. Stop any running jobs using the DB.
2. Pick the backup file.
3. Restore into place:

Compressed backup (`.db.gz`):

```bash
gunzip -c "$HOME/Backups/hydrex-optimiser/data_YYYYMMDDTHHMMSSZ.db.gz" > data/db/data.db
```

Uncompressed backup (`.db`):

```bash
cp "$HOME/Backups/hydrex-optimiser/data_YYYYMMDDTHHMMSSZ.db" data/db/data.db
```

4. Verify:

```bash
sqlite3 data/db/data.db "PRAGMA integrity_check;"
```

Expected output: `ok`

## 3) Suggested cadence

- Before major fetch/reindex runs: take one labeled backup
- End of day (active research): one backup
- Before large schema/process changes: one labeled backup

## 4) Optional automation (macOS launchd)

If you want nightly backups, create a launchd job that runs:

```bash
cd /Users/richardjamieson/Documents/GitHub/hydrex-optimiser && bash scripts/backup_local_db.sh --label nightly --keep 30
```

If you want, I can add a ready-to-use `launchd` plist next.
