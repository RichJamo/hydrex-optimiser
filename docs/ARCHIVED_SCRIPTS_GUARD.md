# Archived Scripts Reappearance Guard

## Problem

After the archive migration commit (c98366a), archived scripts occasionally **reappear at the root level as untracked files**. This happens when:

1. **macOS "Keep" action**: When deleting files in Finder, pressing "Keep" can restore files from .Trash or system recovery
2. **Local filesystem recovery**: System utilities or file manager "undo" actions
3. **Copy/paste remnants**: Clipboard operations that restore deleted files

**Why this is dangerous:**

- Untracked duplicates can be accidentally committed
- Creates confusion about which version is authoritative
- Violates the archive structure established in commit c98366a

## Solution: Detection & Prevention

### Quick Check

Run anytime to detect if archived scripts have reappeared:

```bash
python scripts/detect_archived_script_reappearance.py
```

**Output if clean:**

```
✓ No archived scripts at root - repository is clean
```

**Output if violation detected:**

```
❌ VIOLATION: 5 archived scripts reappeared at root!

Duplicates found (tracked: 0, untracked: 5):
  - analyze_epoch_maximum_return.py (untracked)
  - fetch_all_gauges.py (untracked)
  ...
```

### Automatic Prevention: Pre-Commit Hook

Install the hook to block commits when archived scripts are detected:

```bash
cp scripts/pre-commit-archived-scripts-guard .git/hooks/pre-commit
chmod +x .git/hooks/pre-commit
```

The hook will fail commits with clear instructions if a violation is detected.

### Manual Cleanup

If duplicates are found:

1. **Untracked files** (safe to delete):

   ```bash
   rm <filename>.py
   ```

2. **Tracked files** (restore from git):
   ```bash
   git checkout -- <filename>.py
   ```

## Root Cause Analysis

The reappearance is **not** a git issue—the archived migration commit (c98366a) is persistent on main. Instead:

- **Local filesystem interaction**: macOS system tools or the Finder "Keep" action
- **No automatic restore**: Git never undoes the archive move; files must be manually restored somehow
- **Pattern**: Happens after explicit user actions (pressing "Keep" dialog, using undo)

## Implementation Details

### `detect_archived_script_reappearance.py`

- Compares root `*.py` filenames with `scripts/archive/*.py`
- Reports duplicates with track status
- Returns exit code 0 (clean) or 1 (violation)
- Safe to call frequently in CI/CD

### `pre-commit-archived-scripts-guard`

- Runs before every commit
- Uses detection script to check for violations
- Blocks commit with clear remediation instructions
- Does NOT auto-fix (preserves user intent)

## Workflow Best Practices

1. **Before any file operations in Finder**: Use terminal or VS Code instead

   ```bash
   rm file.py  # instead of Finder delete
   ```

2. **If "Keep" dialog appears**: Always choose **Move to Trash** (don't click Keep)

3. **Before committing**: Run detection script

   ```bash
   python scripts/detect_archived_script_reappearance.py
   ```

4. **Regular validation**: Include detection in CI/CD pipelines

## Historical Context

- **Commit c98366a**: Archive migration moved all test/old scripts to `scripts/archive/`
- **Previous recurrence**: Similar issue solved by cleanup work
- **Preventive measures**: Now in place to catch future reappearances
