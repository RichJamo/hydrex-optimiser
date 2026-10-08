"""
Heartbeat pings from the boundary monitor to an outside dead-man's switch.

Why this exists (2026-10-07): the home internet went down an hour before the
boundary, the monitor missed every phase, and nothing told anyone. An alert
sent from the monitor cannot fix that, because the monitor is offline too.
Instead the monitor pings an outside service (healthchecks.io) while it is
healthy; when the pings stop, the service raises the alert from outside.

Protocol (https://healthchecks.io/docs/http_api/): a request to the check URL
reports "alive"; a request to the check URL plus ``/fail`` reports "failing",
which alerts at once instead of waiting for the grace period. The request
body is shown in the healthchecks.io event log.

A ping must never stall the monitor. Each one is sent on a daemon thread, and
while a previous ping is still in flight (for example stuck in a DNS lookup)
new ones are dropped rather than queued.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

import requests

HEARTBEAT_URL_ENV = "MONITOR_HEARTBEAT_URL"
PING_TIMEOUT_SECONDS = 5.0


class NoHeartbeat:
    """Heartbeat used when no URL is configured: reports nothing."""

    description = f"disabled ({HEARTBEAT_URL_ENV} not set)"

    def report_alive(self, status: str) -> None:
        pass

    def report_failing(self, reason: str) -> None:
        pass


class HealthchecksHeartbeat:
    """
    Pings a healthchecks.io check, at most once per ``min_interval_seconds``.

    "Alive" and "failing" pings share one rate limit, so a long outage of the
    RPC provider alone (home internet still up) sends one ``/fail`` ping per
    interval, not one per retry.

    Invariant: at most one ping is in flight. ``report_*`` never blocks the
    caller for longer than it takes to start a thread.
    """

    def __init__(
        self,
        ping_url: str,
        min_interval_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
        send: Optional[Callable[[str, str], None]] = None,
    ) -> None:
        if not ping_url.startswith("https://"):
            raise ValueError(f"{HEARTBEAT_URL_ENV} must be an https:// URL")
        if min_interval_seconds <= 0:
            raise ValueError("min_interval_seconds must be > 0")
        self._ping_url = ping_url.rstrip("/")
        self._min_interval_seconds = float(min_interval_seconds)
        self._clock = clock
        self._send = send or _post_ping
        self._last_sent_at: Optional[float] = None
        self._in_flight: Optional[threading.Thread] = None
        self.description = "enabled (healthchecks.io)"

    def report_alive(self, status: str) -> None:
        """Report a successful check. ``status`` appears in the event log."""
        self._maybe_send(self._ping_url, status)

    def report_failing(self, reason: str) -> None:
        """Report that checks are failing; healthchecks.io alerts immediately."""
        self._maybe_send(f"{self._ping_url}/fail", reason)

    def _maybe_send(self, url: str, body: str) -> None:
        now = self._clock()
        if (
            self._last_sent_at is not None
            and now - self._last_sent_at < self._min_interval_seconds
        ):
            return
        if self._in_flight is not None and self._in_flight.is_alive():
            return
        self._last_sent_at = now
        self._in_flight = threading.Thread(
            target=self._send_quietly,
            args=(url, body),
            name="monitor-heartbeat",
            daemon=True,
        )
        self._in_flight.start()

    def _send_quietly(self, url: str, body: str) -> None:
        try:
            self._send(url, body)
        except Exception:
            # A failed ping is itself the signal: healthchecks.io alerts when pings stop.
            pass


def _post_ping(url: str, body: str) -> None:
    requests.post(url, data=body.encode("utf-8")[:10_000], timeout=PING_TIMEOUT_SECONDS)


def heartbeat_from_env(min_interval_seconds: float = 60.0):
    """Return a HealthchecksHeartbeat if MONITOR_HEARTBEAT_URL is set, else NoHeartbeat."""
    ping_url = os.getenv(HEARTBEAT_URL_ENV, "").strip()
    if not ping_url:
        return NoHeartbeat()
    return HealthchecksHeartbeat(ping_url, min_interval_seconds=min_interval_seconds)
