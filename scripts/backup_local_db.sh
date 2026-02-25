#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Create a consistent SQLite backup with optional compression and retention.

Usage:
  scripts/backup_local_db.sh [options]

Options:
  --source PATH      Source SQLite DB file.
                     Default: DATABASE_PATH from .env, else data/db/data.db
  --dest-dir PATH    Backup destination directory.
                     Default: $HOME/Backups/hydrex-optimiser
  --keep N           Keep the newest N backups for this DB stem (default: 14)
  --no-gzip          Keep plain .db backup instead of .db.gz
  --label NAME       Optional label in filename (e.g. pre-release)
  -h, --help         Show this help
EOF
}

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

SOURCE_DB=""
DEST_DIR="${HOME}/Backups/hydrex-optimiser"
KEEP_COUNT=14
USE_GZIP=1
LABEL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --source)
      SOURCE_DB="$2"
      shift 2
      ;;
    --dest-dir)
      DEST_DIR="$2"
      shift 2
      ;;
    --keep)
      KEEP_COUNT="$2"
      shift 2
      ;;
    --no-gzip)
      USE_GZIP=0
      shift
      ;;
    --label)
      LABEL="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ -z "$SOURCE_DB" ]]; then
  if [[ -f ".env" ]]; then
    ENV_PATH_LINE="$(grep -E '^DATABASE_PATH=' .env | tail -n 1 || true)"
    if [[ -n "$ENV_PATH_LINE" ]]; then
      SOURCE_DB="${ENV_PATH_LINE#DATABASE_PATH=}"
    fi
  fi
fi

if [[ -z "$SOURCE_DB" ]]; then
  SOURCE_DB="data/db/data.db"
fi

if [[ ! -f "$SOURCE_DB" ]]; then
  echo "Source DB not found: $SOURCE_DB" >&2
  exit 1
fi

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "sqlite3 is required but not found in PATH" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"

timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
source_base="$(basename "$SOURCE_DB")"
source_stem="${source_base%.*}"
label_part=""
if [[ -n "$LABEL" ]]; then
  label_part="_${LABEL}"
fi

plain_backup="${DEST_DIR}/${source_stem}${label_part}_${timestamp}.db"

sqlite3 "$SOURCE_DB" ".backup '$plain_backup'"

integrity="$(sqlite3 "$plain_backup" 'PRAGMA integrity_check;' | head -n 1)"
if [[ "$integrity" != "ok" ]]; then
  echo "Backup integrity check failed: $integrity" >&2
  rm -f "$plain_backup"
  exit 1
fi

final_path="$plain_backup"
if [[ "$USE_GZIP" -eq 1 ]]; then
  gzip -f "$plain_backup"
  final_path="${plain_backup}.gz"
fi

if [[ "$KEEP_COUNT" =~ ^[0-9]+$ ]]; then
  all_backups=()
  while IFS= read -r line; do
    [[ -n "$line" ]] && all_backups+=("$line")
  done < <(ls -1t "${DEST_DIR}/${source_stem}"*.db* 2>/dev/null || true)

  if (( ${#all_backups[@]} > KEEP_COUNT )); then
    idx=$KEEP_COUNT
    while (( idx < ${#all_backups[@]} )); do
      rm -f "${all_backups[$idx]}"
      idx=$((idx + 1))
    done
  fi
else
  echo "Invalid --keep value: $KEEP_COUNT" >&2
  exit 1
fi

echo "Backup created: $final_path"
echo "Source: $SOURCE_DB"
echo "Integrity: $integrity"
