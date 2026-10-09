"""Pins claim_and_swap_rewards.py's command-line surface before it is split into
src/claims/.

This is step 1 of the split-claim-and-swap refactor (issue #8): the script moves from
one 3,970-line file into a package of single-responsibility modules, but
scripts/claim_and_swap_rewards.py keeps the same file name, flags, defaults, help text,
and every top-level name the five pre-existing tests
(test_claim_source_resolution.py, test_claim_transfer_detection.py,
test_reclaim_guard.py, test_voter_claim_call_spec.py, test_claim_artifact_output.py)
import from it. This test is the guard that a move changed none of that.

Contract under test:
  - `--help` output is byte-for-byte what it was before the split (golden file,
    rendered with COLUMNS=100 so argparse's line wrapping is deterministic);
  - every name those five test files pull off the loaded module is still there.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claim_and_swap_rewards.py"
GOLDEN_HELP = ROOT / "tests" / "golden_claim_script_help.txt"

# Every top-level name the five existing test files import from the loaded module.
REQUIRED_PUBLIC_NAMES = [
    "CLAIM_SOURCE_AUTO",
    "CLAIM_SOURCES",
    "ERC20_TRANSFER_TOPIC",
    "RECLAIM_GUARD_BLOCK",
    "RECLAIM_GUARD_FORCED",
    "RECLAIM_GUARD_NOT_APPLICABLE",
    "RECLAIM_GUARD_PROCEED",
    "VOTER_RECIPIENT_CLAIM_SIGNATURES",
    "VOTER_SELF_CLAIM_SIGNATURES",
    "ClaimMovedNothingError",
    "assert_claims_moved_tokens",
    "choose_claim_source",
    "count_reward_transfers",
    "evaluate_reclaim_guard",
    "export_claim_artifact",
    "summarize_claim_transfers",
    "voter_claim_call_spec",
]


def _load_claim_module():
    spec = importlib.util.spec_from_file_location("claim_and_swap_rewards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_output_is_unchanged():
    env = dict(os.environ, COLUMNS="100")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert result.stdout == GOLDEN_HELP.read_text()


def test_every_name_the_existing_tests_import_is_still_present():
    module = _load_claim_module()
    missing = [name for name in REQUIRED_PUBLIC_NAMES if not hasattr(module, name)]
    assert not missing, f"Names no longer importable from the script: {missing}"
