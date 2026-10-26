"""Deterministic processing for persisted webhook events (Phase 2).

Takes an ALREADY-PERSISTED WebhookEvent with status="received" and moves
it through a minimal lifecycle:

    received -> processed   (known event type, handler succeeded)
    received -> ignored     (valid but unsupported event type)
    received -> failed      (handler raised; safe error recorded)
    received -> received    (REPLAY_PENDING: handler asked to keep the
                             event replayable -- e.g. domain correlation
                             was incomplete or reported a data mismatch)

Terminal states (processed / ignored / failed) are never re-entered:
callers receive ALREADY_TERMINAL instead. A REPLAY_PENDING event stays
status="received" and remains fully replayable. A failed event also
remains conceptually replayable -- the durable row is never deleted or
replaced, so a future replay tool may reset it explicitly.

Since Slice F2 the supported handlers delegate to the revenue domain
service (app.domain.revenue), which performs deterministic
Order/PaymentAttempt/RecoveryCase correlation inside the SAME
transaction; this module still owns no business logic itself. The
signature-verification and persistence layers (Slices A/C) are untouched.

Framework-independent: no HTTP, no Razorpay SDK, no queues. Reusable by
the HTTP webhook path, a replay script, and a future background worker.
Callers must pass a FRESH session: the persistence transaction belongs
to the persistence service (Slice C), never to this module.

DOMAIN STATE MACHINE NOTE:
The payment/order/recovery state machine lives in app/domain/revenue.py.
This module only dispatches known event types into it and records the
lifecycle outcome.
"""

import enum
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

# Re-exported for backwards compatibility (Slice D tests import it here).
from app.domain.revenue import PayloadStructureError  # noqa: F401
from app.models import WebhookEvent

# WebhookEvent.status values (see app/models.py).
STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_IGNORED = "ignored"
STATUS_FAILED = "failed"

_TERMINAL_STATUSES = frozenset(
    {STATUS_PROCESSED, STATUS_IGNORED, STATUS_FAILED}
)

# Upper bound for text stored in WebhookEvent.error: bounded, single-line,
# free of stack traces and internal details.
_MAX_ERROR_LENGTH = 500


class ProcessOutcome(enum.Enum):
    """Explicit result of process_event -- never a bare boolean."""

    PROCESSED = "processed"  # Known type; handler ran successfully.
    IGNORED = "ignored"  # Valid event whose type has no handler yet.
    FAILED = "failed"  # Handler raised; safe error text was recorded.
    ALREADY_TERMINAL = "already_terminal"  # Not in "received"; untouched.
    REPLAY_PENDING = "replay_pending"  # Kept status="received" on purpose.


class ReplayPendingError(Exception):
    """A handler deliberately left the event non-terminal/replayable."""


# Handler contract: given the caller's session and the loaded event, do
# its work or raise. Returning normally means success; raising
# ReplayPending marks the event as intentionally still-replayable.
Handler = Callable[[Session, WebhookEvent], None]


def _apply_domain_event(session: Session, event: WebhookEvent) -> None:
    """Delegate a supported event to the F2 revenue domain service."""
    from app.domain.revenue import (
        REPLAYABLE_OUTCOMES,
        apply_webhook_to_domain,
    )

    outcome = apply_webhook_to_domain(session, event)
    if outcome in REPLAYABLE_OUTCOMES:
        raise ReplayPendingError(
            f"domain correlation left the event replayable: {outcome.value}"
        )


HANDLERS: dict[str, Handler] = {
    "payment.failed": _apply_domain_event,
    "payment.authorized": _apply_domain_event,
    "payment.captured": _apply_domain_event,
    "order.paid": _apply_domain_event,
}


def _bounded_error_text(exc: BaseException) -> str:
    """Safe, single-line, bounded description of a handler failure."""
    message = str(exc).strip()
    raw = (
        f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    )
    collapsed = " ".join(raw.split())
    if len(collapsed) <= _MAX_ERROR_LENGTH:
        return collapsed
    suffix = " ...[truncated]"
    keep = _MAX_ERROR_LENGTH - len(suffix)
    return collapsed[:keep] + suffix


def _record_failure(
    session: Session, event_id: str, exc: BaseException
) -> None:
    """Persist FAILED state in its own fresh, safe transaction.

    The caller must already have rolled back the handler transaction.
    """
    event = session.query(WebhookEvent).filter_by(event_id=event_id).one()
    event.status = STATUS_FAILED
    event.error = _bounded_error_text(exc)
    event.processed_at = datetime.now(UTC)
    session.commit()


def process_event(session: Session, event_id: str) -> ProcessOutcome:
    """Process one persisted webhook event by its unique event_id.

    Loads the durable WebhookEvent, refuses to touch terminal events,
    dispatches through HANDLERS, then commits the resulting status.

    Raises ValueError when no event exists for event_id or when the row
    carries an unrecognized status value. Handler failures are rolled
    back and recorded as failed via _record_failure -- exceptions are
    never silently swallowed. REPLAY_PENDING outcomes roll back too and
    leave the event at status="received" for later replay.
    """
    event = (
        session.query(WebhookEvent).filter_by(event_id=event_id).one_or_none()
    )
    if event is None:
        raise ValueError(f"no WebhookEvent found for event_id {event_id!r}")

    if event.status != STATUS_RECEIVED:
        if event.status in _TERMINAL_STATUSES:
            return ProcessOutcome.ALREADY_TERMINAL
        raise ValueError(
            f"event {event_id!r} has unexpected status {event.status!r}"
        )

    handler = HANDLERS.get(event.event_type)
    try:
        if handler is None:
            outcome = ProcessOutcome.IGNORED
        else:
            handler(session, event)
            outcome = ProcessOutcome.PROCESSED
    except ReplayPendingError:
        session.rollback()  # discard any partial correlation work
        return ProcessOutcome.REPLAY_PENDING
    except Exception as exc:
        session.rollback()
        _record_failure(session, event_id, exc)
        return ProcessOutcome.FAILED

    event.status = (
        STATUS_PROCESSED
        if outcome is ProcessOutcome.PROCESSED
        else STATUS_IGNORED
    )
    event.processed_at = datetime.now(UTC)
    session.commit()
    return outcome
