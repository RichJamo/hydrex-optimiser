"""Where claim_and_swap_rewards writes its run artifact by default.

Until 2026-10-08 the default --output was phase1_2_artifact.json in the repo root, so
every claim run left a modified tracked file behind. The default now sits in the
git-ignored runs/ folder, which a fresh clone does not have.

Contract under test:
  - the default --output is inside runs/;
  - export_claim_artifact creates missing parent folders and writes valid JSON.
"""

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_claim_module():
    spec = importlib.util.spec_from_file_location(
        "claim_and_swap_rewards", ROOT / "scripts" / "claim_and_swap_rewards.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_output_is_in_the_ignored_runs_folder():
    source = (ROOT / "scripts" / "claim_and_swap_rewards.py").read_text()
    assert 'default="runs/claim_and_swap_artifact.json"' in source
    assert "runs/" in (ROOT / ".gitignore").read_text().splitlines()


def test_export_creates_missing_folders(tmp_path):
    claim = _load_claim_module()
    output = tmp_path / "runs" / "nested" / "artifact.json"

    claim.export_claim_artifact(
        str(output),
        epoch=1791417600,
        signer_address="0x1111111111111111111111111111111111111111",
        gauges=[],
        gauge_to_bribes={},
        reward_tokens={},
    )

    artifact = json.loads(output.read_text())
    assert artifact["epoch"] == 1791417600
    assert artifact["claim_results"] == []
