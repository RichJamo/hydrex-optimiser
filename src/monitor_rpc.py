"""
Wall-clock-bounded RPC reads for the boundary monitor's polling loop.

Why this exists (2026-10-07): the home internet went down an hour before the
boundary and every monitor check took ~5.5 minutes to fail. Two things stacked:

  - A DNS lookup against an unreachable resolver blocks for ~60s, and Python
    performs it outside the HTTP request timeout, so ``request_kwargs`` cannot
    bound it.
  - web3's HTTPProvider retries a failed connection 5 times by default
    (``http_retry_request_middleware``), so one RPC call made 5 lookups.

A monitor stuck inside one 5-minute attempt can miss every phase even if the
network comes back minutes before the boundary. This module bounds each check
in wall-clock time instead, and leaves retrying to the monitor's own loop.
"""

from __future__ import annotations

import threading
from typing import Callable, List, TypeVar

from web3 import Web3

T = TypeVar("T")

NEAR_BOUNDARY_WINDOW_SECONDS = 3600

# A stuck DNS lookup lasts ~60s and the monitor retries every 5s near the boundary,
# so 12 stuck reads keep a fresh attempt starting on every retry for a whole lookup.
DEFAULT_MAX_STUCK_READS = 12


class RpcDeadlineExceeded(Exception):
    """An RPC read did not finish within its wall-clock deadline."""


class TooManyStuckRpcReads(Exception):
    """The cap on reads still running past their deadline is reached."""


def build_monitor_web3(rpc_url: str, request_timeout_seconds: float) -> Web3:
    """
    Build a Web3 instance for the monitor's polling loop.

    Postconditions:
      - HTTP requests use ``request_timeout_seconds`` for connect and read.
      - web3's built-in connection retry is disabled, so one RPC call makes at
        most one connection attempt (and one DNS lookup). The caller retries.
    """
    if not rpc_url:
        raise ValueError("rpc_url is empty")
    provider = Web3.HTTPProvider(
        rpc_url, request_kwargs={"timeout": request_timeout_seconds}
    )
    provider.middlewares = ()
    return Web3(provider)


class DeadlineBoundedCaller:
    """
    Run each RPC read on a daemon thread, waiting at most a deadline for it.

    A thread blocked in a DNS lookup cannot be cancelled, so a read that misses
    its deadline keeps running in the background until the lookup gives up.
    Each new call starts a fresh read rather than waiting on the stuck ones, so
    a network that comes back is noticed on the next call, not after the
    slowest lookup ends.

    Invariant: at most ``max_stuck_reads`` reads are running past their
    deadline at once. At the cap, ``call`` fails fast with TooManyStuckRpcReads
    instead of starting another thread.
    """

    def __init__(
        self, deadline_seconds: float, max_stuck_reads: int = DEFAULT_MAX_STUCK_READS
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("deadline_seconds must be > 0")
        if max_stuck_reads < 1:
            raise ValueError("max_stuck_reads must be >= 1")
        self.deadline_seconds = float(deadline_seconds)
        self.max_stuck_reads = int(max_stuck_reads)
        self._stuck_reads: List[threading.Thread] = []

    @property
    def stuck_read_count(self) -> int:
        """Reads that missed their deadline and are still running."""
        self._stuck_reads = [t for t in self._stuck_reads if t.is_alive()]
        return len(self._stuck_reads)

    def call(self, read: Callable[[], T]) -> T:
        """
        Return ``read()``'s result, or raise its exception, if it finishes
        within the deadline.

        Raises:
          TooManyStuckRpcReads: ``max_stuck_reads`` earlier reads are still
            running past their deadline; no new read was started.
          RpcDeadlineExceeded: this read did not finish within the deadline.
        """
        if self.stuck_read_count >= self.max_stuck_reads:
            raise TooManyStuckRpcReads(
                f"{self.max_stuck_reads} earlier RPC reads are still running "
                "past their deadline"
            )

        outcome: dict = {}

        def _run() -> None:
            try:
                outcome["result"] = read()
            except BaseException as exc:  # re-raised on the caller's thread
                outcome["error"] = exc

        worker = threading.Thread(target=_run, name="monitor-rpc-read", daemon=True)
        worker.start()
        worker.join(self.deadline_seconds)
        if worker.is_alive():
            self._stuck_reads.append(worker)
            raise RpcDeadlineExceeded(
                f"RPC read did not finish within {self.deadline_seconds:g}s"
            )
        if "error" in outcome:
            raise outcome["error"]
        return outcome["result"]


def error_retry_wait_seconds(
    seconds_until_boundary: int,
    normal_wait_seconds: int,
    near_boundary_wait_seconds: int,
    near_boundary_window_seconds: int = NEAR_BOUNDARY_WINDOW_SECONDS,
) -> int:
    """
    How long the monitor waits after a failed check before trying again.

    Within ``near_boundary_window_seconds`` before the boundary the short wait
    applies, so a recovered network is noticed within seconds. At any other
    time, including after the boundary has passed, the normal wait applies.
    """
    if 0 <= seconds_until_boundary <= near_boundary_window_seconds:
        return near_boundary_wait_seconds
    return normal_wait_seconds
