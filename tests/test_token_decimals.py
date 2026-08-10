"""Unit tests for on-chain decimals resolution in the live snapshot fetcher.

Regression cover for the Aug-2026 defect: 12 reward tokens sat in token_metadata
with decimals=18 despite being 2/6/8/9-decimal (cbBTC 8, cbXRP/EURC 6, IDRX 2,
SOL 9, ...). Callers normalise as raw / 10**decimals.get(token, 18), so an
unresolved token is silently valued 10^10-10^16 too low and disappears from the
optimizer's view.

Policy under test:
  1. Cached decimals are used as-is.
  2. Anything uncached is resolved on-chain and persisted.
  3. A token whose decimals() call fails is left OUT of both the map and the
     cache, so a later run retries it rather than freezing it at 18.
"""

import sqlite3

import pytest

from data.fetchers import fetch_live_snapshot as fls

CBBTC = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
BROKEN = "0x00000000000000000000000000000000000000ff"


class StubContract:
    def __init__(self, decimals):
        self._decimals = decimals

    class _Fn:
        def __init__(self, value):
            self._value = value

        def call(self):
            if isinstance(self._value, Exception):
                raise self._value
            return self._value

    @property
    def functions(self):
        outer = self

        class _F:
            def decimals(self_inner):
                return StubContract._Fn(outer._decimals)

        return _F()


class StubEth:
    def __init__(self, chain):
        self._chain = chain

    def contract(self, address=None, abi=None):
        key = str(address).lower()
        if key not in self._chain:
            raise ValueError(f"no such token {key}")
        return StubContract(self._chain[key])


class StubW3:
    """Minimal Web3 stand-in; `chain` maps address -> decimals or Exception."""

    def __init__(self, chain):
        self.eth = StubEth({k.lower(): v for k, v in chain.items()})


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        """CREATE TABLE token_metadata (
               token_address VARCHAR NOT NULL,
               symbol VARCHAR,
               decimals INTEGER,
               updated_at INTEGER,
               PRIMARY KEY (token_address)
           )"""
    )
    return c


def _cached(conn, addr):
    row = conn.execute(
        "SELECT decimals FROM token_metadata WHERE token_address = ?", (addr,)
    ).fetchone()
    return row[0] if row else None


def test_cached_decimals_are_used(conn):
    conn.execute(
        "INSERT INTO token_metadata (token_address, decimals) VALUES (?, ?)", (USDC, 6)
    )
    out = fls.load_token_decimals(conn, {USDC}, w3=StubW3({}))
    assert out[USDC] == 6


def test_uncached_token_is_resolved_onchain_and_persisted(conn):
    """cbBTC regression: must come back as 8 and be written to the cache."""
    w3 = StubW3({CBBTC: 8})
    out = fls.load_token_decimals(conn, {CBBTC}, w3=w3)
    assert out[CBBTC] == 8
    assert _cached(conn, CBBTC) == 8


def test_failed_resolution_is_not_cached_as_18(conn):
    """A transient decimals() failure must not freeze the token at 18.

    This is the root cause of the original defect: persisting the fallback makes
    one bad RPC call permanent.
    """
    w3 = StubW3({BROKEN: RuntimeError("execution reverted")})
    out = fls.load_token_decimals(conn, {BROKEN}, w3=w3)
    assert BROKEN not in out
    assert _cached(conn, BROKEN) is None


def test_existing_wrong_cache_value_is_not_silently_trusted_when_absent(conn):
    """Only genuinely uncached tokens are resolved; a NULL decimals row counts as uncached."""
    conn.execute(
        "INSERT INTO token_metadata (token_address, decimals) VALUES (?, NULL)", (CBBTC,)
    )
    out = fls.load_token_decimals(conn, {CBBTC}, w3=StubW3({CBBTC: 8}))
    assert out[CBBTC] == 8
    assert _cached(conn, CBBTC) == 8


def test_mixed_batch_resolves_only_the_gaps(conn):
    conn.execute(
        "INSERT INTO token_metadata (token_address, decimals) VALUES (?, ?)", (USDC, 6)
    )
    w3 = StubW3({CBBTC: 8, BROKEN: RuntimeError("boom")})
    out = fls.load_token_decimals(conn, {USDC, CBBTC, BROKEN}, w3=w3)
    assert out == {USDC: 6, CBBTC: 8}


def test_no_w3_returns_cache_only_without_crashing(conn):
    """Back-compat: callers without an RPC handle still get the cached subset."""
    conn.execute(
        "INSERT INTO token_metadata (token_address, decimals) VALUES (?, ?)", (USDC, 6)
    )
    out = fls.load_token_decimals(conn, {USDC, CBBTC}, w3=None)
    assert out == {USDC: 6}


def test_empty_token_set_is_a_noop(conn):
    assert fls.load_token_decimals(conn, set(), w3=None) == {}
