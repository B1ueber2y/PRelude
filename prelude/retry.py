"""Retrying temporary failures (network blips, overloaded services) with backoff."""

from __future__ import annotations

import logging
import socket
import time
import urllib.error
from collections.abc import Callable

log = logging.getLogger(__name__)

_TRANSIENT_TEXT = (
    "overloaded",
    "529",
    "internal server error",
    "bad gateway",
    "service unavailable",
    "gateway timeout",
    "econnreset",
    "connection reset",
    "could not resolve host",
    "temporary failure in name resolution",
    "early eof",
    "rpc failed",
    "the remote end hung up",
    "operation timed out",
)


def is_transient(exc: BaseException) -> bool:
    """True for failures worth trying again soon. Long Claude timeouts are not (they'd just repeat)."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (429, 500, 502, 503, 504):
            return True
        return exc.code == 403 and "rate limit" in str(exc.reason).lower()
    if isinstance(exc, urllib.error.URLError | ConnectionError | socket.timeout | TimeoutError):
        return True
    text = str(exc).lower()
    if "timed out after" in text:  # our own Claude run timeout
        return False
    return any(marker in text for marker in _TRANSIENT_TEXT)


def retry[T](
    fn: Callable[[], T],
    *,
    what: str,
    attempts: int = 3,
    delay: float = 5.0,
    transient: Callable[[BaseException], bool] = is_transient,
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call `fn`; on a transient failure wait (delay, 3*delay, ...) and try again."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts or not transient(e):
                raise
            wait = delay * (3 ** (attempt - 1))
            log.warning("%s failed (%s); retrying in %.0fs (%d/%d)", what, e, wait, attempt, attempts - 1)
            (sleep or time.sleep)(wait)
    raise AssertionError("unreachable")
