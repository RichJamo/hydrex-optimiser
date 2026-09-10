"""Reward-token discovery window for the cg_ref producer.

Regression cover for the Sep 2026 finding that `scripts/fetch_cg_ref_prices.py` only ever
asked CoinGecko about the *current* epoch's reward tokens. A token that bribed recently but
not in the anchor epoch therefore never acquired a cg_ref row, and the sanity guard in
price_feed.py fell back to anchoring on the routing feed's own previous value. That anchor is
self-referential: every corrected reading sits more than PRICE_SANITY_MAX_SPIKE_RATIO from the
bad value and is rejected, so a single bad quote locks itself in permanently.

Two live examples at the time of writing: AZUSD sat at $0.748924 against a realisable
$0.0932 (8x) and RIZE at $0.004516 against a realisable $0.00098 (4.6x). Neither could
self-correct while it had no cg_ref to appeal to.

These tests exercise discovery only. Whether CoinGecko actually lists a given token is a
separate axis -- AZUSD is not listed at all, so a wider window cannot help it, and it needed
its stored anchor corrected by hand.
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "fetch_cg_ref_prices.py"

ANCHOR = 1788998400          # 2026-09-10
WEEK = 604800
PREV = ANCHOR - WEEK
OLD = ANCHOR - 3 * WEEK
ANCIENT = ANCHOR - 20 * WEEK
LATER = ANCHOR + WEEK

TOK_ANCHOR = "0xaaaa000000000000000000000000000000000001"
TOK_PREV = "0xbbbb000000000000000000000000000000000002"
TOK_OLD = "0xcccc000000000000000000000000000000000003"
TOK_ANCIENT = "0xdddd000000000000000000000000000000000004"
TOK_LATER = "0xeeee000000000000000000000000000000000005"
TOK_INACTIVE = "0xffff000000000000000000000000000000000006"


@pytest.fixture()
def mod(tmp_path):
    """Load the script against a throwaway DB seeded with a known epoch spread."""
    db = tmp_path / "t.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE boundary_reward_snapshots ("
        "epoch INTEGER, reward_token TEXT, active_only INTEGER)"
    )
    conn.executemany(
        "INSERT INTO boundary_reward_snapshots VALUES (?,?,?)",
        [
            (ANCHOR, TOK_ANCHOR, 1),
            (ANCHOR, TOK_ANCHOR.upper(), 1),   # dedupe + lowercase
            (PREV, TOK_PREV, 1),
            (OLD, TOK_OLD, 1),
            (ANCIENT, TOK_ANCIENT, 1),
            (LATER, TOK_LATER, 1),             # must never leak in via an explicit anchor
            (ANCHOR, TOK_INACTIVE, 0),         # active_only=0 stays excluded
        ],
    )
    conn.commit()
    conn.close()

    spec = importlib.util.spec_from_file_location("fetch_cg_ref_prices", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.DATABASE_PATH = str(db)
    return m


def test_lookback_one_is_current_epoch_only(mod):
    """The historical behaviour must survive exactly, so the flag is a safe opt-in."""
    got = mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=1)
    assert got == [TOK_ANCHOR]


def test_window_reaches_back_and_stops(mod):
    """lookback=2 must pull in the previous epoch's token and nothing older.

    This is the assertion that would fail if the window were ignored: TOK_PREV is absent
    from the anchor epoch and is exactly the class of token that was silently skipped.
    """
    got = set(mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=2))
    assert got == {TOK_ANCHOR, TOK_PREV}
    assert TOK_OLD not in got


def test_window_is_monotonic_and_eventually_covers_all_past(mod):
    prev = set()
    for n in (1, 2, 4, 21):
        got = set(mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=n))
        assert prev <= got, f"lookback={n} dropped tokens a smaller window returned"
        prev = got
    assert prev == {TOK_ANCHOR, TOK_PREV, TOK_OLD, TOK_ANCIENT}


def test_lookback_zero_covers_every_past_epoch(mod):
    """0 is the sentinel for an unbounded window, and must still respect the anchor."""
    got = set(mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=0))
    assert got == {TOK_ANCHOR, TOK_PREV, TOK_OLD, TOK_ANCIENT}
    assert TOK_LATER not in got


def test_shipped_default_reaches_dormant_tokens(mod):
    """The default must cover tokens dormant for months, not just a rolling window.

    Those carry the stalest anchors and are the ones the guard can never unstick: RIZE and
    SPX last bribed 5-7 months before this was written, so any bounded window misses exactly
    the population the change exists to protect.
    """
    assert mod.DEFAULT_LOOKBACK_EPOCHS == 0
    got = set(mod.discover_reward_tokens(None, epoch=ANCHOR))
    assert TOK_ANCIENT in got, "default must reach a token 20 epochs back"


def test_anchor_excludes_later_epochs(mod):
    """An explicit --epoch must not sweep in epochs that closed after it."""
    got = set(mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=21))
    assert TOK_LATER not in got


def test_inactive_rows_excluded(mod):
    got = set(mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=21))
    assert TOK_INACTIVE not in got


def test_default_anchor_is_max_epoch(mod):
    """With no epoch given the window must anchor on the newest epoch present."""
    got = set(mod.discover_reward_tokens(None, lookback_epochs=1))
    assert got == {TOK_LATER}


def test_negative_lookback_rejected(mod):
    with pytest.raises(ValueError):
        mod.discover_reward_tokens(None, epoch=ANCHOR, lookback_epochs=-1)


def test_empty_table_returns_empty(tmp_path, mod):
    empty = tmp_path / "empty.db"
    conn = sqlite3.connect(empty)
    conn.execute(
        "CREATE TABLE boundary_reward_snapshots ("
        "epoch INTEGER, reward_token TEXT, active_only INTEGER)"
    )
    conn.commit()
    conn.close()
    mod.DATABASE_PATH = str(empty)
    assert mod.discover_reward_tokens(None) == []
