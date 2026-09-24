"""The post-mortem fills missing token symbols itself, and can touch nothing else.

Found in review 2026-09-18. The reconciliation joins expected tokens to the
actual-rewards JSON by ticker, so a reward token with no symbol in token_metadata splits
into a phantom +/- pair. Epoch 1789603200's five largest apparent misses were all this.
The first fix backfilled the 112 missing symbols by hand; the next new reward token would
have brought the problem straight back.

run_postmortem_review.py now runs `repair_token_metadata.py --backfill-only` before the
pipeline. Running something on every post-mortem is only safe if it is narrow, so:

Contract under test (--backfill-only):
  - fills a missing symbol from chain, and never changes an existing one;
  - never changes existing decimals, even when the chain disagrees — the Aug-2026 decimals
    repair must survive;
  - does not re-apply the curated JSON maps or hard overrides;
  - covers reward tokens that have no token_metadata row yet;
  - leaves an unreadable token's symbol empty rather than guessing;
  - queries the chain only for rows that need it.
"""

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPAIR = ROOT / "scripts" / "repair_token_metadata.py"
WRAPPER = ROOT / "scripts" / "run_postmortem_review.py"

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # has a hard override in the script
NOCK = "0x9b5e262cf9bb04869ab40b19af91d2dc85761722"  # real decimals 16
NEW_TOKEN = "0x00000000000000000000000000000000000000a1"
BROKEN = "0x00000000000000000000000000000000000000b2"

CHAIN = {
    NOCK: ("NOCK", 18),  # chain "says" 18 here, to prove stored 16 wins
    NEW_TOKEN: ("NEWT", 9),
}


@pytest.fixture
def repair(monkeypatch):
    spec = importlib.util.spec_from_file_location("repair_token_metadata", REPAIR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    queried = []

    def fake_backfill(addresses):
        queried.extend(addresses)
        resolved = {a: CHAIN[a] for a in addresses if a in CHAIN}
        failed = [a for a in addresses if a not in CHAIN]
        return resolved, failed

    monkeypatch.setattr(module, "backfill_symbols_onchain", fake_backfill)
    module.queried = queried
    return module


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE token_metadata (token_address TEXT PRIMARY KEY, symbol TEXT, "
        "decimals INTEGER, updated_at INTEGER)"
    )
    conn.execute("CREATE TABLE boundary_reward_snapshots (reward_token TEXT)")
    conn.executemany(
        "INSERT INTO token_metadata VALUES (?, ?, ?, 0)",
        [
            (USDC, "usdc-local-label", 18),  # wrong vs the hard override, on purpose
            (NOCK, None, 16),
            (BROKEN, None, 18),
        ],
    )
    conn.executemany(
        "INSERT INTO boundary_reward_snapshots VALUES (?)",
        [(NOCK,), (NEW_TOKEN,), (USDC,)],
    )
    conn.commit()
    conn.close()
    return path


def _run(repair, monkeypatch, db, *extra):
    monkeypatch.setattr(
        sys, "argv", ["repair", "--database", str(db), "--backfill-only", *extra]
    )
    repair.main()
    conn = sqlite3.connect(db)
    rows = {
        r[0]: (r[1], r[2])
        for r in conn.execute(
            "SELECT token_address, symbol, decimals FROM token_metadata"
        )
    }
    conn.close()
    return rows


def test_fills_a_missing_symbol_and_keeps_stored_decimals(repair, monkeypatch, db):
    rows = _run(repair, monkeypatch, db)
    assert rows[NOCK] == ("NOCK", 16), "stored decimals must beat the chain value"


def test_never_reapplies_curated_overrides(repair, monkeypatch, db):
    """USDC has a hard override (USDC, 6); --backfill-only must leave the row alone."""
    rows = _run(repair, monkeypatch, db)
    assert rows[USDC] == ("usdc-local-label", 18)


def test_covers_reward_tokens_with_no_metadata_row(repair, monkeypatch, db):
    rows = _run(repair, monkeypatch, db)
    assert rows[NEW_TOKEN] == ("NEWT", 9)


def test_unreadable_symbol_is_left_empty_not_guessed(repair, monkeypatch, db):
    rows = _run(repair, monkeypatch, db)
    assert rows[BROKEN][0] in (None, "")
    assert rows[BROKEN][1] == 18


def test_chain_is_queried_only_for_rows_that_need_it(repair, monkeypatch, db):
    _run(repair, monkeypatch, db)
    assert USDC not in repair.queried, "a row with a symbol must not cost an RPC call"
    assert set(repair.queried) == {NOCK, NEW_TOKEN, BROKEN}


def test_second_run_is_a_no_op(repair, monkeypatch, db):
    """Safe to run every post-mortem: once filled, nothing is queried again."""
    first = _run(repair, monkeypatch, db)
    repair.queried.clear()
    second = _run(repair, monkeypatch, db)
    assert second == first
    assert repair.queried == [BROKEN], "only the genuinely unreadable token is retried"


def test_wrapper_backfills_before_the_pipeline_unless_disabled():
    """The pipeline reads token_metadata, so the backfill must precede it."""
    source = WRAPPER.read_text(encoding="utf-8")
    backfill_at = source.index('"--backfill-only"')
    pipeline_at = source.index("run_preboundary_analysis_pipeline.sh")
    assert backfill_at < pipeline_at
    assert "if not args.no_symbol_backfill:" in source


def _db_with_snapshots_table(tmp_path, snapshots_ddl):
    path = tmp_path / "data.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE token_metadata (token_address TEXT PRIMARY KEY, symbol TEXT, "
        "decimals INTEGER, updated_at INTEGER)"
    )
    if snapshots_ddl:
        conn.execute(snapshots_ddl)
    conn.commit()
    conn.close()
    return path


def test_unreadable_snapshots_table_warns(repair, monkeypatch, tmp_path, capsys):
    """Found in review: a bare `except: pass` hid any failure here, silently dropping
    reward-token coverage for the run. A table that exists but cannot be read must say so.
    """
    path = _db_with_snapshots_table(
        tmp_path, "CREATE TABLE boundary_reward_snapshots (not_reward_token TEXT)"
    )
    _run(repair, monkeypatch, path)
    assert "WARNING: could not read reward tokens" in capsys.readouterr().out


def test_missing_snapshots_table_is_quiet(repair, monkeypatch, tmp_path, capsys):
    """A fresh DB has no snapshots table yet; nothing is lost, so no warning."""
    path = _db_with_snapshots_table(tmp_path, None)
    _run(repair, monkeypatch, path)
    assert "WARNING" not in capsys.readouterr().out
