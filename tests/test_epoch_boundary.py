"""The boundary block is the block where the Minter's active_period() reaches the epoch.

Why it matters: an epoch flips when a keeper calls the Minter, 9-17 seconds after 00:00
UTC in the epochs measured so far, not at 00:00 itself. Bribes paid in that gap still
count for the closing epoch (BribeV2 keys them by active_period()), so a review reading
state at the first block stamped 00:00 would miss them. Since Base's Denim upgrade
(2026-11-05) blocks come every 200 ms and several share one timestamp, so the search
must not assume a block rate.

The fake chain models exactly what find_epoch_flip reads: block timestamps, the latest
block number, Voter.minter() and Minter.active_period() at a given block.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

from config.settings import VOTER_ADDRESS
from src.epoch_boundary import (
    EpochNotFlippedError,
    InconsistentChainStateError,
    find_epoch_flip,
    first_block_where,
)

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "run_postmortem_review.py"

WEEK = 604800
OLD_EPOCH = 1793836800
NEW_EPOCH = OLD_EPOCH + WEEK
VOTER = VOTER_ADDRESS  # the address the wrapper passes; the fake chain checks it
MINTER = "0xA7D64625F45548a19B2A19e28E7546bb2839003E"
REPLACEMENT_MINTER = "0x000000000000000000000000000000000000dEaD"


class _Call:
    def __init__(self, read):
        self._read = read

    def call(self, block_identifier="latest"):
        return self._read(block_identifier)


class _FakeChain:
    """Blocks 0..len(timestamps)-1; the Minter flips to ``period_after_mint`` at ``mint_block``."""

    def __init__(self, timestamps, mint_block, period_after_mint=NEW_EPOCH, minter_replaced_at=None):
        self.timestamps = timestamps
        self.minter_replaced_at = minter_replaced_at
        self.mint_block = mint_block
        self.period_after_mint = period_after_mint
        self.reads = 0
        chain = self

        class _Eth:
            @property
            def block_number(self):
                return len(chain.timestamps) - 1

            def get_block(self, number):
                chain.reads += 1
                return {"timestamp": chain.timestamps[number]}

            def contract(self, address, abi):
                names = {entry["name"] for entry in abi}
                functions = type("Functions", (), {})()
                if "minter" in names:
                    assert address.lower() == VOTER.lower()
                    functions.minter = lambda: _Call(chain.minter_at)
                if "active_period" in names:
                    assert address == MINTER
                    functions.active_period = lambda: _Call(chain.active_period_at)
                return type("Contract", (), {"functions": functions})()

        self.eth = _Eth()

    def minter_at(self, block):
        if block == "latest":
            block = len(self.timestamps) - 1
        replaced = self.minter_replaced_at is not None and block >= self.minter_replaced_at
        return REPLACEMENT_MINTER if replaced else MINTER

    def active_period_at(self, block):
        self.reads += 1
        if block == "latest":
            block = len(self.timestamps) - 1
        return self.period_after_mint if block >= self.mint_block else OLD_EPOCH


def _denim_timestamps(start, seconds, blocks_per_second=5):
    """Five blocks per second, so five blocks share each timestamp, as on Base since Denim."""
    return [start + i // blocks_per_second for i in range(seconds * blocks_per_second)]


def test_finds_the_mint_block_not_the_first_block_stamped_with_the_epoch_time():
    timestamps = _denim_timestamps(NEW_EPOCH - 100, 300)
    first_at_epoch_time = timestamps.index(NEW_EPOCH)
    mint_block = first_at_epoch_time + 75  # 15 s later, as on 2026-11-12
    flip = find_epoch_flip(_FakeChain(timestamps, mint_block), VOTER, NEW_EPOCH)

    assert flip.first_block_at_epoch_time == first_at_epoch_time == 500
    assert flip.mint_block == mint_block
    assert flip.mint_lag_blocks == 75
    assert flip.mint_lag_seconds == 15
    assert flip.minter_address == MINTER


def test_takes_the_first_of_several_blocks_sharing_the_epoch_timestamp():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    flip = find_epoch_flip(_FakeChain(timestamps, mint_block=60), VOTER, NEW_EPOCH)
    assert timestamps[flip.first_block_at_epoch_time - 1] < NEW_EPOCH
    assert flip.first_block_at_epoch_time == 50
    assert timestamps[50:55] == [NEW_EPOCH] * 5


def test_a_mint_in_the_first_block_of_the_epoch_has_zero_lag():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    flip = find_epoch_flip(_FakeChain(timestamps, mint_block=50), VOTER, NEW_EPOCH)
    assert flip.mint_block == flip.first_block_at_epoch_time == 50
    assert flip.mint_lag_blocks == 0 and flip.mint_lag_seconds == 0


def test_search_reads_logarithmically_many_blocks():
    timestamps = _denim_timestamps(NEW_EPOCH - 50_000, 100_000)
    chain = _FakeChain(timestamps, mint_block=timestamps.index(NEW_EPOCH) + 85)
    find_epoch_flip(chain, VOTER, NEW_EPOCH)
    assert chain.reads < 60  # two searches over 500k blocks are ~19 reads each


def test_refuses_before_the_minter_has_flipped():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    with pytest.raises(EpochNotFlippedError):
        find_epoch_flip(_FakeChain(timestamps, mint_block=10_000), VOTER, NEW_EPOCH)


def test_refuses_an_epoch_whose_start_time_has_not_arrived():
    timestamps = _denim_timestamps(NEW_EPOCH - 100, 30)
    with pytest.raises(EpochNotFlippedError, match="has not started"):
        find_epoch_flip(_FakeChain(timestamps, mint_block=10_000), VOTER, NEW_EPOCH)


def test_refuses_when_the_minter_shows_the_epoch_before_its_start_time():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    with pytest.raises(InconsistentChainStateError):
        find_epoch_flip(_FakeChain(timestamps, mint_block=20), VOTER, NEW_EPOCH)


def test_reads_the_minter_that_was_in_place_at_the_epoch():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    chain = _FakeChain(timestamps, mint_block=60, minter_replaced_at=100)
    flip = find_epoch_flip(chain, VOTER, NEW_EPOCH)
    assert flip.minter_address == MINTER and flip.mint_block == 60


def test_refuses_when_the_minter_skipped_the_epoch():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    chain = _FakeChain(timestamps, mint_block=60, period_after_mint=NEW_EPOCH + WEEK)
    with pytest.raises(InconsistentChainStateError, match="skipped"):
        find_epoch_flip(chain, VOTER, NEW_EPOCH)


def test_refuses_an_epoch_that_is_not_thursday_midnight():
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 30)
    with pytest.raises(ValueError, match="not a Thursday"):
        find_epoch_flip(_FakeChain(timestamps, mint_block=60), VOTER, NEW_EPOCH + 3600)


def test_first_block_where_returns_the_first_true_block():
    assert first_block_where(lambda block: block >= 7, 0, 100) == 7
    assert first_block_where(lambda block: block >= 7, 7, 7) == 7
    assert first_block_where(lambda block: True, 3, 100) == 3


def test_first_block_where_refuses_when_the_upper_bound_is_not_reached():
    with pytest.raises(InconsistentChainStateError):
        first_block_where(lambda block: block >= 200, 0, 100)
    with pytest.raises(ValueError):
        first_block_where(lambda block: True, 5, 4)


def _load_postmortem_module():
    spec = importlib.util.spec_from_file_location("run_postmortem_review_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_postmortem_main(monkeypatch, tmp_path, extra_args, mint_lag_blocks=75):
    """Run the real main() and the real resolve -> find_epoch_flip path against a fake chain.

    Only the Web3 constructor is replaced, so the epoch main() passes on is what gets
    searched. Stops at the first subprocess, the boundary upsert, and returns its argv.
    """
    module = _load_postmortem_module()
    commands = []
    timestamps = _denim_timestamps(NEW_EPOCH - 10, 40)
    chain = _FakeChain(timestamps, mint_block=timestamps.index(NEW_EPOCH) + mint_lag_blocks)

    class _StubWeb3:
        def __new__(cls, provider):
            return chain

        @staticmethod
        def HTTPProvider(url):
            return url

    class _StopAfterFirstSubprocess(Exception):
        pass

    def record(command, env, dry_run):
        commands.append([str(part) for part in command])
        raise _StopAfterFirstSubprocess

    monkeypatch.setattr(module, "Web3", _StubWeb3)
    monkeypatch.setattr(module, "run_subprocess", record)
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)  # main() installs the redacting hook
    monkeypatch.setenv("RPC_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_postmortem_review.py", "--db-path", str(tmp_path / "absent.db"), "--voting-power", "1813743"]
        + extra_args,
    )
    with pytest.raises(_StopAfterFirstSubprocess):
        module.main()
    return commands, timestamps.index(NEW_EPOCH) + mint_lag_blocks


def _value_after(command, flag):
    return command[command.index(flag) + 1]


def test_find_boundary_block_upserts_the_epochs_mint_block_with_its_own_source_tag(monkeypatch, tmp_path):
    commands, mint_block = _run_postmortem_main(
        monkeypatch, tmp_path, ["--epoch", str(NEW_EPOCH), "--find-boundary-block"]
    )
    upsert = commands[0]
    assert upsert[1].endswith("set_epoch_boundary_manual.py")
    assert _value_after(upsert, "--epoch") == str(NEW_EPOCH)
    assert _value_after(upsert, "--boundary-block") == str(mint_block) == "125"
    assert _value_after(upsert, "--source-tag") == "onchain_mint_block"


def test_find_boundary_block_keeps_a_source_tag_the_operator_chose(monkeypatch, tmp_path):
    commands, _ = _run_postmortem_main(
        monkeypatch,
        tmp_path,
        ["--epoch", str(NEW_EPOCH), "--find-boundary-block", "--source-tag", "operator_tag"],
    )
    assert _value_after(commands[0], "--source-tag") == "operator_tag"


def test_a_manual_boundary_block_keeps_the_manual_source_tag(monkeypatch, tmp_path):
    commands, _ = _run_postmortem_main(
        monkeypatch, tmp_path, ["--epoch", str(NEW_EPOCH), "--boundary-block", "123"]
    )
    assert _value_after(commands[0], "--boundary-block") == "123"
    assert _value_after(commands[0], "--source-tag") == "manual_explorer_boundary"


def test_find_boundary_block_cannot_be_combined_with_a_manual_block(monkeypatch, tmp_path):
    module = _load_postmortem_module()
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_postmortem_review.py", "--epoch", str(NEW_EPOCH), "--boundary-block", "1", "--find-boundary-block"],
    )
    with pytest.raises(SystemExit, match="cannot be combined"):
        module.main()


def test_find_boundary_block_requires_an_epoch_before_it_needs_an_rpc(monkeypatch):
    module = _load_postmortem_module()
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.delenv("RPC_URL", raising=False)
    monkeypatch.setattr(module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["run_postmortem_review.py", "--find-boundary-block"])
    with pytest.raises(SystemExit, match="requires --epoch"):
        module.main()
