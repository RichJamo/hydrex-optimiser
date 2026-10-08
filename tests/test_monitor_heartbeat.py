"""Heartbeat pings from the boundary monitor to an outside dead-man's switch.

Added 2026-10-08, after the monitor missed every phase of epoch 1791417600 while the
home internet was down and nothing alerted anyone. An alert sent by the monitor cannot
work when the monitor is offline, so the monitor pings healthchecks.io while healthy
and the service alerts from outside when the pings stop.

Contract under test (src/monitor_heartbeat.py):
  - report_alive pings the check URL; report_failing pings <url>/fail;
  - pings are rate-limited to one per min_interval_seconds, alive and fail together;
  - a ping that hangs never blocks the caller, and no second ping starts while one is
    in flight;
  - a ping that raises is swallowed (the missing ping is itself the signal);
  - no ping body ever contains a URL: requests/web3 errors quote the RPC URL, whose
    path carries the API key, and healthchecks.io stores ping bodies (found in review
    2026-10-08 before the heartbeat was ever enabled);
  - with MONITOR_HEARTBEAT_URL unset the monitor gets a NoHeartbeat that sends nothing;
    a non-https URL is refused rather than silently used.

Time comes from an injected clock and sends from an injected function, so no test
depends on wall-clock time or the network. One test sends a real HTTP request to a
server on 127.0.0.1 to check the wire format.
"""

import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from src.monitor_heartbeat import (
    HEARTBEAT_URL_ENV,
    HealthchecksHeartbeat,
    NoHeartbeat,
    _post_ping,
    heartbeat_from_env,
    redact_urls,
)

CHECK_URL = "https://hc-ping.com/00000000-0000-0000-0000-000000000000"


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class RecordingSender:
    """Records each ping; can be told to block until released."""

    def __init__(self, block: bool = False):
        self.pings = []
        self.release = threading.Event()
        self.block = block
        self.sent = threading.Event()

    def __call__(self, url, body):
        if self.block:
            self.release.wait(timeout=30)
        self.pings.append((url, body))
        self.sent.set()


def _wait_for(sender: RecordingSender, count: int) -> None:
    deadline = time.monotonic() + 5
    while len(sender.pings) < count:
        assert (
            time.monotonic() < deadline
        ), f"expected {count} ping(s), got {sender.pings}"
        time.sleep(0.005)


def test_alive_pings_the_check_url_and_failing_pings_its_fail_endpoint():
    clock, sender = FakeClock(), RecordingSender()
    heartbeat = HealthchecksHeartbeat(
        CHECK_URL, min_interval_seconds=60, clock=clock, send=sender
    )

    heartbeat.report_alive("block 52314127, 600s until boundary")
    _wait_for(sender, 1)
    clock.now += 60
    heartbeat.report_failing("3 consecutive failed checks")
    _wait_for(sender, 2)

    assert sender.pings == [
        (CHECK_URL, "block 52314127, 600s until boundary"),
        (CHECK_URL + "/fail", "3 consecutive failed checks"),
    ]


def test_pings_are_rate_limited_across_alive_and_failing():
    clock, sender = FakeClock(), RecordingSender()
    heartbeat = HealthchecksHeartbeat(
        CHECK_URL, min_interval_seconds=60, clock=clock, send=sender
    )

    heartbeat.report_alive("first")
    _wait_for(sender, 1)
    clock.now += 59
    heartbeat.report_alive("too soon")
    heartbeat.report_failing("too soon as well")
    clock.now += 1
    heartbeat.report_alive("interval elapsed")
    _wait_for(sender, 2)

    assert [body for _, body in sender.pings] == ["first", "interval elapsed"]


def test_hanging_ping_never_blocks_the_monitor_and_is_not_stacked():
    clock, sender = FakeClock(), RecordingSender(block=True)
    heartbeat = HealthchecksHeartbeat(
        CHECK_URL, min_interval_seconds=60, clock=clock, send=sender
    )
    try:
        started = time.monotonic()
        heartbeat.report_alive("stuck in DNS")
        assert time.monotonic() - started < 0.5

        clock.now += 120
        heartbeat.report_failing("would be a second thread")
    finally:
        sender.release.set()
    _wait_for(sender, 1)
    time.sleep(0.05)

    assert [body for _, body in sender.pings] == ["stuck in DNS"]


def test_ping_that_raises_is_swallowed(monkeypatch):
    uncaught = []
    monkeypatch.setattr(threading, "excepthook", lambda args: uncaught.append(args))
    clock = FakeClock()

    def _raise(url, body):
        raise ConnectionError("network down")

    heartbeat = HealthchecksHeartbeat(
        CHECK_URL, min_interval_seconds=60, clock=clock, send=_raise
    )
    heartbeat.report_alive("anything")
    heartbeat._in_flight.join(timeout=5)

    assert not heartbeat._in_flight.is_alive()
    assert uncaught == []


def test_unset_url_gives_a_heartbeat_that_sends_nothing(monkeypatch):
    monkeypatch.delenv(HEARTBEAT_URL_ENV, raising=False)

    heartbeat = heartbeat_from_env()

    assert isinstance(heartbeat, NoHeartbeat)
    assert "disabled" in heartbeat.description
    heartbeat.report_alive("ignored")
    heartbeat.report_failing("ignored")


def test_set_url_gives_a_healthchecks_heartbeat(monkeypatch):
    monkeypatch.setenv(HEARTBEAT_URL_ENV, CHECK_URL)

    assert isinstance(heartbeat_from_env(), HealthchecksHeartbeat)


@pytest.mark.parametrize("bad_url", ["http://hc-ping.com/x", "hc-ping.com/x"])
def test_non_https_url_is_refused(bad_url):
    with pytest.raises(ValueError, match="https"):
        HealthchecksHeartbeat(bad_url)


def test_post_ping_sends_the_body_to_the_url():
    received = []

    class _Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, body.decode()))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _post_ping(
            f"http://127.0.0.1:{server.server_address[1]}/check-id/fail", "3 failures"
        )
    finally:
        server.shutdown()

    assert received == [("/check-id/fail", "3 failures")]


FAKE_KEY = "AbCdEf123456_SECRETKEY"
RPC_ERROR_TEXT = (
    "HTTPSConnectionPool(host='base-mainnet.g.alchemy.com', port=443): Max retries "
    f"exceeded with url: /v2/{FAKE_KEY} (Caused by NameResolutionError) | "
    f"429 Client Error: Too Many Requests for url: "
    f"https://base-mainnet.g.alchemy.com/v2/{FAKE_KEY}"
)


def test_redact_urls_removes_full_urls_and_bare_keyed_paths():
    redacted = redact_urls(RPC_ERROR_TEXT)

    assert FAKE_KEY not in redacted
    assert "alchemy.com/v2" not in redacted
    assert "Max retries exceeded" in redacted


def test_failing_ping_never_carries_the_rpc_key():
    clock, sender = FakeClock(), RecordingSender()
    heartbeat = HealthchecksHeartbeat(
        CHECK_URL, min_interval_seconds=60, clock=clock, send=sender
    )

    heartbeat.report_failing(f"3 consecutive failed checks: {RPC_ERROR_TEXT}")
    _wait_for(sender, 1)

    url, body = sender.pings[0]
    assert url == CHECK_URL + "/fail"
    assert FAKE_KEY not in body
    assert body.startswith("3 consecutive failed checks")


def test_disabled_heartbeat_says_why():
    assert NoHeartbeat(reason="dry run").description == "disabled (dry run)"
