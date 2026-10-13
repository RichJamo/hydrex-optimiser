"""The README's results table must have a row for every reviewed epoch.

The table is the first thing a visitor reads, and it is updated by hand each Thursday
after the post-mortem. A hand step gets skipped, and a table that stops in the past
makes the project look abandoned. This test makes the skip visible: committing an epoch
record with a completed review, without its README row, fails CI.

Contract under test: for every data/epochs/actual_rewards_epoch_<ts>.json from the
first like-for-like review (2026-09-17) onwards whose ``postmortem`` block has a
``boundary_opt_expected_usd``, README.md has a table row that starts with the epoch's
date and shows the rewards received and the hindsight-best figure.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIRST_LIKE_FOR_LIKE_EPOCH = 1789603200  # 2026-09-17; earlier reviews are not comparable


def _reviewed_epochs():
    records = []
    for path in sorted((ROOT / "data" / "epochs").glob("actual_rewards_epoch_*.json")):
        record = json.loads(path.read_text())
        review = record.get("postmortem") or {}
        if (
            record["epoch"] >= FIRST_LIKE_FOR_LIKE_EPOCH
            and "boundary_opt_expected_usd" in review
        ):
            records.append(
                (
                    record["epoch"],
                    record["total_usd"],
                    review["boundary_opt_expected_usd"],
                )
            )
    return records


def test_there_are_reviewed_epochs_to_check():
    assert len(_reviewed_epochs()) >= 4


@pytest.mark.parametrize("epoch, received_usd, best_usd", _reviewed_epochs())
def test_reviewed_epoch_has_a_readme_row(epoch, received_usd, best_usd):
    date = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    rows = [
        line
        for line in (ROOT / "README.md").read_text().splitlines()
        if line.startswith(f"| {date} |")
    ]

    assert len(rows) == 1, f"README results table needs exactly one row for {date}"
    assert f"${received_usd:,.2f}" in rows[0]
    assert f"${best_usd:,.2f}" in rows[0]
