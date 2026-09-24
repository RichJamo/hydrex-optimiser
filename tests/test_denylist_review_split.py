"""The review path must score boundary_opt over gauges auto_voter may actually vote.

Found across three post-mortems: GAUGE_DENYLIST was applied in auto_voter.py but not in
preboundary_epoch_review.py or export_boundary_optimal_allocation.py, so the benchmark
credited gauges the voter is forbidden to use and the reported opportunity gap was
unreachable by construction. Measured contamination: 63.6% (epoch 1786579200), 39.2%
(1787788800), 17.7% (1789603200). On 1789603200 that turned a real $46 gap into $128.

Contract under test:
  - split_states_by_denylist partitions states exactly — nothing lost, nothing duplicated;
  - matching is case-insensitive, because config and on-chain casing differ;
  - summarize_denylisted_gauges ranks by boundary USD and computes $/1k safely, including
    the zero-vote case that would otherwise divide by zero.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "preboundary_epoch_review.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("preboundary_epoch_review", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def denied(mod):
    """A real denylist entry, so the test tracks config rather than a fixture."""
    entries = sorted(mod.denylisted_gauges())
    assert entries, "GAUGE_DENYLIST is empty; this test needs at least one entry"
    return entries[0]


def _state(gauge, votes=1000.0, rewards=10.0):
    return (gauge, "0xpool", votes, rewards)


def test_denylisted_gauge_is_excluded_from_the_votable_set(mod, denied):
    votable, blocked = mod.split_states_by_denylist(
        [_state(denied), _state("0xfeedface")]
    )
    assert [s[0] for s in votable] == ["0xfeedface"]
    assert [s[0] for s in blocked] == [denied]


def test_matching_is_case_insensitive(mod, denied):
    """config stores lowercase; boundary rows may arrive checksummed."""
    _, blocked = mod.split_states_by_denylist([_state(denied.upper())])
    assert len(blocked) == 1


def test_split_partitions_exactly(mod, denied):
    """Postcondition: no state is dropped and none is counted twice."""
    states = [_state(denied), _state("0xaaa"), _state("0xbbb"), _state(denied.upper())]
    votable, blocked = mod.split_states_by_denylist(states)
    assert len(votable) + len(blocked) == len(states)
    assert [s for s in votable if s in blocked] == []


def test_empty_input_yields_two_empty_lists(mod):
    assert mod.split_states_by_denylist([]) == ([], [])


def test_summary_ranks_by_boundary_usd(mod):
    rows = mod.summarize_denylisted_gauges(
        [_state("0xsmall", 1000.0, 5.0), _state("0xbig", 1000.0, 500.0)], 1_000_000
    )
    assert [r["gauge"] for r in rows] == ["0xbig", "0xsmall"]


def test_summary_computes_usd_per_1k_votes(mod):
    (row,) = mod.summarize_denylisted_gauges(
        [_state("0xg", 250_000.0, 100.0)], 1_000_000
    )
    assert row["usd_per_1k_votes"] == pytest.approx(0.4)


def test_zero_vote_gauge_does_not_divide_by_zero(mod):
    """A gauge with a bribe but no votes yet is real and must not crash the report."""
    (row,) = mod.summarize_denylisted_gauges([_state("0xg", 0.0, 100.0)], 1_000_000)
    assert row["usd_per_1k_votes"] == 0.0


def test_group_a_gauges_are_no_longer_denylisted(mod):
    """Removed 2026-09-17: excluded for 'unpriced tokens', a bug in our pricing.

    All four have paid in USDC/cbBTC/ARIO in each of the last 8 epochs, and the
    token-decimals and price-feed fixes landed in Aug 2026. If one is re-added it should
    carry an over-prediction note backed by a probe vote, not the old unpriced reason.
    """
    removed = {
        "0x25c10987091f98bff0f48a5bd24d7b3bf3419c52",
        "0x5d08b7cdb98ad2db2c5b24c32f7c32ad7ff19379",
        "0x46bba290006233b0eda8fc6d6b4e66eb02115774",
        "0xe63cd99406e98d909ab6d702b11dd4cd31a425a2",
    }
    assert mod.denylisted_gauges().isdisjoint(removed)
