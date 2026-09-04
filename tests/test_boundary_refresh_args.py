"""Boundary refresh arg construction for the post-mortem wrapper.

Regression cover for epoch 1788393600, where the refresh skipped a 54,595 oHYDX bribe
(~$869) because --ignore-whitelist was never passed. The whitelist is built from
(bribe, token) pairs that already have a non-zero row in boundary_reward_snapshots, so a
token new to a bribe is never queried, never gets a row, and stays excluded forever.
That understated executed_realized_at_boundary by $162.97 ($251.93 -> $414.90).


Not covered here: the CLI default itself. The parser is constructed inline in main(),
so `--boundary-ignore-whitelist` defaulting to True is verified only via `--help`
rather than by a unit test. Extracting the parser would be a large diff for a small
gain; revisit if that default regresses.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_postmortem_review.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_postmortem_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def build():
    return _load_module().build_boundary_refresh_args


def test_ignore_whitelist_is_present_when_requested(build):
    args = build(epoch=1788393600, price_source="snapshot", ignore_whitelist=True)
    assert "--ignore-whitelist" in args


def test_ignore_whitelist_is_omitted_when_opted_out(build):
    args = build(epoch=1788393600, price_source="snapshot", ignore_whitelist=False)
    assert "--ignore-whitelist" not in args


def test_refresh_is_scoped_to_one_epoch_never_all_epochs(build):
    """--all-epochs is the shell script's default; the wrapper must always override it."""
    args = build(epoch=1788393600, price_source="snapshot", ignore_whitelist=True)
    assert "--epochs 1788393600" in args
    assert "--all-epochs" not in args


@pytest.mark.parametrize("source", ["snapshot", "routing"])
def test_price_source_is_always_pinned(build, source):
    """An unpinned price source lets a refresh reprice the vote-time basis at today's quotes."""
    args = build(epoch=1788393600, price_source=source, ignore_whitelist=True)
    assert f"--price-source {source}" in args


def test_epoch_is_coerced_to_int(build):
    """Guards against an epoch reaching the shell as a float, e.g. '--epochs 1788393600.0'."""
    args = build(epoch=1788393600.0, price_source="snapshot", ignore_whitelist=True)
    assert "--epochs 1788393600" in args
    assert "1788393600.0" not in args
