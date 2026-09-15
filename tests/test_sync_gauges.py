"""Unit tests for the gauge universe sync.

Regression cover for the Sep-2026 defect: `gauges` / `gauge_bribe_mapping` were
populated once (Feb 2026) and never refreshed, so 123 of 414 on-chain gauges never
reached the live snapshot, the optimizer, or the postmortem.

Contract under test (see data/fetchers/sync_gauges.py):
  1. Gauges on-chain but not in the DB are added to gauges, gauge_bribe_mapping
     and bribe_reward_tokens, with a real is_alive value.
  2. Existing rows are never modified.
  3. A partial read writes nothing for that gauge, and a later run retries it.
  4. Known gauges without a mapping row are backfilled.
  5. dry_run writes nothing; a second run is a no-op.
"""

import sqlite3

import pytest

from data.fetchers import sync_gauges as sg
from data.fetchers.fetch_epoch_bribes_multicall import load_pairs_from_bribe_reward_tokens

ZERO = sg.ZERO_ADDRESS
POOL_OLD, GAUGE_OLD = "0x" + "a1" * 20, "0x" + "b1" * 20
POOL_NEW, GAUGE_NEW = "0x" + "a2" * 20, "0x" + "b2" * 20
POOL_KILLED, GAUGE_KILLED = "0x" + "a3" * 20, "0x" + "b3" * 20
IB_NEW, EB_NEW = "0x" + "c2" * 20, "0x" + "d2" * 20
IB_KILLED = "0x" + "c3" * 20
USDC, OHYDX = "0x" + "e1" * 20, "0x" + "e2" * 20


class FakeReader:
    """Chain stand-in. Set an entry to None to simulate a failed call."""

    def __init__(self):
        self.pool_list = [POOL_OLD, POOL_NEW, POOL_KILLED]
        self.gauge_of = {POOL_OLD: GAUGE_OLD, POOL_NEW: GAUGE_NEW, POOL_KILLED: GAUGE_KILLED}
        self.bribes = {
            ("internal", GAUGE_NEW): IB_NEW,
            ("external", GAUGE_NEW): EB_NEW,
            ("alive", GAUGE_NEW): True,
            ("internal", GAUGE_KILLED): IB_KILLED,
            ("external", GAUGE_KILLED): ZERO,
            ("alive", GAUGE_KILLED): False,
        }
        self.tokens = {IB_NEW: [USDC], EB_NEW: [USDC, OHYDX], IB_KILLED: []}
        self.bribe_queries = []

    def pool_count(self):
        return len(self.pool_list)

    def pools(self, count):
        return {i: p for i, p in enumerate(self.pool_list[:count])}

    def gauges_for_pools(self, pools):
        return {p: self.gauge_of.get(p) for p in pools}

    def bribes_for_gauges(self, gauges):
        self.bribe_queries.append(list(gauges))
        out = {}
        for g in gauges:
            for kind in ("internal", "external", "alive"):
                out[(kind, g)] = self.bribes.get((kind, g))
        return out

    def reward_tokens_for_bribes(self, bribes):
        return {b: self.tokens.get(b) for b in bribes}


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    sg.ensure_gauge_tables(c)
    c.execute(
        "INSERT INTO gauges (address, pool, internal_bribe, external_bribe, is_alive, created_at, last_updated) "
        "VALUES (?, ?, ?, ?, 1, 100, 100)",
        (GAUGE_OLD, POOL_OLD, "0x" + "c1" * 20, "0x" + "d1" * 20),
    )
    c.execute(
        "INSERT INTO gauge_bribe_mapping VALUES (?, ?, ?, 100)",
        (GAUGE_OLD, "0x" + "c1" * 20, "0x" + "d1" * 20),
    )
    c.commit()
    return c


def _gauge(conn, address):
    return conn.execute(
        "SELECT pool, internal_bribe, external_bribe, is_alive, created_at FROM gauges WHERE address = ?",
        (address,),
    ).fetchone()


def _mapping(conn, address):
    return conn.execute(
        "SELECT internal_bribe, external_bribe FROM gauge_bribe_mapping WHERE gauge_address = ?",
        (address,),
    ).fetchone()


def _pairs(conn):
    return set(conn.execute("SELECT bribe_contract, reward_token FROM bribe_reward_tokens").fetchall())


def test_new_gauge_is_added_to_all_three_tables(conn):
    result = sg.sync_new_gauges(conn, FakeReader(), now_ts=500)

    assert result.on_chain_pools == 3
    assert result.known_gauges == 1
    assert sorted(result.new_gauges) == sorted([(GAUGE_NEW, POOL_NEW), (GAUGE_KILLED, POOL_KILLED)])
    assert _gauge(conn, GAUGE_NEW) == (POOL_NEW, IB_NEW, EB_NEW, 1, 500)
    assert _mapping(conn, GAUGE_NEW) == (IB_NEW, EB_NEW)
    assert {(IB_NEW, USDC), (EB_NEW, USDC), (EB_NEW, OHYDX)} <= _pairs(conn)
    assert result.reward_pairs_added == 3


def test_new_pairs_are_visible_to_the_snapshot_pair_loader(conn):
    """End of the chain that was broken: pick_pairs reads bribe_reward_tokens."""
    sg.sync_new_gauges(conn, FakeReader(), now_ts=500)
    pairs = load_pairs_from_bribe_reward_tokens(conn, {IB_NEW, EB_NEW})
    assert set(pairs) == {(IB_NEW, USDC), (EB_NEW, USDC), (EB_NEW, OHYDX)}


def test_killed_gauge_stores_real_alive_flag_and_blank_zero_bribe(conn):
    sg.sync_new_gauges(conn, FakeReader(), now_ts=500)
    pool, ib, eb, alive, _ = _gauge(conn, GAUGE_KILLED)
    assert (pool, ib, eb, alive) == (POOL_KILLED, IB_KILLED, "", 0)


def test_only_unknown_gauges_are_queried_and_existing_rows_untouched(conn):
    reader = FakeReader()
    sg.sync_new_gauges(conn, reader, now_ts=500)
    assert GAUGE_OLD not in reader.bribe_queries[0]
    assert _gauge(conn, GAUGE_OLD) == (POOL_OLD, "0x" + "c1" * 20, "0x" + "d1" * 20, 1, 100)


@pytest.mark.parametrize("failed_key", [("internal", GAUGE_NEW), ("external", GAUGE_NEW), ("alive", GAUGE_NEW)])
def test_failed_gauge_read_writes_nothing_then_retries(conn, failed_key):
    reader = FakeReader()
    reader.bribes[failed_key] = None
    first = sg.sync_new_gauges(conn, reader, now_ts=500)

    assert first.skipped_incomplete == 1
    assert _gauge(conn, GAUGE_NEW) is None
    assert _mapping(conn, GAUGE_NEW) is None
    assert not any(b in (IB_NEW, EB_NEW) for b, _ in _pairs(conn))

    reader.bribes[failed_key] = {"internal": IB_NEW, "external": EB_NEW, "alive": True}[failed_key[0]]
    second = sg.sync_new_gauges(conn, reader, now_ts=600)
    assert second.new_gauges == [(GAUGE_NEW, POOL_NEW)]
    assert _gauge(conn, GAUGE_NEW)[4] == 600


def test_unreadable_reward_token_list_blocks_the_gauge(conn):
    """Writing the gauge without its pairs would hide its bribes permanently."""
    reader = FakeReader()
    reader.tokens[EB_NEW] = None
    result = sg.sync_new_gauges(conn, reader, now_ts=500)
    assert _gauge(conn, GAUGE_NEW) is None
    assert result.skipped_incomplete == 1


def test_unreadable_pool_gauge_lookup_is_counted_not_written(conn):
    reader = FakeReader()
    reader.gauge_of[POOL_NEW] = None
    result = sg.sync_new_gauges(conn, reader, now_ts=500)
    assert _gauge(conn, GAUGE_NEW) is None
    assert result.skipped_incomplete == 1


def test_known_gauge_without_mapping_is_backfilled(conn):
    lonely = "0x" + "f1" * 20
    conn.execute(
        "INSERT INTO gauges (address, pool, internal_bribe, external_bribe, is_alive) VALUES (?, ?, ?, ?, 1)",
        (lonely, "0x" + "f2" * 20, "0x" + "F3" * 20, None),
    )
    conn.commit()
    reader = FakeReader()
    reader.pool_list.append("0x" + "f2" * 20)
    reader.gauge_of["0x" + "f2" * 20] = lonely

    result = sg.sync_new_gauges(conn, reader, now_ts=500)
    assert result.mappings_backfilled == 1
    assert _mapping(conn, lonely) == ("0x" + "f3" * 20, "")


def test_dry_run_writes_nothing(conn):
    result = sg.sync_new_gauges(conn, FakeReader(), dry_run=True, now_ts=500)
    assert len(result.new_gauges) == 2
    assert _gauge(conn, GAUGE_NEW) is None
    assert _pairs(conn) == set()


def test_second_run_is_a_noop(conn):
    sg.sync_new_gauges(conn, FakeReader(), now_ts=500)
    reader = FakeReader()
    again = sg.sync_new_gauges(conn, reader, now_ts=600)
    assert again.new_gauges == []
    assert again.reward_pairs_added == 0
    assert reader.bribe_queries == []
    assert _gauge(conn, GAUGE_NEW)[4] == 500
