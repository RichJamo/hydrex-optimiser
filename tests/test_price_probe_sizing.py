"""Unit tests for routing price-probe sizing.

Covers the LAOD mispricing regression (epoch 1787184000): the feed quoted every
token at exactly 1 whole token, so `amountOut` came back in USDC's 6-decimal raw
units. For a sub-cent token that is a handful of raw units and the implied price is
rounding noise, not a market price. LAOD read $0.000187 that way against a
realisable $1.75e-07 -- 1,067x high -- and the optimizer paid 51,000 votes for a
bribe worth $0.0149. SPLASH read 523x high; AYB, BAES and "i" returned amountOut=0
and failed the fetch outright.

Sizing policy under test:
  1. Pass one is always exactly 1 whole token, whatever the token's history.
  2. A quote whose output lands under HYDREX_ROUTING_QUOTE_MIN_USDC_RAW is re-probed
     at fixed multiples of a whole token until the output clears the floor.
  3. Probe sizes are constants and are never derived from a previously-computed price.
     An earlier revision sized each probe at a target USD notional using the last
     answer; for a pool too thin to fill that size the new lower price enlarged the
     next probe, which lowered the price again. Measured on LAOD, that loop fell ~14x
     per run and would have reached zero. test_price_is_stable_across_runs pins this.
  4. Probing stops once the implied notional exceeds HYDREX_ROUTING_QUOTE_MAX_USD,
     because bribe positions run from a few dollars to rarely over $100 and depth
     beyond that is impact we will never actually pay.
"""

import pytest

from src.price_feed import PriceFeed

USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
LAOD = "0xc3679395bddfb080fed2e26a54ab224dc582c99a"
KING = "0xe22c243c7559c667a1eb94b593369d192c5fbac0"


@pytest.fixture
def feed():
    pf = PriceFeed()
    pf.database = None  # keep _price_hint off the DB; tests pass hints explicitly
    pf.routing_quote_target_usd = 25.0
    pf.routing_quote_max_usd = 250.0
    pf.routing_quote_min_usdc_raw = 1000
    return pf


def test_first_pass_is_always_one_whole_token(feed):
    swap = feed._build_price_probe_swap(LAOD, 18)
    assert swap["amount"] == str(10**18)
    assert swap["fromTokenAddress"] == LAOD
    assert swap["toTokenAddress"] == USDC


def test_first_pass_uses_the_tokens_own_decimals(feed):
    assert feed._build_price_probe_swap(USDC, 6)["amount"] == str(10**6)


def test_first_pass_ignores_any_stored_price(feed):
    """The regression guard: a cached price must not influence the probe size."""
    feed.cache[LAOD] = (1.75e-07, 1_000_000_000.0)
    assert feed._build_price_probe_swap(LAOD, 18)["amount"] == str(10**18)


def test_growth_multipliers_are_constant_and_ascending(feed):
    assert feed.PROBE_GROWTH_MULTIPLIERS == tuple(sorted(feed.PROBE_GROWTH_MULTIPLIERS))
    assert all(isinstance(m, int) and m > 1 for m in feed.PROBE_GROWTH_MULTIPLIERS)


def _stub_routing(feed, monkeypatch, responder):
    """Route every /quote/multi call through `responder(swaps) -> legs`."""
    calls = []

    def fake_request(request_fn, request_label, item_count):
        # request_fn closes over the chunk; capture it by invoking the recorder instead.
        raise AssertionError("unused")

    def fake_post(url, json=None, headers=None, timeout=None):
        swaps = json["swaps"]
        calls.append(swaps)

        class R:
            status_code = 200

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {"swaps": responder(swaps)}

        return R()

    monkeypatch.setattr("src.price_feed.requests.post", fake_post)
    monkeypatch.setattr(PriceFeed, "_get_token_decimals", lambda self, a: 18)
    return calls


ONE = 10**18


def _laod_responder(swaps):
    """A LAOD-like pool: 1 token quantises to 187 raw units, larger probes are honest."""
    legs = []
    for s in swaps:
        amt = int(s["amount"])
        out = 187 if amt == ONE else int(amt / ONE * 1.75e-07 * 10**6)
        legs.append({"fromTokenAddress": s["fromTokenAddress"],
                     "amountIn": str(amt), "amountOut": str(out)})
    return legs


def test_tiny_output_escalates_the_ladder_until_the_floor_is_cleared(feed, monkeypatch):
    """The LAOD case end to end.

    1 token quantises to 187 raw USDC units -> $0.000187, which is noise. 1e3 tokens
    still only returns 175 raw units, under the 1000 floor, so the ladder escalates
    again to 1e6 tokens where the output is large enough to divide meaningfully.
    """
    calls = _stub_routing(feed, monkeypatch, _laod_responder)
    prices = feed._fetch_prices_via_hydrex_routing([LAOD])

    sizes = [int(c[0]["amount"]) for c in calls]
    assert sizes == [ONE, 10**3 * ONE, 10**6 * ONE], "ladder should escalate until filled"
    assert prices[LAOD] == pytest.approx(1.75e-07, rel=1e-3)
    # The bug being fixed: the 1-token read was ~1067x the realisable price.
    assert 0.000187 / prices[LAOD] > 1000


def test_price_is_stable_across_runs(feed, monkeypatch):
    """Regression guard for the feedback loop.

    Feeding each run's answer back as the next run's cached price must not move the
    result. The earlier price-derived sizing failed exactly here: LAOD fell ~14x per
    run because a lower price enlarged the next probe, which lowered the price again.
    """
    import time as _time

    results = []
    for _ in range(4):
        _stub_routing(feed, monkeypatch, _laod_responder)
        price = feed._fetch_prices_via_hydrex_routing([LAOD])[LAOD]
        results.append(price)
        feed.cache[LAOD] = (price, _time.time())  # feed the answer back

    assert len(set(results)) == 1, f"price drifted across runs: {results}"


def test_healthy_first_pass_output_is_not_reprobed(feed, monkeypatch):
    """A token with a meaningful first-pass output must cost only one pass."""
    ONE = 10**18

    def responder(swaps):
        return [{"fromTokenAddress": s["fromTokenAddress"],
                 "amountIn": s["amount"],
                 "amountOut": str(int(int(s["amount"]) / ONE * 1_000_000))}  # $1.00/token
                for s in swaps]

    calls = _stub_routing(feed, monkeypatch, responder)
    prices = feed._fetch_prices_via_hydrex_routing([LAOD])

    assert len(calls) == 1, "healthy quote should not be re-probed"
    assert prices[LAOD] == pytest.approx(1.0, rel=1e-6)
