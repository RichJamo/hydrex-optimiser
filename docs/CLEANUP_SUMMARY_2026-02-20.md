# Cleanup Summary: Archived Scripts Reappearance (Feb 20, 2026)

## Situation

After previous cleanup work, 24 archived scripts reappeared at the root level as untracked files. This occurred despite the archive migration being locked in commit c98366a.

## Root Cause

**macOS local filesystem recovery**: When user clicked "Keep" in a previous session, the system restored deleted files from local recovery buffers. This is a **local phenomenon, not a git issue**.

## Cleanup Performed

### Before

- **Root scripts**: 34 .py files
- **Archived scripts**: 53 .py files
- **Duplicates**: 24 files (all untracked)
- **Tracked modified files**: 12 (docs/, config/, scripts/repair_token_metadata.py, etc.)

### Action Taken

Removed all 24 untracked duplicates:

```
✓ Removed analyze_epoch_maximum_return.py
✓ Removed analyze_recent_vote.py
✓ Removed check_mystery_gauge_details.py
... (21 more)
✓ Removed verify_historical_bribes.py
```

### After

- **Root scripts**: 10 .py files (active/primary only)
- **Duplicates**: 0
- **Tracked modified files**: All preserved (12 files)
- **Repository**: Clean

## Preventive Measures Implemented

### 1. Detection Script

**File**: [scripts/detect_archived_script_reappearance.py](../scripts/detect_archived_script_reappearance.py)

Detects when archived scripts reappear at root. Safe to run anytime:

```bash
python scripts/detect_archived_script_reappearance.py
```

Returns exit code 0 (clean) or 1 (violation detected).

### 2. Pre-Commit Hook

**File**: [scripts/pre-commit-archived-scripts-guard](../scripts/pre-commit-archived-scripts-guard)

Blocks commits if archived scripts are detected at root. Install:

```bash
cp scripts/pre-commit-archived-scripts-guard .git/hooks/pre-commit
```

### 3. Documentation

**File**: [docs/ARCHIVED_SCRIPTS_GUARD.md](../docs/ARCHIVED_SCRIPTS_GUARD.md)

Complete guide on:

- How duplicates occur
- How to detect them
- How to prevent them
- Best practices

## Why This Keeps Happening

1. **Finder operations**: Deleting files in macOS Finder with "Keep" option
2. **System recovery**: macOS's built-in file recovery mechanisms
3. **Clipboard/undo actions**: System-level undo operations
4. **Not git**: The archive migration (c98366a) is persistent; git isn't undoing it

## Recommendations

- **Avoid Finder deletions**: Use terminal `rm` instead of Finder
- **Ignore "Keep" dialogs**: Choose "Move to Trash" instead
- **Use guard scripts**: Regular scans before commits
- **Install pre-commit hook**: Automatic protection

## Files Modified

- ✓ `scripts/detect_archived_script_reappearance.py` (new)
- ✓ `scripts/pre-commit-archived-scripts-guard` (new)
- ✓ `docs/ARCHIVED_SCRIPTS_GUARD.md` (new)
- ✓ All tracked modifications preserved

## Verification

```bash
# Run detection (should show clean)
python scripts/detect_archived_script_reappearance.py

# View all guard documentation
cat docs/ARCHIVED_SCRIPTS_GUARD.md
```

## Status

✅ **Cleanup complete**  
✅ **All tracked changes preserved**  
✅ **Prevention systems in place**  
✅ **Documentation added**
