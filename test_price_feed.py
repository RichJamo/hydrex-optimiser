import unittest
from unittest.mock import patch

import requests

from src.price_feed import PriceFeed


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            error = requests.HTTPError(f"status={self.status_code}")
            error.response = self
            raise error

    def json(self):
        return self._payload


class PriceFeedFallbackModeTest(unittest.TestCase):
    def test_batch_fetch_skips_coingecko_when_fallback_disabled(self):
        token = "0x1111111111111111111111111111111111111111"
        price_feed = PriceFeed(database=None, allow_coingecko_fallback=False)
        price_feed._fetch_prices_via_hydrex_routing = lambda addresses: {}

        def unexpected_coingecko(*args, **kwargs):
            raise AssertionError("CoinGecko fallback should be disabled for batch refresh")

        price_feed._coingecko_get = unexpected_coingecko

        prices = price_feed.fetch_batch_prices_by_address([token])

        self.assertEqual(prices, {})

    def test_single_token_fetch_returns_none_when_routing_misses_and_fallback_disabled(self):
        token = "0x1111111111111111111111111111111111111111"
        price_feed = PriceFeed(database=None, allow_coingecko_fallback=False)
        price_feed._fetch_prices_via_hydrex_routing = lambda addresses: {}

        def unexpected_coingecko(*args, **kwargs):
            raise AssertionError("CoinGecko fallback should be disabled for single-token fetch")

        price_feed._fetch_price_by_id = unexpected_coingecko
        price_feed._fetch_price_by_address = unexpected_coingecko

        price = price_feed.get_token_price(token)

        self.assertIsNone(price)

    def test_batch_fetch_uses_coingecko_when_fallback_enabled(self):
        token = "0x1111111111111111111111111111111111111111"
        price_feed = PriceFeed(database=None, allow_coingecko_fallback=True)
        price_feed._fetch_prices_via_hydrex_routing = lambda addresses: {}
        price_feed._coingecko_get = lambda path, params: {token: {"usd": 1.23}}

        prices = price_feed.fetch_batch_prices_by_address([token])

        self.assertEqual(prices[token], 1.23)

    def test_routing_retries_429_then_succeeds(self):
        token = "0x1111111111111111111111111111111111111111"
        price_feed = PriceFeed(database=None, allow_coingecko_fallback=False)
        price_feed.routing_taker = "0x" + ("1" * 40)
        price_feed.routing_retry_max = 3
        price_feed.routing_backoff_base_seconds = 1.5
        price_feed._get_token_decimals = lambda _token: 18

        responses = [
            _FakeResponse(status_code=429),
            _FakeResponse(
                status_code=200,
                payload={
                    "swaps": [
                        {
                            "fromTokenAddress": token,
                            "amountIn": str(10**18),
                            "amountOut": str(2_000_000),
                        }
                    ]
                },
            ),
        ]

        with patch("src.price_feed.requests.post", side_effect=responses) as post_mock, patch(
            "src.price_feed.time.sleep"
        ) as sleep_mock:
            prices = price_feed.fetch_batch_prices_by_address([token])

        self.assertAlmostEqual(prices[token], 2.0)
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(1.5)

    def test_routing_400_no_quote_is_not_retried(self):
        token = "0x1111111111111111111111111111111111111111"
        price_feed = PriceFeed(database=None, allow_coingecko_fallback=False)
        price_feed.routing_taker = "0x" + ("1" * 40)
        price_feed.routing_retry_max = 3
        price_feed.routing_backoff_base_seconds = 1.5
        price_feed._get_token_decimals = lambda _token: 18

        no_quote = _FakeResponse(
            status_code=400,
            payload={"message": "No valid quotes received from any source"},
            text='{"message":"No valid quotes received from any source"}',
        )

        with patch("src.price_feed.requests.post", return_value=no_quote) as post_mock, patch(
            "src.price_feed.time.sleep"
        ) as sleep_mock:
            prices = price_feed.fetch_batch_prices_by_address([token])

        self.assertEqual(prices, {})
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_not_called()

    def test_token_policy_skip_and_coingecko_bypass(self):
        skip_token = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        coingecko_token = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        routed_token = "0xcccccccccccccccccccccccccccccccccccccccc"

        price_feed = PriceFeed(database=None, allow_coingecko_fallback=True)
        price_feed.routing_skip_tokens = {skip_token}
        price_feed.routing_coingecko_fallback_tokens = {coingecko_token}

        observed = {}

        def fake_routing(tokens):
            observed["routing_tokens"] = list(tokens)
            return {routed_token: 2.5}

        price_feed._fetch_prices_via_hydrex_routing = fake_routing
        price_feed._coingecko_get = lambda _path, _params: {coingecko_token: {"usd": 1.23}}

        prices = price_feed.fetch_batch_prices_by_address([skip_token, coingecko_token, routed_token])

        self.assertEqual(observed["routing_tokens"], [routed_token])
        self.assertEqual(prices[routed_token], 2.5)
        self.assertEqual(prices[coingecko_token], 1.23)
        self.assertNotIn(skip_token, prices)


if __name__ == "__main__":
    unittest.main()