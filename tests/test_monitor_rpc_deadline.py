"""How long one boundary-monitor check may take when the network is down.

Found 2026-10-08, after the monitor missed every phase of epoch 1791417600. The home
internet dropped from 23:02 to 02:36 UTC and the monitor failed 39 checks in that time,
about 330s each, although its error handler said "Retrying in 30 seconds". Reproduced
offline: web3's HTTPProvider retries a failed connection 5 times, each retry makes a
fresh DNS lookup, and an unreachable DNS server blocks each lookup for ~60s. The lookup
runs outside the HTTP request timeout, so no request_kwargs setting could bound it.

Contract under test (src/monitor_rpc.py):
  - build_monitor_web3 makes exactly ONE connection attempt (one DNS lookup) per RPC call;
  - DeadlineBoundedCaller returns within its deadline however long the lookup blocks;
  - a read stuck past its deadline does not block the next one, so a network that comes
    back is noticed on the next call rather than after the ~60s lookup ends;
  - at most max_stuck_reads reads run past their deadline at once; at the cap the next
    call fails fast without starting a thread, and calls work again once one ends;
  - a read that finishes in time returns its result or raises its own exception;
  - error_retry_wait_seconds uses the short wait only within the final hour before the
    boundary.

No test touches the real network: DNS is replaced by a function the test controls, and
the healthy-path test talks to a JSON-RPC server on 127.0.0.1.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from requests.exceptions import ConnectionError as RequestsConnectionError

from src.monitor_rpc import (
    DeadlineBoundedCaller,
    RpcDeadlineExceeded,
    TooManyStuckRpcReads,
    build_monitor_web3,
    error_retry_wait_seconds,
    seconds_until_boundary_by_local_clock,
)

UNRESOLVABLE_RPC_URL = "https://rpc.example.invalid/v2/key"


@pytest.fixture
def failing_dns(monkeypatch):
    """Every DNS lookup fails immediately. Returns a list that records each lookup."""
    lookups = []

    def _fail(*args, **kwargs):
        lookups.append(args[0] if args else None)
        raise socket.gaierror(socket.EAI_NONAME, "test: DNS server unreachable")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    return lookups


@pytest.fixture
def blocking_dns(monkeypatch):
    """Every DNS lookup blocks until the test sets the returned event, then fails."""
    release = threading.Event()

    def _block(*args, **kwargs):
        release.wait(timeout=30)
        raise socket.gaierror(socket.EAI_NONAME, "test: DNS server unreachable")

    monkeypatch.setattr(socket, "getaddrinfo", _block)
    yield release
    release.set()


def test_one_rpc_call_makes_exactly_one_dns_lookup(failing_dns):
    w3 = build_monitor_web3(UNRESOLVABLE_RPC_URL, request_timeout_seconds=5)

    with pytest.raises(RequestsConnectionError):
        w3.eth.block_number

    # web3's default retry middleware makes 5; each would block ~60s on a dead resolver.
    assert len(failing_dns) == 1


def test_check_returns_at_deadline_while_dns_lookup_is_still_blocked(blocking_dns):
    w3 = build_monitor_web3(UNRESOLVABLE_RPC_URL, request_timeout_seconds=5)
    reader = DeadlineBoundedCaller(deadline_seconds=0.3)

    started = time.monotonic()
    with pytest.raises(RpcDeadlineExceeded):
        reader.call(lambda: w3.eth.block_number)
    elapsed = time.monotonic() - started

    # The lookup is held for up to 30s; the check must give up at the 0.3s deadline.
    assert 0.25 <= elapsed < 2.0


def test_recovered_network_is_seen_while_an_earlier_read_is_still_stuck():
    reader = DeadlineBoundedCaller(deadline_seconds=0.1, max_stuck_reads=12)
    release = threading.Event()
    try:
        with pytest.raises(RpcDeadlineExceeded):
            reader.call(lambda: release.wait(timeout=30))
        assert reader.stuck_read_count == 1

        # The stuck lookup has ~60s left; the next call must not wait behind it.
        assert reader.call(lambda: "block 52314127") == "block 52314127"
    finally:
        release.set()


def test_call_fails_fast_once_the_stuck_read_cap_is_reached():
    reader = DeadlineBoundedCaller(deadline_seconds=0.1, max_stuck_reads=2)
    release = threading.Event()
    try:
        for _ in range(2):
            with pytest.raises(RpcDeadlineExceeded):
                reader.call(lambda: release.wait(timeout=30))

        third_read_started = threading.Event()
        started = time.monotonic()
        with pytest.raises(TooManyStuckRpcReads):
            reader.call(third_read_started.set)
        assert time.monotonic() - started < 0.5
        assert not third_read_started.is_set()
    finally:
        release.set()

    deadline = time.monotonic() + 5
    while reader.stuck_read_count:
        assert time.monotonic() < deadline, "released reads never finished"
        time.sleep(0.01)
    assert reader.call(lambda: 42) == 42


def test_read_that_finishes_in_time_returns_its_result_or_its_own_error():
    reader = DeadlineBoundedCaller(deadline_seconds=5)

    assert reader.call(lambda: (1, 2, 3)) == (1, 2, 3)

    def _boom():
        raise ValueError("Voter ABI missing _epochTimestamp")

    with pytest.raises(ValueError, match="Voter ABI missing"):
        reader.call(_boom)


def test_deadline_and_stuck_read_cap_must_be_positive():
    with pytest.raises(ValueError):
        DeadlineBoundedCaller(deadline_seconds=0)
    with pytest.raises(ValueError):
        DeadlineBoundedCaller(deadline_seconds=1, max_stuck_reads=0)


class _FakeBaseRpc(BaseHTTPRequestHandler):
    BLOCK_NUMBER = 52314127
    BLOCK_TIMESTAMP = 1791417601

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        method = request["method"]
        if method == "eth_blockNumber":
            result = hex(self.BLOCK_NUMBER)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": hex(self.BLOCK_NUMBER),
                "timestamp": hex(self.BLOCK_TIMESTAMP),
                "hash": "0x" + "11" * 32,
                "parentHash": "0x" + "22" * 32,
                "transactions": [],
            }
        else:
            result = None
        body = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_rpc_url():
    server = HTTPServer(("127.0.0.1", 0), _FakeBaseRpc)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_healthy_check_reads_the_block_within_the_deadline(fake_rpc_url):
    w3 = build_monitor_web3(fake_rpc_url, request_timeout_seconds=5)
    reader = DeadlineBoundedCaller(deadline_seconds=5)

    def _read():
        number = int(w3.eth.block_number)
        return number, int(w3.eth.get_block(number)["timestamp"])

    assert reader.call(_read) == (52314127, 1791417601)


@pytest.mark.parametrize(
    "seconds_until_boundary, expected_wait",
    [
        (3601, 30),  # just outside the final hour
        (3600, 5),  # final hour starts
        (35, 5),
        (0, 5),  # the boundary itself
        (-1, 30),  # boundary passed: back to the normal pace
        (604800, 30),
    ],
)
def test_short_retry_wait_applies_only_in_the_final_hour(
    seconds_until_boundary, expected_wait
):
    assert (
        error_retry_wait_seconds(
            seconds_until_boundary,
            normal_wait_seconds=30,
            near_boundary_wait_seconds=5,
        )
        == expected_wait
    )


def test_build_rejects_empty_url():
    with pytest.raises(ValueError):
        build_monitor_web3("", request_timeout_seconds=5)


WEEK = 604800
BOUNDARY = 1791417600  # Thursday 2026-10-08 00:00 UTC, a week multiple


@pytest.mark.parametrize(
    "now_ts, known_boundary, expected",
    [
        (BOUNDARY - 600, BOUNDARY, 600),  # last seen boundary still ahead
        (BOUNDARY - 600, None, 600),  # no check ever succeeded: week-aligned
        (BOUNDARY + 5, BOUNDARY, WEEK - 5),  # seen boundary passed: next week
        (BOUNDARY, BOUNDARY, WEEK),  # exactly at the boundary: the next one
        (BOUNDARY - 600, BOUNDARY + 300, 900),  # simulated boundary is honoured
    ],
)
def test_local_clock_boundary_estimate(now_ts, known_boundary, expected):
    assert (
        seconds_until_boundary_by_local_clock(now_ts, known_boundary, WEEK) == expected
    )
