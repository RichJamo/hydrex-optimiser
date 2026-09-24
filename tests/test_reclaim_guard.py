"""Which outcome the already-claimed guard reports, and why exactly one may fire.

Found 2026-09-24 on epoch 1790208000. The `--skip-claims` exemption landed in f6134b8,
but the exemption and the `--force` warning were separate `if` statements reading the
same state, so a swap-only run carrying a redundant `--force` logged both:

    INFO    Epoch 1790208000 has 52 prior Phase 3 claim rows; guard not applicable to a
            --skip-claims run
    WARNING --force passed: bypassing already-claimed guard for epoch 1790208000
            (52 prior success rows)

Only the first is true. The second says a guard was bypassed when no guard applied, and
it is the line that got read back: both the epoch-1789603200 and epoch-1790208000 notes
record that a swap-only run "needed --force", and both epochs were then run with a flag
that changed nothing. A log line that teaches operators to reach for --force is a defect
even though no funds moved wrongly.

Contract under test (evaluate_reclaim_guard):
  - no prior rows -> "proceed", whatever the flags say;
  - prior rows + --skip-claims -> "not_applicable", and this BEATS --force, because a run
    with no claim path has no guard to bypass;
  - prior rows + Phase 3 running + --force -> "forced";
  - prior rows + Phase 3 running + no --force -> "block";
  - exactly one decision is returned, so the caller cannot log two contradictory lines.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "claim_and_swap_rewards.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("claim_and_swap_rewards", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("skip_claims", [True, False])
@pytest.mark.parametrize("force", [True, False])
def test_no_prior_rows_always_proceeds(mod, skip_claims, force):
    """A first claim for an epoch is never gated, under any flag combination."""
    decision, reason = mod.evaluate_reclaim_guard(
        prior_success_count=0, skip_claims=skip_claims, force=force
    )
    assert decision == mod.RECLAIM_GUARD_PROCEED
    assert "no prior" in reason


def test_negative_count_is_treated_as_zero(mod):
    """A bad count must not be trusted into blocking a legitimate first claim."""
    decision, _ = mod.evaluate_reclaim_guard(
        prior_success_count=-1, skip_claims=False, force=False
    )
    assert decision == mod.RECLAIM_GUARD_PROCEED


def test_skip_claims_is_exempt_without_force(mod):
    """The exemption from f6134b8: a swap-only run needs no --force. Guards the fix."""
    decision, reason = mod.evaluate_reclaim_guard(
        prior_success_count=52, skip_claims=True, force=False
    )
    assert decision == mod.RECLAIM_GUARD_NOT_APPLICABLE
    assert "52" in reason


def test_skip_claims_beats_force(mod):
    """The 2026-09-24 defect.

    A swap-only run carrying a redundant --force must report that the guard did not
    apply, NOT that --force bypassed one. Before the fix both branches fired and the
    misleading warning is the line that got read back into the epoch notes.
    """
    decision, reason = mod.evaluate_reclaim_guard(
        prior_success_count=52, skip_claims=True, force=True
    )
    assert decision == mod.RECLAIM_GUARD_NOT_APPLICABLE
    assert "--force is not needed here" in reason
    assert "bypassing" not in reason


def test_force_bypasses_when_phase3_would_run(mod):
    """--force is only ever reported as a bypass when there was something to bypass."""
    decision, reason = mod.evaluate_reclaim_guard(
        prior_success_count=52, skip_claims=False, force=True
    )
    assert decision == mod.RECLAIM_GUARD_FORCED
    assert "bypassing" in reason


def test_prior_rows_block_a_real_reclaim(mod):
    """The guard's actual job: a second Phase 3 run on a claimed epoch is refused."""
    decision, reason = mod.evaluate_reclaim_guard(
        prior_success_count=52, skip_claims=False, force=False
    )
    assert decision == mod.RECLAIM_GUARD_BLOCK
    assert "52" in reason


def test_every_flag_combination_yields_exactly_one_known_decision(mod):
    """The caller branches on the decision, so an unknown value would silently proceed."""
    known = {
        mod.RECLAIM_GUARD_PROCEED,
        mod.RECLAIM_GUARD_NOT_APPLICABLE,
        mod.RECLAIM_GUARD_FORCED,
        mod.RECLAIM_GUARD_BLOCK,
    }
    assert len(known) == 4, "the four decisions must be distinct string constants"
    for count in (0, 1, 52):
        for skip_claims in (True, False):
            for force in (True, False):
                decision, reason = mod.evaluate_reclaim_guard(
                    prior_success_count=count, skip_claims=skip_claims, force=force
                )
                assert decision in known, (count, skip_claims, force, decision)
                assert reason, "every decision must carry an operator-readable reason"
