"""A phase must not be reported as failed when its vote may be on-chain.

On 2026-11-04 the monitor logged "PHASE 2 AUTO-VOTE FAILED" after its 20s limit, yet run 208
had broadcast and its vote landed at T-35s (status 1, 48 logs). The monitor also discarded the
child's output, so its log held no trace of the vote.

Contract under test:
1. A child that stops before a send began is FAILED, and a revert is FAILED.
2. A child that stops after a send began, with no receipt seen, is BROADCAST_UNCONFIRMED.
3. A child that saw a successful receipt is CONFIRMED, even if it then exited non-zero.
4. The markers this relies on are what scripts/auto_voter.py really prints.
5. Child output reaches the log with the RPC key redacted, and text that looks like Rich
   markup cannot raise out of the monitor's handlers.
6. The real subprocess path delivers what a Rich-printing child wrote before the kill.
7. main() runs each phase exactly once, whatever the earlier phases' outcomes were, reports
   each outcome in its own words, and does not repeat a phase whose trigger raised.
"""

import importlib.util
import io
import os
import sys
from pathlib import Path
from unittest import mock

import pytest
from rich.console import Console

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "boundary_monitor.py"
AUTO_VOTER = ROOT / "scripts" / "auto_voter.py"
FAKE_TX_HASH = "0x" + "ab" * 32
SECRET = "SECRETKEY123"
LEAKY_LINE = f"Max retries exceeded with url: /v2/{SECRET}\n"
MARKUP_LOOKALIKE = "error reading [/tmp/foo] [/red]\n"

BEFORE_SEND = "Signing transaction...\n"
SEND_STARTED = f"{BEFORE_SEND}Sending transaction...\n"
SENT = f"{SEND_STARTED}✓ Transaction sent: \n{FAKE_TX_HASH}\nWaiting for receipt...\n"
MINED_OK = f"{SENT}✓ TRANSACTION SUCCESSFUL\n  Block: 53523709\n"
REVERTED = f"{SENT}✗ TRANSACTION FAILED\n"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("boundary_monitor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def monitor_log(mod, monkeypatch):
    """Capture what the monitor prints, as its log file would receive it."""
    buffer = io.StringIO()
    monkeypatch.setattr(mod, "console", Console(file=buffer, width=200))
    return buffer


def _trigger(mod, monkeypatch, run, timeout_seconds=1):
    """Call trigger_auto_voter with subprocess.run replaced by ``run``."""
    real_subprocess = sys.modules["subprocess"]

    class _StubSubprocess:
        TimeoutExpired = real_subprocess.TimeoutExpired

        @staticmethod
        def run(cmd, **kwargs):
            return run(cmd, **kwargs)

    monkeypatch.setattr(mod, "subprocess", _StubSubprocess)
    return mod.trigger_auto_voter(
        db_path="unused.db",
        your_voting_power=1,
        top_k=1,
        candidate_pools=1,
        auto_top_k=False,
        auto_top_k_min=1,
        auto_top_k_max=1,
        auto_top_k_step=1,
        min_votes_per_pool=1,
        max_gas_price_gwei=1.0,
        private_key_source="",
        dry_run=True,
        query_block=1,
        skip_fresh_fetch=True,
        auto_top_k_return_tolerance_pct=1.0,
        phase_label="phase2",
        min_seconds_before_boundary=0,
        enforce_pre_boundary_guard=False,
        timeout_seconds=timeout_seconds,
    )


def _times_out_with(stdout_bytes):
    def run(cmd, **kwargs):
        real_subprocess = sys.modules["subprocess"]
        raise real_subprocess.TimeoutExpired(cmd, 1, output=stdout_bytes)

    return run


def _exits_with(returncode, stdout, stderr=""):
    def run(cmd, **kwargs):
        real_subprocess = sys.modules["subprocess"]
        return real_subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

    return run


@pytest.mark.parametrize(
    "child_stdout, expected_name",
    [
        ("", "FAILED"),
        (BEFORE_SEND, "FAILED"),
        (SEND_STARTED, "BROADCAST_UNCONFIRMED"),
        (SENT, "BROADCAST_UNCONFIRMED"),
        (MINED_OK, "CONFIRMED"),
        (REVERTED, "FAILED"),
    ],
)
def test_classify_stopped_phase(mod, child_stdout, expected_name):
    assert mod.classify_stopped_phase(child_stdout) is mod.PhaseOutcome[expected_name]


def test_markers_match_what_auto_voter_prints(mod):
    """A reworded message in auto_voter.py would otherwise turn every timeout into FAILED."""
    source = AUTO_VOTER.read_text()
    assert f"[cyan]{mod.VOTE_SEND_STARTED_MARKER}[/cyan]" in source
    assert mod.VOTE_MINED_OK_MARKER in source
    assert mod.VOTE_REVERTED_MARKER in source


@pytest.mark.parametrize(
    "captured, expected",
    [
        (None, ""),
        ("text", "text"),
        (b"bytes", "bytes"),
        (b"bad \xff byte", "bad � byte"),
    ],
)
def test_decode_partial_output_never_raises(mod, captured, expected):
    assert mod.decode_partial_output(captured) == expected


def test_timeout_after_send_logs_the_output_with_the_key_removed(
    mod, monkeypatch, monitor_log
):
    stdout = f"{LEAKY_LINE}{MARKUP_LOOKALIKE}{SENT}".encode()
    outcome, _ = _trigger(mod, monkeypatch, _times_out_with(stdout))

    assert outcome is mod.PhaseOutcome.BROADCAST_UNCONFIRMED
    log = monitor_log.getvalue()
    assert FAKE_TX_HASH in log
    assert "[/tmp/foo]" in log
    assert SECRET not in log


def test_timeout_with_no_captured_output_is_failed(mod, monkeypatch, monitor_log):
    outcome, _ = _trigger(mod, monkeypatch, _times_out_with(None))
    assert outcome is mod.PhaseOutcome.FAILED
    assert "(no output captured)" in monitor_log.getvalue()


@pytest.mark.parametrize(
    "stdout, expected_name",
    [
        (BEFORE_SEND, "FAILED"),
        (SENT, "BROADCAST_UNCONFIRMED"),
        (MINED_OK, "CONFIRMED"),
        (REVERTED, "FAILED"),
    ],
)
def test_non_zero_exit_is_classified_from_the_output(
    mod, monkeypatch, monitor_log, stdout, expected_name
):
    """auto_voter exits 1 on an unavailable receipt or a failure after a good receipt."""
    outcome, _ = _trigger(mod, monkeypatch, _exits_with(1, stdout))
    assert outcome is mod.PhaseOutcome[expected_name]


@pytest.mark.parametrize("returncode", [0, 1])
def test_child_output_cannot_raise_or_leak_on_a_normal_exit(
    mod, monkeypatch, monitor_log, returncode
):
    stdout = f"{LEAKY_LINE}{MARKUP_LOOKALIKE}"
    _trigger(mod, monkeypatch, _exits_with(returncode, stdout))

    log = monitor_log.getvalue()
    assert "[/tmp/foo]" in log
    assert SECRET not in log


def test_unexpected_error_text_cannot_raise_or_leak(mod, monkeypatch, monitor_log):
    def run(cmd, **kwargs):
        raise OSError(f"{LEAKY_LINE}{MARKUP_LOOKALIKE}")

    outcome, message = _trigger(mod, monkeypatch, run)

    assert outcome is mod.PhaseOutcome.FAILED
    assert "[/tmp/foo]" in monitor_log.getvalue()
    assert SECRET not in monitor_log.getvalue()
    assert SECRET not in message


def test_monitor_error_line_escapes_exception_text(mod):
    """That print sits in the main loop's handler, where a MarkupError would end the monitor."""
    assert "{escape(redact_urls(str(e)))}[/red]" in SCRIPT.read_text()


def test_real_kill_after_broadcast_reports_it_as_possibly_on_chain(
    mod, monkeypatch, monitor_log
):
    """A real child, killed for real, with no PYTHONUNBUFFERED to help it flush."""
    real_subprocess = sys.modules["subprocess"]
    child_env = {k: v for k, v in os.environ.items() if k != "PYTHONUNBUFFERED"}
    child = (
        "import time\n"
        "from rich.console import Console\n"
        "console = Console()\n"
        "console.print('[cyan]Sending transaction...[/cyan]')\n"
        f"console.print('[green]✓ Transaction sent: {FAKE_TX_HASH}[/green]')\n"
        "time.sleep(30)\n"
    )

    def run(cmd, **kwargs):
        return real_subprocess.run(
            [sys.executable, "-c", child],
            env=child_env,
            stdin=real_subprocess.DEVNULL,
            **kwargs,
        )

    outcome, message = _trigger(mod, monkeypatch, run, timeout_seconds=5)

    assert outcome is mod.PhaseOutcome.BROADCAST_UNCONFIRMED
    assert "timed out" in message
    assert "Sending transaction..." in monitor_log.getvalue()


CURRENT_EPOCH = 1793232000
NEXT_BOUNDARY = CURRENT_EPOCH + 604800
PHASES = ["phase1", "phase2", "phase3"]
OUTCOME_WORDS = {
    "CONFIRMED": "AUTO-VOTE COMPLETED",
    "BROADCAST_UNCONFIRMED": "VOTE MAY BE ON-CHAIN",
    "FAILED": "AUTO-VOTE FAILED",
}


def _run_main_to_the_boundary(mod, trigger, db_path):
    """Drive the real main() on a fake clock, from T-300s in 10s checks, with no RPC."""
    seconds_until_boundary = {"value": 300}

    class FakeClockReader:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, _read):
            timestamp = NEXT_BOUNDARY - seconds_until_boundary["value"]
            seconds_until_boundary["value"] -= 10
            return 1000, timestamp, CURRENT_EPOCH

    w3 = mock.MagicMock()
    w3.is_connected.return_value = True
    w3.eth.chain_id = 8453
    log = io.StringIO()
    argv = [
        "boundary_monitor",
        "--dry-run",
        "--db-path",
        str(db_path),
        "--your-voting-power",
        "1",
        "--trigger-seconds-before",
        "240",
        "--second-trigger-seconds-before",
        "60",
        "--third-trigger-seconds-before",
        "35",
        "--no-price-prefetch",
        "--rpc",
        "http://fake.invalid/",
        "--check-interval",
        "10",
    ]
    with mock.patch.object(mod, "build_monitor_web3", return_value=w3), mock.patch.object(
        mod, "DeadlineBoundedCaller", FakeClockReader
    ), mock.patch.object(mod, "trigger_auto_voter", trigger), mock.patch.object(
        mod, "check_epoch_voting_power", return_value=(True, "ok", 1)
    ), mock.patch.object(
        mod, "load_wallet", return_value=mock.MagicMock(address="0x" + "1" * 40)
    ), mock.patch.object(
        mod.time, "sleep", lambda _seconds: None
    ), mock.patch.object(
        mod, "console", Console(file=log, width=200)
    ), mock.patch.object(
        mod.subprocess, "Popen"
    ), mock.patch.object(
        sys, "argv", argv
    ), mock.patch.object(
        sys, "excepthook", sys.excepthook
    ):
        mod.main()
    return log.getvalue()


@pytest.mark.parametrize("outcome_name", sorted(OUTCOME_WORDS))
@pytest.mark.parametrize("phase", PHASES)
def test_main_runs_every_phase_once_and_reports_each_outcome(
    mod, phase, outcome_name, tmp_path
):
    calls = []

    def trigger(**kwargs):
        label = kwargs["phase_label"]
        calls.append(label)
        if label == phase:
            return mod.PhaseOutcome[outcome_name], ""
        return mod.PhaseOutcome.CONFIRMED, ""

    log = _run_main_to_the_boundary(mod, trigger, tmp_path / "test.db")

    assert calls == PHASES
    phase_number = phase[-1]
    assert f"PHASE {phase_number} {OUTCOME_WORDS[outcome_name]}" in log
    other_words = set(OUTCOME_WORDS.values()) - {OUTCOME_WORDS[outcome_name]}
    for words in other_words:
        assert f"PHASE {phase_number} {words}" not in log
    assert "Boundary has passed" in log


@pytest.mark.parametrize("phase", PHASES)
def test_main_does_not_repeat_a_phase_whose_trigger_raised(mod, phase, tmp_path):
    calls = []

    def trigger(**kwargs):
        calls.append(kwargs["phase_label"])
        if kwargs["phase_label"] == phase:
            raise RuntimeError("raised after the child may have sent")
        return mod.PhaseOutcome.CONFIRMED, ""

    _run_main_to_the_boundary(mod, trigger, tmp_path / "test.db")

    assert calls == PHASES
