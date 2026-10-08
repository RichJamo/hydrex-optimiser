"""A hung phase 1 must not block phases 2 and 3 past the flip.

`trigger_auto_voter()` ran the child auto-voter with a hard-coded `timeout=600`
(scripts/boundary_monitor.py:265). A normal phase takes 10-30s, so a stuck RPC call
inside phase 1 could hold the monitor's loop for up to 10 minutes -- past the phase-2
and phase-3 triggers and the boundary itself -- so neither later phase ever ran.

`phase_timeout_seconds()` bounds each phase's run time by the time left before the
next phase is due: phase 1 until 5s before the phase-2 trigger, phase 2 until 5s
before the phase-3 trigger, phase 3 until 20s after the boundary (a vote after the
flip reverts anyway, so there is no reason to wait longer), with a 10-second floor so
a pathologically tight schedule never yields a timeout a subprocess can't even start
in.
"""

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "boundary_monitor.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("boundary_monitor", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "phase, seconds_until_boundary, second_trigger, third_trigger, expected",
    [
        ("phase1", 240, 60, 35, 175),
        ("phase2", 60, 60, 35, 20),
        ("phase3", 35, 60, 35, 55),
    ],
)
def test_phase_timeout_at_typical_trigger_times(
    mod, phase, seconds_until_boundary, second_trigger, third_trigger, expected
):
    assert (
        mod.phase_timeout_seconds(
            phase, seconds_until_boundary, second_trigger, third_trigger
        )
        == expected
    )


@pytest.mark.parametrize(
    "phase, seconds_until_boundary, second_trigger, third_trigger",
    [
        ("phase1", 70, 60, 35),  # 70-60-5 = 5, below the floor
        ("phase2", 30, 60, 35),  # 30-35-5 = -10, below the floor
    ],
)
def test_phase_timeout_never_below_ten_second_floor(
    mod, phase, seconds_until_boundary, second_trigger, third_trigger
):
    assert (
        mod.phase_timeout_seconds(
            phase, seconds_until_boundary, second_trigger, third_trigger
        )
        == 10
    )


def test_trigger_auto_voter_returns_within_the_passed_limit_when_child_hangs(
    mod, monkeypatch
):
    """A child that sleeps past timeout_seconds is killed and reported, not waited on.

    Stubs the command subprocess.run executes (sleeps 5s) rather than calling
    auto_voter.py, and gives trigger_auto_voter a 1s timeout_seconds -- so a pass
    proves both that the real limit is honoured (elapsed well under the old 600s) and
    that the failure message names that limit rather than the old fixed text.
    """
    real_subprocess = sys.modules["subprocess"]
    timeout_seconds = 1

    class _StubSubprocess:
        TimeoutExpired = real_subprocess.TimeoutExpired

        @staticmethod
        def run(cmd, **kwargs):
            return real_subprocess.run(
                [sys.executable, "-c", "import time; time.sleep(5)"], **kwargs
            )

    monkeypatch.setattr(mod, "subprocess", _StubSubprocess)

    start = time.monotonic()
    success, message = mod.trigger_auto_voter(
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
        phase_label="phase1",
        min_seconds_before_boundary=0,
        enforce_pre_boundary_guard=False,
        timeout_seconds=timeout_seconds,
    )
    elapsed = time.monotonic() - start

    assert success is False
    assert str(timeout_seconds) in message
    assert elapsed < 30, "trigger_auto_voter waited far longer than its own timeout"


def test_default_trigger_spacing_is_240_60_35(mod, monkeypatch):
    """The phase-2/phase-3 defaults regressed to 60/35 (docs/OPERATIONS_RUNBOOK.md).

    40/20 spacing sent phase 3 about 1s after the flip and the vote reverted; only
    .env and the runbook's command line carried the working 60/35, so a monitor
    started without either flag used the failing spacing. This locks the parser's
    own defaults -- with the three AUTO_VOTE_*_SECONDS_BEFORE variables unset -- to
    240/60/35.
    """
    for var in (
        "AUTO_VOTE_TRIGGER_SECONDS_BEFORE",
        "AUTO_VOTE_SECOND_TRIGGER_SECONDS_BEFORE",
        "AUTO_VOTE_THIRD_TRIGGER_SECONDS_BEFORE",
    ):
        monkeypatch.delenv(var, raising=False)

    args = mod.build_arg_parser().parse_args(
        ["--rpc", "http://unused", "--your-voting-power", "1"]
    )

    assert args.trigger_seconds_before == 240
    assert args.second_trigger_seconds_before == 60
    assert args.third_trigger_seconds_before == 35
