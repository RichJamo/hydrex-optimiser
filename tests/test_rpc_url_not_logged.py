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
3. The vote and claim error handlers named below redact exception text.

The scan cannot see a key inside exception text in general, so other handlers that print
an exception are not covered by it.
"""

import ast
from pathlib import Path

import pytest

from src.monitor_heartbeat import redact_urls, rpc_host_for_log

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
# Source lines that handle errors from RPC calls on the vote and claim paths.
REDACTED_ERROR_LINES = {
    "scripts/auto_voter.py": [
        'f"{redact_urls(str(e))}[/yellow]"',
        "safe_error = redact_urls(str(e))",
    ],
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
