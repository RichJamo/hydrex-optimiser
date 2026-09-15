"""Unit tests for the phase-2/3 refresh scope (dormant gauge pruning).

Contract under test (see data/fetchers/refresh_scope.py): a gauge is kept if it has
bribes in the current snapshot, bribes in live or boundary data in the previous N
vote epochs, or was added recently / has no creation date. Pruning fails open when
disabled or when live history is incomplete, and never adds gauges.
"""

import sqlite3

import pytest

from config.settings import WEEK
from data.fetchers.refresh_scope import select_refresh_gauges

VOTE_EPOCH = 1_789_603_200
SNAPSHOT_TS = VOTE_EPOCH + 6 * 86400
NOW = SNAPSHOT_TS
N = 4
OLD = VOTE_EPOCH - 30 * WEEK  # created long ago

DORMANT = "0x" + "d0" * 20
ACTIVE_NOW = "0x" + "a0" * 20
ACTIVE_LIVE = "0x" + "a1" * 20
ACTIVE_BOUNDARY = "0x" + "a2" * 20
YOUNG = "0x" + "a3" * 20
NO_CREATED_AT = "0x" + "a4" * 20
ACTIVE_TOO_LONG_AGO = "0x" + "d1" * 20
ALL = [DORMANT, ACTIVE_NOW, ACTIVE_LIVE, ACTIVE_BOUNDARY, YOUNG, NO_CREATED_AT, ACTIVE_TOO_LONG_AGO]


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.executescript(
        """
        CREATE TABLE gauges (address TEXT PRIMARY KEY, created_at INTEGER);
        CREATE TABLE live_gauge_snapshots (snapshot_ts INTEGER, vote_epoch INTEGER,
            gauge_address TEXT, rewards_normalized_total REAL);
        CREATE TABLE live_reward_token_samples (snapshot_ts INTEGER, vote_epoch INTEGER,
            gauge_address TEXT, rewards_normalized REAL);
        CREATE TABLE boundary_gauge_values (epoch INTEGER, vote_epoch INTEGER,
            gauge_address TEXT, total_usd REAL);
        """
    )
    for g in ALL:
        created = {YOUNG: NOW - WEEK, NO_CREATED_AT: None}.get(g, OLD)
        c.execute("INSERT INTO gauges VALUES (?, ?)", (g, created))
        # current snapshot row for every gauge; only ACTIVE_NOW has bribes
        c.execute("INSERT INTO live_gauge_snapshots VALUES (?, ?, ?, ?)",
                  (SNAPSHOT_TS, VOTE_EPOCH, g, 5.0 if g == ACTIVE_NOW else 0.0))
    # a live snapshot exists for each of the previous N vote epochs
    for i in range(1, N + 1):
        c.execute("INSERT INTO live_gauge_snapshots VALUES (?, ?, ?, 0.0)",
                  (VOTE_EPOCH - i * WEEK + 86400, VOTE_EPOCH - i * WEEK, DORMANT))
    c.execute("INSERT INTO live_reward_token_samples VALUES (1, ?, ?, 2.0)", (VOTE_EPOCH - 3 * WEEK, ACTIVE_LIVE))
    c.execute("INSERT INTO live_reward_token_samples VALUES (1, ?, ?, 0.0)", (VOTE_EPOCH - WEEK, DORMANT))
    c.execute("INSERT INTO boundary_gauge_values VALUES (?, ?, ?, 40.0)", (VOTE_EPOCH, VOTE_EPOCH - WEEK, ACTIVE_BOUNDARY))
    c.execute("INSERT INTO live_reward_token_samples VALUES (1, ?, ?, 9.0)",
              (VOTE_EPOCH - (N + 1) * WEEK, ACTIVE_TOO_LONG_AGO))
    c.commit()
    return c


def _select(conn, lookback=N, gauges=ALL):
    return select_refresh_gauges(conn, SNAPSHOT_TS, VOTE_EPOCH, gauges, lookback, NOW)


def test_keeps_every_active_rule_and_skips_dormant(conn):
    scope = _select(conn)
    assert scope.reason == "pruned"
    assert scope.gauges == {ACTIVE_NOW, ACTIVE_LIVE, ACTIVE_BOUNDARY, YOUNG, NO_CREATED_AT}
    assert scope.skipped == 2


def test_activity_older_than_lookback_does_not_count(conn):
    assert ACTIVE_TOO_LONG_AGO not in _select(conn).gauges
    assert ACTIVE_TOO_LONG_AGO in _select(conn, lookback=N + 1).gauges


def test_gauge_missing_from_gauges_table_is_kept(conn):
    stranger = "0x" + "ee" * 20
    assert stranger in _select(conn, gauges=ALL + [stranger]).gauges


def test_zero_lookback_disables_pruning(conn):
    scope = _select(conn, lookback=0)
    assert scope.gauges == set(ALL)
    assert scope.reason == "disabled"


def test_missing_live_history_fails_open(conn):
    conn.execute("DELETE FROM live_gauge_snapshots WHERE vote_epoch = ?", (VOTE_EPOCH - 2 * WEEK,))
    scope = _select(conn)
    assert scope.gauges == set(ALL)
    assert scope.skipped == 0
    assert str(VOTE_EPOCH - 2 * WEEK) in scope.reason


def test_without_boundary_table_live_data_still_decides(conn):
    conn.execute("DROP TABLE boundary_gauge_values")
    scope = _select(conn)
    assert ACTIVE_BOUNDARY not in scope.gauges
    assert ACTIVE_LIVE in scope.gauges


def test_never_adds_gauges_outside_the_input(conn):
    subset = [DORMANT, ACTIVE_NOW]
    assert _select(conn, gauges=subset).gauges == {ACTIVE_NOW}


def test_addresses_are_matched_case_insensitively(conn):
    scope = _select(conn, gauges=[ACTIVE_NOW.upper().replace("0X", "0x"), DORMANT])
    assert scope.gauges == {ACTIVE_NOW}
