"""Persistence service for verified webhook events (Phase 2, Slice C).

Accepts ALREADY-VERIFIED webhook data (signature checking lives in
app.webhooks.signature; HTTP transport is a future layer's concern) and
persists it as a WebhookEvent row.

Idempotency is enforced by the database UNIQUE constraint on
webhook_events.event_id -- never by a SELECT-before-INSERT probe. The
service owns its INSERT transaction: commits on success, rolls back and
reports DUPLICATE only for the known event_id uniqueness violation, and
re-raises anything else after rollback so unrelated failures are never
swallowed.

Framework-independent: no HTTP, no Razorpay, no payment processing.
New events remain status="received"; a future processor slice owns
every state transition beyond that.
"""

import enum

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import WebhookEvent

# PostgreSQL SQLSTATE for unique-violation errors.
_UNIQUE_VIOLATION_SQLSTATE = "23505"
# Named UNIQUE constraint backing webhook idempotency (see app/models.py).
_EVENT_ID_CONSTRAINT_NAME = "uq_webhook_events_event_id"


class PersistOutcome(enum.Enum):
    """Explicit result of persist_event -- never a bare boolean."""

    NEW = "new"  # Row was inserted and committed by this call.
    DUPLICATE = "duplicate"  # event_id already existed; nothing changed.


def _sqlstate(dbapi_error: BaseException) -> str | None:
    """SQLSTATE from either DBAPI naming convention (or None).

    psycopg 2 exposes ``pgcode``, psycopg 3 exposes ``sqlstate``.
    """
    return (
        getattr(dbapi_error, "pgcode", None)
        or getattr(dbapi_error, "sqlstate", None)
    )


def _is_duplicate_event_id(exc: IntegrityError) -> bool:
    """True only for the event_id UNIQUE violation -- nothing else."""
    orig = exc.orig
    if _sqlstate(orig) == _UNIQUE_VIOLATION_SQLSTATE:
        # PostgreSQL: trust the server-reported constraint name when
        # diagnostics are available (psycopg exposes them via exc.orig.diag).
        constraint_name = getattr(
            getattr(orig, "diag", None), "constraint_name", None
        )
        if constraint_name is not None:
            return constraint_name == _EVENT_ID_CONSTRAINT_NAME
        # Diagnostics unavailable: fall back to the rendered message.
        return _EVENT_ID_CONSTRAINT_NAME in str(orig)
    # Other backends (e.g. SQLite in unit tests) name the violated column:
    # "UNIQUE constraint failed: webhook_events.event_id".
    return str(orig).startswith(
        f"UNIQUE constraint failed: {WebhookEvent.__tablename__}.event_id"
    )


def persist_event(
    session: Session,
    *,
    event_id: str,
    event_type: str,
    raw_payload: bytes,
) -> PersistOutcome:
    """Persist one verified webhook event exactly as received.

    Decodes raw_payload as strict UTF-8 (malformed bytes fail loudly
    before any database work; the body is stored verbatim, never
    reserialized) and inserts a WebhookEvent with status="received".

    Returns NEW after a successful commit, or DUPLICATE when PostgreSQL
    rejected the insert specifically because of
    uq_webhook_events_event_id (the failed transaction is rolled back
    and the session remains usable). Any other database error is rolled
    back and re-raised.
    """
    event = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=raw_payload.decode("utf-8"),
        status="received",
    )
    session.add(event)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        if _is_duplicate_event_id(exc):
            return PersistOutcome.DUPLICATE
        raise
    return PersistOutcome.NEW
