"""The RPC URL carries its API key, so it must never reach a log.

Found 2026-11-05: 204 git-ignored log files held a provider key, written by four "connected
to RPC" messages and by the monitor printing a requests error that quotes the URL's path.
One of the four truncated to 50 characters, which still showed part of the key. The
monitor's heartbeat had already leaked a key in 2026-10, so this class has now been seen
twice and gets a test rather than a note.

Contract under test:
1. rpc_host_for_log() returns the hostname and no part of the path, query or userinfo.
2. No print or logging call under SOURCE_DIRS mentions an RPC URL variable, unless the
   value goes through rpc_host_for_log() or redact_urls().
3. The vote, claim and pre-flight error handlers keep the key out of what they print,
   return and store, shown by raising a key-bearing error through them.
4. A real script hitting a failing local RPC, including an uncaught error, prints no key.

The scan cannot see a key inside exception text in general, so handlers that print an
exception and are not exercised here are not covered.
"""

import ast
import http.server
import importlib.util
import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

from src.monitor_heartbeat import redact_urls, rpc_host_for_log
from src.voting_power import check_epoch_voting_power

ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIRS = ["scripts", "src", "data", "config"]
LOG_METHODS = {
    "print",
    "log",
    "debug",
    "info",
    "warning",
    "error",
    "exception",
    "critical",
}
# Handlers that need a full claim or monitoring run to reach, so only their text is pinned.
REDACTED_ERROR_LINES = {
    "scripts/claim_and_swap_rewards.py": [
        "redact_urls(traceback.format_exc())",
        'f"[red]Error:[/red] {redact_urls(str(e))}"',
    ],
    "scripts/boundary_monitor.py": ['f"{redact_urls(str(e))}[/red]"'],
}
RPC_URL_NAMES = {"RPC_URL", "rpc_url", "rpc"}
SAFE_WRAPPERS = {"rpc_host_for_log", "redact_urls"}
SECRET = "SECRETKEY123"


@pytest.mark.parametrize(
    "rpc_url",
    [
        f"https://base-mainnet.g.alchemy.com/v2/{SECRET}",
        f"https://base-mainnet.g.alchemy.com/v2/{SECRET}?retries=1",
        f"https://user:{SECRET}@base-mainnet.g.alchemy.com/v2/",
        f"https://base-mainnet.g.alchemy.com/?apikey={SECRET}",
    ],
)
def test_rpc_host_for_log_keeps_only_the_hostname(rpc_url):
    assert rpc_host_for_log(rpc_url) == "base-mainnet.g.alchemy.com"


@pytest.mark.parametrize(
    "unparseable", ["", SECRET, f"/v2/{SECRET}", f"https://[{SECRET}/v2/"]
)
def test_rpc_host_for_log_never_echoes_what_it_cannot_parse(unparseable):
    assert rpc_host_for_log(unparseable) == "<unparseable rpc url>"


def test_redact_urls_removes_the_key_from_a_real_requests_error():
    """The shape the monitor printed, copied from a 2026-10 log with the key replaced."""
    error = (
        "HTTPSConnectionPool(host='base-mainnet.g.alchemy.com', port=443): "
        f"Max retries exceeded with url: /v2/{SECRET} (Caused by NameResolutionError)"
    )
    assert SECRET not in redact_urls(error)


def _call_name(call):
    function = call.func
    if isinstance(function, ast.Attribute):
        return function.attr
    if isinstance(function, ast.Name):
        return function.id
    return None


def _unwrapped_rpc_url_mentions(node):
    """Yield RPC URL names under ``node``, skipping anything passed to a safe wrapper."""
    if isinstance(node, ast.Call) and _call_name(node) in SAFE_WRAPPERS:
        return
    if isinstance(node, ast.Name) and node.id in RPC_URL_NAMES:
        yield node.id
    if isinstance(node, ast.Attribute) and node.attr in RPC_URL_NAMES:
        yield node.attr
    for child in ast.iter_child_nodes(node):
        yield from _unwrapped_rpc_url_mentions(child)


def _rpc_url_log_sites(source, filename):
    sites = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if isinstance(node, ast.Call) and _call_name(node) in LOG_METHODS:
            for argument in [*node.args, *[kw.value for kw in node.keywords]]:
                for name in _unwrapped_rpc_url_mentions(argument):
                    sites.append(f"{filename}:{node.lineno} logs {name}")
    return sites


@pytest.mark.parametrize(
    "line",
    [
        'logger.info(f"Connecting to RPC: {RPC_URL}")',
        'console.print(f"Connected to {args.rpc}")',
        'console.print(f"Connected to {RPC_URL[:50]}...")',
        "print(rpc_url)",
    ],
)
def test_the_scan_catches_each_way_the_key_was_logged(line):
    assert _rpc_url_log_sites(line, "example.py")


@pytest.mark.parametrize(
    "line",
    [
        'logger.info(f"Connecting to RPC: {rpc_host_for_log(RPC_URL)}")',
        'console.print(f"{redact_urls(str(rpc_url))}")',
        'console.print("RPC_URL missing")',
    ],
)
def test_the_scan_allows_safe_forms(line):
    assert not _rpc_url_log_sites(line, "example.py")


def test_vote_and_claim_error_handlers_redact_exception_text():
    for relative, expected_lines in REDACTED_ERROR_LINES.items():
        source = (ROOT / relative).read_text()
        for expected in expected_lines:
            assert expected in source, f"{relative} no longer has: {expected}"
    claim_source = (ROOT / "scripts/claim_and_swap_rewards.py").read_text()
    assert "exc_info=True" not in claim_source


def test_no_source_file_logs_an_rpc_url():
    sites = []
    for directory in SOURCE_DIRS:
        for path in sorted((ROOT / directory).rglob("*.py")):
            relative = str(path.relative_to(ROOT))
            sites.extend(_rpc_url_log_sites(path.read_text(), relative))
    assert not sites, "\n".join(sites)


LEAKY_ERROR = RuntimeError(
    f"429 Client Error for url: https://base-mainnet.g.alchemy.com/v2/{SECRET}"
)
THROWAWAY_PRIVATE_KEY = "0x" + "11" * 32  # dry runs against 127.0.0.1 only
SIGNER = "0x" + "11" * 20


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        f"{name}_under_test", ROOT / "scripts" / f"{name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def auto_voter():
    return _load_script("auto_voter")


@pytest.fixture(scope="module")
def boundary_monitor():
    return _load_script("boundary_monitor")


def _capture_console(module):
    output = io.StringIO()
    return output, mock.patch.object(
        module, "console", Console(file=output, width=200)
    )


def _rpc_failing_with_key():
    w3 = mock.MagicMock()
    w3.eth.call.side_effect = LEAKY_ERROR
    return w3


def test_preflight_checks_redact_a_failed_read(boundary_monitor):
    ok, detail = boundary_monitor.check_signer_partner_role(
        _rpc_failing_with_key(), SIGNER, SIGNER
    )
    assert not ok and "RuntimeError" in detail and SECRET not in detail

    ok, detail, _votes = check_epoch_voting_power(
        _rpc_failing_with_key(), SIGNER, SIGNER, 1
    )
    assert not ok and "RuntimeError" in detail and SECRET not in detail


def test_simulation_error_is_redacted(auto_voter):
    vote_contract = mock.MagicMock()
    vote_contract.functions.vote.side_effect = LEAKY_ERROR
    output, patched_console = _capture_console(auto_voter)
    with patched_console:
        assert not auto_voter.simulate_vote_transaction(
            vote_contract, [SIGNER], [100], SIGNER
        )
    assert "Simulation error" in output.getvalue()
    assert SECRET not in output.getvalue()


def test_failed_read_of_a_stuck_transaction_is_redacted(auto_voter):
    w3 = mock.MagicMock()
    w3.eth.get_transaction.side_effect = LEAKY_ERROR
    output, patched_console = _capture_console(auto_voter)
    with patched_console:
        auto_voter._replacement_gas_price(w3, "0xabc", 10**9, 5.0)
    assert "Could not read stuck transaction" in output.getvalue()
    assert SECRET not in output.getvalue()


def test_send_path_error_is_redacted_in_output_and_return_value(auto_voter):
    w3 = mock.MagicMock()
    w3.eth.gas_price = 10**8
    w3.eth.get_transaction_count.side_effect = LEAKY_ERROR
    wallet = mock.MagicMock(address=SIGNER)
    output, patched_console = _capture_console(auto_voter)
    with patched_console, mock.patch.object(
        auto_voter, "simulate_vote_transaction", return_value=True
    ):
        result = auto_voter.build_and_send_vote_transaction(
            w3=w3,
            vote_contract=mock.MagicMock(),
            wallet=wallet,
            pool_addresses=[SIGNER],
            vote_proportions=[100],
            max_gas_price_gwei=5.0,
            vote_target_address=SIGNER,
            gas_limit=1_000_000,
            gas_buffer_multiplier=1.2,
            dry_run=False,
        )
    assert result[0] is False
    assert "429 Client Error" in result[1] and SECRET not in result[1]
    assert "Transaction error" in output.getvalue()
    assert SECRET not in output.getvalue()


def test_stored_run_error_text_is_not_raw_exception_text():
    """main()'s failure path needs a recorded run to reach, so this one is a source scan."""
    tree = ast.parse((ROOT / "scripts/auto_voter.py").read_text())
    raw_sites = []
    for handler in ast.walk(tree):
        if isinstance(handler, ast.ExceptHandler) and handler.name:
            for node in ast.walk(handler):
                if (
                    isinstance(node, ast.keyword)
                    and node.arg == "error_text"
                    and isinstance(node.value, ast.Call)
                    and getattr(node.value.func, "id", "") == "str"
                ):
                    raw_sites.append(node.value.lineno)
    assert not raw_sites, raw_sites


class _FailingRpc(http.server.BaseHTTPRequestHandler):
    """Answers the allowed JSON-RPC methods and returns 429 for every other one."""

    ANSWERS = {
        "web3_clientVersion": "fake",
        "eth_chainId": "0x2105",
        "eth_blockNumber": "0x10",
    }

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["content-length"])))
        if request["method"] not in self.server.allowed_methods:
            self.send_response(429)
            self.send_header("content-length", "0")
            self.end_headers()
            return
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request["id"],
                "result": self.ANSWERS[request["method"]],
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _run_script_against_failing_rpc(script_args, allowed_methods):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _FailingRpc)
    server.allowed_methods = allowed_methods
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rpc_url = f"http://127.0.0.1:{server.server_address[1]}/v2/{SECRET}"
    try:
        return subprocess.run(
            [sys.executable, *script_args, "--rpc", rpc_url],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize(
    "script", ["scripts/boundary_monitor.py", "scripts/auto_voter.py"]
)
def test_uncaught_rpc_error_exits_with_a_traceback_and_no_key(script, tmp_path):
    completed = _run_script_against_failing_rpc(
        [
            script,
            "--dry-run",
            "--db-path",
            str(tmp_path / "test.db"),
            "--your-voting-power",
            "1",
        ],
        {"web3_clientVersion"},
    )
    assert completed.returncode == 1
    assert "Traceback" in completed.stderr and "429" in completed.stderr
    assert SECRET not in completed.stdout + completed.stderr


def test_failed_wallet_preflight_read_prints_no_key(tmp_path):
    completed = _run_script_against_failing_rpc(
        [
            "scripts/auto_voter.py",
            "--dry-run",
            "--db-path",
            str(tmp_path / "test.db"),
            "--your-voting-power",
            "1",
            "--private-key-source",
            THROWAWAY_PRIVATE_KEY,
        ],
        {"web3_clientVersion", "eth_chainId", "eth_blockNumber"},
    )
    assert completed.returncode == 1
    assert "Failed to load wallet" in completed.stdout
    assert SECRET not in completed.stdout + completed.stderr
