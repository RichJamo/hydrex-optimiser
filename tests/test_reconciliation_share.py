"""Our share of a gauge's bribe, as the post-mortem reconciliation computes it.

Found 2026-09-17. The token block of run_preboundary_analysis_pipeline.sh divided by
(boundary_votes + executed_votes), but boundary_gauge_values.votes_raw ALREADY includes
our votes — the executed_realized block 40 lines above subtracts them for exactly that
reason. Our votes therefore sat in the denominator twice and every expected amount came
out low, worst on the gauges we concentrate in. Epoch 1789603200 reported
token_expected_total_usd = 734.60 where the truth was 788.57, which is
executed_realized_at_boundary_usd to the cent.

This test pins the arithmetic against that live case. The shell script embeds its Python
in a heredoc, so the formula is reimplemented here; `test_pipeline_uses_inclusive_denominator`
guards the script itself against regressing to the old form.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PIPELINE = ROOT / "scripts" / "shell" / "run_preboundary_analysis_pipeline.sh"


def share(boundary_votes: float, executed_votes: int) -> float:
    """Our share of a gauge, given boundary votes that already include ours."""
    if boundary_votes <= 0:
        return 0.0
    return min(1.0, executed_votes / boundary_votes)


def test_share_uses_boundary_votes_as_the_whole():
    """606,000 of 35,167,760 total — not of 35,773,760."""
    assert share(35_167_760.0, 606_000) == pytest.approx(606_000 / 35_167_760.0)


def test_old_double_counting_formula_understated_the_share():
    """The regression, stated as the inequality that caused it."""
    boundary, ours = 35_167_760.0, 606_000
    old = ours / (boundary + ours)
    assert old < share(boundary, ours)


def test_sole_voter_gets_the_whole_bribe():
    """If we are the only voter, boundary votes are our votes and share is 1.0.

    The old formula returned 0.5 here — the clearest statement of the bug.
    """
    assert share(50_000.0, 50_000) == pytest.approx(1.0)
    assert 50_000 / (50_000.0 + 50_000) == pytest.approx(0.5)


def test_share_never_exceeds_one():
    """Guards against a stale boundary row reporting fewer votes than we cast."""
    assert share(1_000.0, 5_000) == 1.0


def test_zero_boundary_votes_yields_zero_share():
    assert share(0.0, 1_000) == 0.0


def test_pipeline_uses_inclusive_denominator():
    """The shipped script must not go back to adding our votes a second time."""
    source = PIPELINE.read_text(encoding="utf-8")
    assert "denom = float(boundary_votes_by_gauge.get(gauge_l, 0.0))" in source
    assert "denom = base_votes + float(executed_votes)" not in source


def test_pipeline_heredoc_has_no_bare_dollar_digit():
    """The heredoc is unquoted (<<PY), so `$7` in a comment expands as a shell arg.

    A `$788.57` in a comment broke the whole pipeline with `$7: unbound variable`.
    """
    source = PIPELINE.read_text(encoding="utf-8")
    offenders = [
        line
        for line in source.splitlines()
        if re.search(r"(?<!\\)\$\d", line) and "${" not in line
    ]
    assert offenders == [], f"bare dollar-digit in unquoted heredoc: {offenders}"
