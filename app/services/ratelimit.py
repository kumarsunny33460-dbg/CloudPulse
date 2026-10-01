"""In-memory sliding window rate limiting for authentication endpoints.

Design notes
------------
* Counters live in a process local dictionary guarded by a lock, so the limiter
  adds no new infrastructure dependency and cannot fail the request path.
* Keys combine the submitted identifier with the client IP. Tracking only the
  identifier would let an attacker deliberately lock a victim's account out;
  tracking only the IP would let one attacker brute force many accounts from a
  single host without ever tripping the limit.
* A multi-worker deployment (gunicorn) gives each worker its own counters, so
  the effective limit is ``LOGIN_MAX_ATTEMPTS * workers``. Swap the store for
  Redis when a shared limit is required; the interface below is deliberately
  small enough for that.
* Successful authentication calls :func:`clear` so a legitimate user who fat
  fingered their password a few times is never left throttled.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque

logger = logging.getLogger("cloudpulse.ratelimit")

_lock = threading.Lock()
_attempts: dict[str, deque] = defaultdict(deque)
_locked_until: dict[str, float] = {}


def _prune(bucket: deque, window: float, now: float) -> bool:
    """Drop expired timestamps. Returns True when the bucket is empty after."""
    cutoff = now - window
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    return not bucket


# Key separator. An identifier containing the previous "|" separator was
# mis-parsed by _split, so purge_expired applied the wrong window to that
# bucket. A NUL cannot appear in an email address or an IP.
SEPARATOR = "\x00"


def _key(scope: str, identifier: str, client_ip: str | None) -> str:
    return SEPARATOR.join((scope, identifier.strip().lower(), client_ip or "unknown"))


def reset() -> None:
    """Clear every counter. Used by the test suite between cases."""
    with _lock:
        _attempts.clear()
        _locked_until.clear()


def record_failure(scope: str, identifier: str, client_ip: str | None) -> int:
    """Register one failed attempt and return the new failure count."""
    key = _key(scope, identifier, client_ip)

    with _lock:
        now = time.time()
        bucket = _attempts[key]
        _prune(bucket, _window(scope), now)
        bucket.append(now)
        return len(bucket)


def failure_count(scope: str, identifier: str, client_ip: str | None) -> int:
    key = _key(scope, identifier, client_ip)

    with _lock:
        bucket = _attempts.get(key)
        if not bucket:
            return 0
        _prune(bucket, _window(scope), time.time())
        return len(bucket)


def is_locked(scope: str, identifier: str, client_ip: str | None) -> bool:
    key = _key(scope, identifier, client_ip)

    with _lock:
        expiry = _locked_until.get(key)
        if expiry is None:
            return False
        if expiry <= time.time():
            _locked_until.pop(key, None)
            _attempts.pop(key, None)
            return False
        return True


def lock_remaining_seconds(scope: str, identifier: str, client_ip: str | None) -> int:
    key = _key(scope, identifier, client_ip)

    with _lock:
        expiry = _locked_until.get(key)
        if expiry is None:
            return 0
        remaining = expiry - time.time()
        if remaining <= 0:
            _locked_until.pop(key, None)
            _attempts.pop(key, None)
            return 0
        return int(remaining) + 1


def lock(scope: str, identifier: str, client_ip: str | None) -> None:
    key = _key(scope, identifier, client_ip)

    with _lock:
        _locked_until[key] = time.time() + _lockout(scope)


def clear(scope: str, identifier: str, client_ip: str | None) -> None:
    key = _key(scope, identifier, client_ip)

    with _lock:
        _attempts.pop(key, None)
        _locked_until.pop(key, None)


def retry_after_seconds(scope: str, identifier: str, client_ip: str | None) -> int:
    """Seconds until the next attempt is permitted.

    Always at least 1 while throttled. Returning 0 produced the user-facing
    "Try again in 0 seconds", which tells the caller to retry immediately and
    is the one message that cannot actually help.
    """
    if is_locked(scope, identifier, client_ip):
        return lock_remaining_seconds(scope, identifier, client_ip)

    limit = _limit(scope)
    if limit <= 0:
        return 0

    remaining = limit - failure_count(scope, identifier, client_ip)
    if remaining <= 0:
        return 1

    with _lock:
        bucket = _attempts.get(_key(scope, identifier, client_ip))
        if not bucket:
            return 0
        oldest = bucket[0]
    window = _window(scope)
    return max(1, int(oldest + window - time.time()) + 1)


def purge_expired() -> int:
    """Drop counters whose whole window has elapsed. Returns keys removed."""
    removed = 0

    with _lock:
        now = time.time()

        for key in [key for key, bucket in _attempts.items()
                    if _prune(bucket, _window(_split(key)[0]), now)]:
            _attempts.pop(key, None)
            removed += 1

        for key in [key for key, expiry in _locked_until.items() if expiry <= now]:
            _locked_until.pop(key, None)
            removed += 1

    return removed


def _split(key: str) -> tuple[str, str, str]:
    scope, identifier, client_ip = key.split(SEPARATOR, 2)
    return scope, identifier, client_ip


def _config(name: str, default):
    from flask import current_app

    try:
        value = current_app.config.get(name, default)
    except RuntimeError:
        return default
    return default if value is None else value


def _limit(scope: str) -> int:
    name = {
        "login": "LOGIN_MAX_ATTEMPTS",
        "register": "REGISTRATION_MAX_ATTEMPTS",
        "password_reset": "PASSWORD_RESET_MAX_ATTEMPTS",
    }.get(scope)
    if name is None:
        return 0
    return max(0, int(_config(name, 0)))


def _window(scope: str) -> float:
    if scope == "login":
        return float(_config("LOGIN_ATTEMPT_WINDOW_SECONDS", 300))
    if scope == "password_reset":
        return float(_config("PASSWORD_RESET_TOKEN_TTL_SECONDS", 3600))
    return float(_config("LOGIN_ATTEMPT_WINDOW_SECONDS", 300))


def _lockout(scope: str) -> float:
    if scope == "register":
        return float(_config("LOGIN_ATTEMPT_WINDOW_SECONDS", 300))
    return float(_config("LOGIN_LOCKOUT_SECONDS", 900))


def over_limit(scope: str, identifier: str, client_ip: str | None) -> bool:
    """True when a new attempt must be refused.

    A limit of ``0`` disables throttling for the scope, which is how the test
    configuration opts out.
    """
    limit = _limit(scope)
    if limit <= 0:
        return False
    return failure_count(scope, identifier, client_ip) >= limit


def reserve(scope: str, identifier: str, client_ip: str | None) -> bool:
    """Claim one attempt slot, returning False when the limit is already reached.

    Checking the limit and recording the attempt are two separate steps, and
    the password hash between them takes tens of milliseconds. Parallel requests
    therefore all pass a naive ``over_limit`` check before any of them records
    a failure, so an attacker sends a burst and defeats the limiter entirely.
    Reserving the slot up front closes that window: the budget is consumed
    before the expensive work starts.

    A successful authentication calls :func:`clear` to return the reservation.
    """
    limit = _limit(scope)
    if limit <= 0:
        return True

    key = _key(scope, identifier, client_ip)

    with _lock:
        now = time.time()
        bucket = _attempts.setdefault(key, deque())
        _prune(bucket, _window(scope), now)

        if len(bucket) >= limit:
            return False

        bucket.append(now)
        return True
