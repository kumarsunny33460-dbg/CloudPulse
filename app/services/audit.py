"""Audit trail helper.

Writes are best effort: an audit failure must never roll back or break the
business operation that triggered it, so problems are logged instead of raised.
"""

from __future__ import annotations

import json
import logging
from ipaddress import ip_address, ip_network

from flask import current_app, g, has_request_context, request
from models import AuditLog, db

logger = logging.getLogger("cloudpulse.audit")


def _setting(name: str, default=None):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:
        return default


def _parse_network(raw: str) -> set:
    """Parse a comma separated list of addresses or CIDR ranges."""
    networks = set()
    for chunk in (raw or "").split(","):
        entry = chunk.strip()
        if not entry:
            continue
        try:
            networks.add(ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring malformed entry in a trusted proxy setting: %r", entry)
    return networks


def _trusted_proxy_networks() -> set:
    """Proxy ranges whose X-Forwarded-For headers are believed."""
    return _parse_network(_setting("TRUSTED_PROXY_NETWORKS", ""))


def _is_trusted_proxy(address: str, trusted: set) -> bool:
    if not address:
        return False
    try:
        parsed = ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in trusted)


def client_ip() -> str | None:
    """Best-effort client IP.

    ``X-Forwarded-For`` is attacker controlled: anyone can send an arbitrary
    value. Trusting it unconditionally lets a client mint a new identity per
    request, which defeats anything keyed on the client address, including the
    login rate limiter. The header is therefore only read when the immediate
    peer is itself a configured trusted proxy. Otherwise ``remote_addr`` is used,
    which is the only value the socket layer guarantees.
    """
    if not has_request_context():
        return None

    peer = request.remote_addr
    trusted = _trusted_proxy_networks()

    if trusted and _is_trusted_proxy(peer, trusted):
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            # Walk the chain right to left and return the first address that is
            # not itself a trusted proxy. A client can prepend arbitrary entries,
            # so only the ones to the right of our own proxies are meaningful.
            for candidate in reversed(
                [entry.strip() for entry in forwarded.split(",") if entry.strip()]
            ):
                if not _is_trusted_proxy(candidate, trusted):
                    return candidate
            # Every entry is a trusted proxy, so the header identifies no
            # client. Falling back to its first entry would be trusting an
            # attacker-controlled value, which is what this check exists to
            # prevent, so the peer address is used instead.
            logger.debug(
                "X-Forwarded-For contained only trusted proxies; using the peer address"
            )

    return peer


def current_actor() -> tuple[int | None, str | None]:
    if has_request_context():
        user_id = g.get("user_id")
        if user_id:
            return user_id, g.get("username")
    return None, None


def record(
    action: str,
    *,
    entity_type: str | None = None,
    entity_id: int | None = None,
    summary: str | None = None,
    detail: dict | str | None = None,
    commit: bool = True,
) -> None:
    """Persist a single audit entry.

    Commits immediately by default because audit calls always happen after the
    business transaction has been committed; keeping the row in that
    transaction would discard it when the request scoped session is torn down.
    """
    actor_id, actor_name = current_actor()

    if isinstance(detail, dict):
        detail_text = json.dumps(detail, default=str, sort_keys=True)
    else:
        detail_text = detail

    try:
        entry = AuditLog(
            actor_id=actor_id,
            actor_name=actor_name,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            summary=(summary or action)[:500],
            detail=detail_text,
            ip_address=client_ip(),
        )
        db.session.add(entry)

        if commit:
            db.session.commit()
        else:
            db.session.flush()
    except Exception:  # pragma: no cover - defensive
        logger.exception("Failed to record audit entry | Action: %s", action)
        db.session.rollback()
