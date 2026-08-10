"""Unit tests for PriceFeed price resolution / sanity checking.

Covers the BETR mispricing regression (epoch 1784160000): the routing feed
valued BETR at 2.0e-6 while the CoinGecko reference read ~1.07e-6 (a 1.87x
divergence, under the old 5x spike guard), inflating a bribe pool's USD by ~2x.

Resolution policy under test:
  1. A *fresh* CoinGecko reference (within PRICE_PREFER_CG_MAX_AGE_SECONDS) is
     preferred outright over the routing quote.
  2. A *recent* (24h) CG reference is preferred once the routing quote diverges
     from it by >= PRICE_PREFER_CG_DIVERGENCE_RATIO, closing the band between
     that ratio and PRICE_SANITY_MAX_SPIKE_RATIO where BETR/REGENT survived.
  3. Otherwise the routing quote is sanity-checked at PRICE_SANITY_MAX_SPIKE_RATIO
     against 24h cg_ref -> wide-window cg_ref -> stored routing price, in that
     order. A prior routing price is only ever the anchor when no CG reference
     exists in any window, since anchoring a routing quote to a previous routing
     quote is self-referential and cannot detect a stable overprice.
  4. With no reference at all, the routing quote passes through unchanged.
"""

import pytest

from src import price_feed as pf_module
from src.price_feed import PriceFeed

BETR = "0x051024b653e8ec69e72693f776c41c2a9401fb07"
REGENT = "0x6f89bca4ea5931edfcb09786267b251dee752b07"
TOK = "0x00000000000000000000000000000000000000aa"


class StubDB:
    """Minimal Database stub exposing only what _sanity_check_prices calls.

    get_cg_ref_price_median serves three windows, mirroring the real
    median-by-age: the "fresh" map at/under fresh_age, the 24h "ref" map, and
    the "wide" map for anything longer (the PRICE_SANITY_LOOKBACK_SECONDS
    anchor lookup). wide defaults to ref when not given.
    """

    def __init__(self, cg_fresh=None, cg_ref=None, routing=None, cg_wide=None,
                 fresh_age=10800, ref_age=86400):
        self._cg_fresh = {k.lower(): v for k, v in (cg_fresh or {}).items()}
        self._cg_ref = {k.lower(): v for k, v in (cg_ref or {}).items()}
        self._routing = {k.lower(): v for k, v in (routing or {}).items()}
        self._cg_wide = {k.lower(): v for k, v in (cg_wide if cg_wide is not None else (cg_ref or {})).items()}
        self._fresh_age = fresh_age
        self._ref_age = ref_age

    def get_cg_ref_price_median(self, token_addresses, max_age_seconds=86400):
        if max_age_seconds <= self._fresh_age:
            src = self._cg_fresh
        elif max_age_seconds <= self._ref_age:
            src = self._cg_ref
        else:
            src = self._cg_wide
        return {t.lower(): src[t.lower()] for t in token_addresses if t.lower() in src}

    def get_batch_token_prices(self, token_addresses, max_age_seconds=3600):
        return {t.lower(): self._routing[t.lower()] for t in token_addresses if t.lower() in self._routing}


@pytest.fixture(autouse=True)
def deterministic_thresholds(monkeypatch):
    """Pin resolution knobs so tests don't drift with config/env changes."""
    monkeypatch.setattr(pf_module, "PRICE_SANITY_MAX_SPIKE_RATIO", 3.0, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_PREFER_CG_MAX_AGE_SECONDS", 10800, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_SANITY_LOOKBACK_SECONDS", 604800, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_DIVERGENCE_LOG_RATIO", 1.5, raising=False)
    monkeypatch.setattr(pf_module, "PRICE_PREFER_CG_DIVERGENCE_RATIO", 1.5, raising=False)


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


def test_betr_band_between_divergence_and_spike_ratio_prefers_cg():
    """BETR regression: routing 2.5x over a 24h cg_ref sits under the 3x spike guard.

    Previously kept the routing quote; must now resolve to the CG reference.
    """
    stub = StubDB(cg_fresh={}, cg_ref={BETR: 8.0e-7})
    out = _feed(stub)._sanity_check_prices({BETR: 2.0e-6})
    assert out[BETR] == pytest.approx(8.0e-7)


def test_regent_persistent_overprice_prefers_cg():
    """REGENT at ~3.1x is the worst persistent offender found in the Aug-7 audit."""
    stub = StubDB(cg_fresh={}, cg_ref={REGENT: 1.0})
    out = _feed(stub)._sanity_check_prices({REGENT: 3.13})
    assert out[REGENT] == pytest.approx(1.0)


def test_routing_kept_when_it_agrees_with_cg_ref():
    """Below the divergence ratio the fresher routing quote is preferred over CG."""
    stub = StubDB(cg_fresh={}, cg_ref={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 1.2})
    assert out[TOK] == pytest.approx(1.2)


def test_stale_cg_anchors_guard_instead_of_prior_routing_price():
    """The self-referential-anchor hole.

    No cg_ref inside 24h, but one exists in the wide window. A routing quote 5x
    over it must clamp to the stale CG price rather than sail through by being
    consistent with its own previous (equally wrong) value.
    """
    stub = StubDB(cg_fresh={}, cg_ref={}, cg_wide={TOK: 1.0}, routing={TOK: 5.0})
    out = _feed(stub)._sanity_check_prices({TOK: 5.0})
    assert out[TOK] == pytest.approx(1.0)


def test_prior_routing_price_is_last_resort_anchor():
    """With no CG reference in any window, the stored routing price still guards."""
    stub = StubDB(cg_fresh={}, cg_ref={}, cg_wide={}, routing={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 10.0})
    assert out[TOK] == pytest.approx(1.0)


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


def test_drop_below_inverse_ratio_is_clamped():
    """A collapse past 1/3x is clamped to the reference, same as a spike."""
    stub = StubDB(cg_fresh={}, cg_ref={}, cg_wide={}, routing={TOK: 9.0})
    out = _feed(stub)._sanity_check_prices({TOK: 1.0})
    assert out[TOK] == pytest.approx(9.0)


def test_zero_price_with_reference_passes_through():
    """A zero/unpriced quote is not clamped up to the reference — it stays zero so
    callers can detect missing coverage rather than silently inheriting a price."""
    stub = StubDB(cg_fresh={}, cg_ref={TOK: 1.0})
    out = _feed(stub)._sanity_check_prices({TOK: 0.0})
    assert out[TOK] == pytest.approx(0.0)
