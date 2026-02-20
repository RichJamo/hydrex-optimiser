#!/usr/bin/env python3
"""
Detect when archived scripts reappear at root level.
This guards against accidental restoration via OS recovery or Keep actions.
"""
import os
import sys
from pathlib import Path

def check_archived_duplicates(repo_root: str = ".") -> dict:
    """
    Compare root *.py files with scripts/archive/*.py files.
    
    Returns dict with:
    - untracked_duplicates: list of root .py files that also exist in archive
    - tracked_duplicates: list of tracked root .py files that also exist in archive
    - violated: bool indicating if violation detected
    """
    repo_path = Path(repo_root).resolve()
    root_scripts = set(f.name for f in repo_path.glob("*.py") if f.is_file())
    archived_scripts = set(
        f.name for f in (repo_path / "scripts" / "archive").glob("*.py") if f.is_file()
    )
    
    duplicates = root_scripts & archived_scripts
    
    result = {
        "duplicates": sorted(list(duplicates)),
        "count": len(duplicates),
        "violated": len(duplicates) > 0,
    }
    
    if duplicates:
        # Check which are tracked
        os.chdir(repo_path)
        import subprocess
        tracked_duplicates = []
        untracked_duplicates = []
        for dup in duplicates:
            status = subprocess.run(
                ["git", "ls-files", dup],
                capture_output=True,
                text=True,
            ).stdout.strip()
            if status:
                tracked_duplicates.append(dup)
            else:
                untracked_duplicates.append(dup)
        
        result["tracked_duplicates"] = tracked_duplicates
        result["untracked_duplicates"] = untracked_duplicates
    
    return result

if __name__ == "__main__":
    result = check_archived_duplicates()
    
    if result["violated"]:
        print(f"❌ VIOLATION: {result['count']} archived scripts reappeared at root!")
        print(f"\nDuplicates found (tracked: {len(result.get('tracked_duplicates', []))}, untracked: {len(result.get('untracked_duplicates', []))}):")
        for dup in result["duplicates"]:
            status = "TRACKED" if dup in result.get("tracked_duplicates", []) else "untracked"
            print(f"  - {dup} ({status})")
        print("\n⚠️  Likely cause: macOS 'Keep' action or local filesystem recovery.")
        print("    Solution: rm <untracked files> or git checkout -- <tracked files>")
        sys.exit(1)
    else:
        print("✓ No archived scripts at root - repository is clean")
        sys.exit(0)
