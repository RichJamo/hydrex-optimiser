"""Unit tests for PriceFeed price resolution / sanity checking.

Covers the BETR mispricing regression (epoch 1784160000): the routing feed
valued BETR at 2.0e-6 while the CoinGecko reference read ~1.07e-6 (a 1.87x
divergence, under the old 5x spike guard), inflating a bribe pool's USD by ~2x.

Resolution policy under test:
  1. A *fresh* CoinGecko reference (within PRICE_PREFER_CG_MAX_AGE_SECONDS) is
     preferred outright over the routing quote.
  2. With no fresh CG ref, the routing quote is sanity-checked against the best
     available reference at PRICE_SANITY_MAX_SPIKE_RATIO (tightened to 3x) and
     clamped to the reference on a spike/drop.
  3. With no reference at all, the routing quote passes through unchanged.
"""

import pytest

from src import price_feed as pf_module
from src.price_feed import PriceFeed

BETR = "0x051024b653e8ec69e72693f776c41c2a9401fb07"
TOK = "0x00000000000000000000000000000000000000aa"


class StubDB:
    """Minimal Database stub exposing only what _sanity_check_prices calls.

    get_cg_ref_price_median returns the "fresh" map when queried with a window
    at/under fresh_age (the prefer-CG lookup) and the broader "ref" map
    otherwise (the guard-reference lookup), mirroring the real median-by-age.
    """

    def __init__(self, cg_fresh=None, cg_ref=None, routing=None, fresh_age=10800):
        self._cg_fresh = {k.lower(): v for k, v in (cg_fresh or {}).items()}
        self._cg_ref = {k.lower(): v for k, v in (cg_ref or {}).items()}
        self._routing = {k.lower(): v for k, v in (routing or {}).items()}
        self._fresh_age = fresh_age

    def get_cg_ref_price_median(self, token_addresses, max_age_seconds=86400):
        src = self._cg_fresh if max_age_seconds <= self._fresh_age else self._cg_ref
        return {t.lower(): src[t.lower()] for t in token_addresses if t.lower() in src}

    def get_batch_token_prices(self, token_addresses, max_age_seconds=3600):
        return {t.lower(): self._routing[t.lower()] for t in token_addresses if t.lower() in self._routing}


@pytest.fixture(autouse=True)
def deterministic_thresholds(monkeypatch):
    """Pin resolution knobs so tests don't drift with config/env changes."""
    monkeypatch.setattr(pf_module, "PRICE_SANITY_MAX_SPIKE_RATIO", 3.0, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_PREFER_CG_MAX_AGE_SECONDS", 10800, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_DIVERGENCE_LOG_RATIO", 1.5, raising=False)


def _feed(stub):
    feed = PriceFeed.__new__(PriceFeed)  # bypass network/constructor
    feed.database = stub
    return feed


def test_fresh_cg_ref_preferred_over_routing_betr_regression():
    """BETR: routing 2.0e-6 must resolve to the fresh CG ref 1.07e-6, not pass through."""
    stub = StubDB(cg_fresh={BETR: 1.07e-6}, cg_ref={BETR: 1.07e-6})
    out = _feed(stub)._sanity_check_prices({BETR: 2.0e-6})
    assert out[BETR] == pytest.approx(1.07e-6)


def test_no_fresh_cg_spike_beyond_tightened_ratio_is_clamped():
    """No fresh CG; routing 4x over the reference exceeds the 3x guard -> clamp to ref."""
    stub = StubDB(cg_fresh={}, cg_ref={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 4.0})
    assert out[TOK] == pytest.approx(1.0)


def test_no_fresh_cg_within_tightened_ratio_passes_through():
    """No fresh CG; routing 2.5x over the reference is within 3x -> keep routing quote."""
    stub = StubDB(cg_fresh={}, cg_ref={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 2.5})
    assert out[TOK] == pytest.approx(2.5)


def test_divergence_is_logged_when_preferring_cg(caplog):
    """A material routing-vs-CG disagreement is logged even when CG is preferred."""
    import logging
    stub = StubDB(cg_fresh={BETR: 1.07e-6}, cg_ref={BETR: 1.07e-6})
    with caplog.at_level(logging.WARNING, logger="src.price_feed"):
        _feed(stub)._sanity_check_prices({BETR: 2.0e-6})
    assert any("Price divergence" in r.message for r in caplog.records)


def test_no_reference_passes_through():
    """Unknown token with no CG or routing reference -> accept the quote as-is."""
    stub = StubDB()
    out = _feed(stub)._sanity_check_prices({TOK: 0.123})
    assert out[TOK] == pytest.approx(0.123)


def test_stale_cg_falls_back_to_routing_guard():
    """CG ref exists but is stale (not in the fresh window); routing quote within 3x of
    the stale CG ref passes through (routing fallback path, not CG-preferred)."""
    stub = StubDB(cg_fresh={}, cg_ref={TOK: 1.0}, routing={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 2.0})
    assert out[TOK] == pytest.approx(2.0)
