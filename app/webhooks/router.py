"""HTTP boundary for Razorpay webhooks (Phase 2, Slice E).

Thin adapter/orchestrator composing the already-verified slices, using
the REAL Razorpay webhook contract:

    raw request bytes
    -> Slice A: HMAC signature verification over X-Razorpay-Signature
       (raw body is never re-encoded before verification)
    -> canonical event identity from the X-Razorpay-Event-Id header
    -> minimal envelope extraction: top-level "event" field
    -> Slice C: persist_event (DB-enforced idempotency on that header id)
    -> Slice D: process_event, only when the event is NEW
    -> public 200 {"status": "accepted"}

Identity separation: X-Razorpay-Event-Id (delivery/deduplication key)
is NOT the payment entity id or order id nested inside payload. Only
the header value becomes WebhookEvent.event_id.

The router owns NO business logic, NO SQL, and NO state transitions.
Rejections fail closed before any database access; persistence and
processor internals are never exposed in responses. The webhook secret
is read from configuration, used only for verification, and never
logged, persisted, or returned.

Public response contract:
    200 {"status": "accepted"}   NEW (any processor outcome) or DUPLICATE
    400 {"detail": ...}          invalid signature / unusable request
    500 {"detail": ...}          server/configuration problem
"""

import json

from fastapi import APIRouter, HTTPException, Request

from app.config import settings
from app.database import session_factory
from app.webhooks.processor import process_event
from app.webhooks.service import PersistOutcome, persist_event
from app.webhooks.signature import verify

router = APIRouter()

_SIGNATURE_HEADER = "X-Razorpay-Signature"
_EVENT_ID_HEADER = "X-Razorpay-Event-Id"
_EVENT_TYPE_KEY = "event"


def _extract_event_type(parsed: object) -> str:
    """Extract the canonical event type from a parsed Razorpay payload.

    Razorpay carries the event type in the top-level "event" field
    ("payment.failed", "payment.captured", "order.paid", ...). Only
    presence/string/non-empty checks happen here -- whether the type is
    SUPPORTED is decided later by the processor's registry, so unknown
    types still reach persistence and are durably ignored there.
    """
    if not isinstance(parsed, dict):
        raise ValueError("payload is not a JSON object")

    event_type = parsed.get(_EVENT_TYPE_KEY)
    if not isinstance(event_type, str) or not event_type.strip():
        raise ValueError("missing or invalid event field")

    return event_type


@router.post("/webhooks/razorpay", tags=["webhooks"])
async def receive_razorpay_webhook(request: Request) -> dict[str, str]:
    """Accept one Razorpay webhook delivery exactly as it arrived."""
    raw_body = await request.body()
    signature = request.headers.get(_SIGNATURE_HEADER)

    secret = settings.RAZORPAY_WEBHOOK_SECRET
    if not secret:
        # Fail closed BEFORE verification/persistence; never reveal why
        # beyond "not configured" and never include any config values.
        raise HTTPException(
            status_code=500,
            detail="Webhook is not configured",
        )

    if not verify(raw_body, signature, secret):
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook signature",
        )

    # Canonical delivery identity lives in a HEADER, never in the body.
    event_id = request.headers.get(_EVENT_ID_HEADER)
    if not event_id or not event_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing webhook event id",
        )

    # Signature succeeded -- only now may the bytes be parsed.
    try:
        parsed = json.loads(raw_body)
        event_type = _extract_event_type(parsed)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Invalid webhook payload",
        ) from None

    with session_factory() as session:
        outcome = persist_event(
            session,
            event_id=event_id,
            event_type=event_type,
            raw_payload=raw_body,
        )
        if outcome is PersistOutcome.NEW:
            process_event(session, event_id)

    return {"status": "accepted"}
