"""Deterministic Razorpay event -> revenue domain correlation (F2).

Turns an already-verified, already-persisted WebhookEvent into domain
state:

    WebhookEvent -> Order -> PaymentAttempt(s) -> RecoveryCase

CORE PRINCIPLES
---------------
- Webhook payloads are SNAPSHOTS. "Last webhook wins" is forbidden: a
  late payment.authorized/payment.failed can never downgrade a captured
  PaymentAttempt or a paid Order.
- No fabricated financial facts. Missing identifiers/amounts yield
  CORRELATION_INCOMPLETE (the WebhookEvent stays replayable); the
  service never invents amounts, currencies, or statuses.
- Canonical data wins. Conflicting authoritative amount/currency values
  return DATA_MISMATCH without mutating stored history.
- Idempotent + race-safe: reprocessing an event is a STATE_NOOP; all
  mutations happen under SELECT ... FOR UPDATE on the canonical Order
  row (single serialization point), and external-identity creation races
  fall back to the authoritative UNIQUE constraints (reload + continue).
- Natural recovery is truthful: closing an OBSERVING case after a paid
  transition records NATURAL_RECOVERY -- no uplift math, no AI, no
  intervention logic.

TRANSACTION CONTRACT
--------------------
The caller owns the transaction (the webhook processor commits after
the handler returns; failures roll back there). This service flushes,
uses SAVEPOINTs for identity-creation races, and NEVER commits.

Result type: CorrelationOutcome -- never booleans.
"""

import enum
import json
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Order, PaymentAttempt, RecoveryCase, WebhookEvent

__all__ = [
    "CorrelationOutcome",
    "PayloadStructureError",
    "REPLAYABLE_OUTCOMES",
    "apply_webhook_to_domain",
]


class CorrelationOutcome(enum.Enum):
    """Explicit result of apply_webhook_to_domain."""

    STATE_UPDATED = "state_updated"
    STATE_NOOP = "state_noop"
    NATURAL_RECOVERY = "natural_recovery"
    CORRELATION_INCOMPLETE = "correlation_incomplete"
    DATA_MISMATCH = "data_mismatch"


# Outcomes whose WebhookEvent must REMAIN status="received" so it can be
# replayed later (the processor leaves it non-terminal).
REPLAYABLE_OUTCOMES = frozenset(
    {
        CorrelationOutcome.CORRELATION_INCOMPLETE,
        CorrelationOutcome.DATA_MISMATCH,
    }
)


class PayloadStructureError(ValueError):
    """The stored payload cannot be parsed/used for correlation."""


# ---------------------------------------------------------------------------
# Deterministic state machines
# ---------------------------------------------------------------------------

_PAYMENT_TRANSITIONS = {
    "CREATED": {"AUTHORIZED", "FAILED", "CAPTURED"},
    "AUTHORIZED": {"CAPTURED", "FAILED"},
    "FAILED": {"AUTHORIZED", "CAPTURED"},
    # CAPTURED is terminal: only CAPTURED->CAPTURED (no-op) survives.
    "CAPTURED": {"CAPTURED"},
}

_ORDER_TRANSITIONS = {
    "created": {"attempted", "paid"},
    "attempted": {"paid"},
    # paid is terminal: paid -> attempted / created is forbidden.
    "paid": {"paid"},
}

_PROPOSALS = {
    "payment.failed": "FAILED",
    "payment.authorized": "AUTHORIZED",
    "payment.captured": "CAPTURED",
    "order.paid": "CAPTURED",
}

_PAYMENT_EVENTS = frozenset(_PROPOSALS) - {"order.paid"}

ACTIVE_CASE_STATUS = "OBSERVING"
CASE_CLOSED = "CLOSED"
RESOLUTION_NATURAL_RECOVERY = "NATURAL_RECOVERY"


def _is_unique_violation(
    exc: IntegrityError,
    constraint_name: str,
    table_name: str,
    column_name: str,
) -> bool:
    """True ONLY for the named unique violation (mirrors Slice C logic)."""
    orig = exc.orig
    sqlstate = getattr(orig, "pgcode", None) or getattr(
        orig, "sqlstate", None
    )
    if sqlstate == "23505":
        constraint = getattr(
            getattr(orig, "diag", None), "constraint_name", None
        )
        if constraint is not None:
            return constraint == constraint_name
        return constraint_name in str(orig)
    # Non-PostgreSQL backends name the violated column in the message.
    return str(orig).startswith(
        f"UNIQUE constraint failed: {table_name}.{column_name}"
    )


# ---------------------------------------------------------------------------
# Snapshot extraction (deterministic, never invents values)
# ---------------------------------------------------------------------------


def _parse_payload(event: WebhookEvent) -> dict:
    try:
        parsed = json.loads(event.payload)
    except (TypeError, ValueError) as exc:
        raise PayloadStructureError(
            "event payload is not valid JSON"
        ) from exc
    if not isinstance(parsed, dict):
        raise PayloadStructureError("event payload is not a JSON object")
    return parsed


def _entity(parsed: dict, *path: str) -> dict | None:
    """Return payload.<path...>.entity when present and usable, else None."""
    node: object = parsed.get("payload")
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    if isinstance(node, dict) and node:
        return node
    return None


def _string_field(entity: dict, key: str) -> str | None:
    value = entity.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return None


def _int_field(entity: dict, key: str) -> int | None:
    value = entity.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


# ---------------------------------------------------------------------------
# Canonical-row resolution (locking + race-safe identity creation)
# ---------------------------------------------------------------------------


def _resolve_order(
    session: Session, order_ref: str, order_entity: dict | None
) -> tuple[Order | None, bool]:
    """Find-or-create the canonical Order. Returns (order, created)."""
    existing = (
        session.query(Order)
        .filter_by(razorpay_order_id=order_ref)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        return existing, False

    # Authoritative order.entity required to create -- never fabricate.
    if order_entity is None:
        return None, False
    amount = _int_field(order_entity, "amount")
    currency = _string_field(order_entity, "currency")
    entity_id = _string_field(order_entity, "id")
    if amount is None or currency is None:
        return None, False
    if entity_id is not None and entity_id != order_ref:
        # Contradictory identities inside one payload.
        return None, False

    try:
        with session.begin_nested():
            order = Order(
                razorpay_order_id=order_ref,
                amount_paise=amount,
                currency=currency,
                status="created",
            )
            session.add(order)
            session.flush()
    except IntegrityError as exc:
        if not _is_unique_violation(
            exc, "uq_orders_razorpay_order_id", "orders", "razorpay_order_id"
        ):
            raise
        # Concurrent creation won; reload the canonical row under lock.
        return _lock_order_by_pk(session, order_ref), False
    # Always continue with a freshly loaded, session-managed instance.
    # (Re-fetching after the savepoint also sidesteps pysqlite's broken
    # SAVEPOINT semantics in the unit-test environment.)
    return _lock_order_by_pk(session, order_ref), True


def _lock_order_by_pk(session: Session, razorpay_order_id: str) -> Order:
    return (
        session.query(Order)
        .filter_by(razorpay_order_id=razorpay_order_id)
        .with_for_update()
        .one()
    )


def _order_data_conflicts(order: Order, order_entity: dict) -> bool:
    """Authoritative snapshot contradicts stored canonical financials."""
    amount = _int_field(order_entity, "amount")
    currency = _string_field(order_entity, "currency")
    if amount is not None and amount != order.amount_paise:
        return True
    if currency is not None and currency != order.currency:
        return True
    return False


def _resolve_attempt(
    session: Session,
    payment_id: str,
    order_pk: int,
    amount: int,
    currency: str,
) -> tuple[PaymentAttempt | None, bool]:
    """Find-or-create the PaymentAttempt under the locked Order."""
    existing = (
        session.query(PaymentAttempt)
        .filter_by(razorpay_payment_id=payment_id)
        .with_for_update()
        .one_or_none()
    )
    if existing is not None:
        return existing, False

    try:
        with session.begin_nested():
            attempt = PaymentAttempt(
                razorpay_payment_id=payment_id,
                order_id=order_pk,
                amount_paise=amount,
                currency=currency,
                status="CREATED",
            )
            session.add(attempt)
            session.flush()
            attempt_pk = attempt.id
    except IntegrityError as exc:
        if not _is_unique_violation(
            exc,
            "uq_payment_attempts_razorpay_payment_id",
            "payment_attempts",
            "razorpay_payment_id",
        ):
            raise
        # Concurrent creation won; reload the canonical row under lock.
        return (
            session.query(PaymentAttempt)
            .filter_by(razorpay_payment_id=payment_id)
            .with_for_update()
            .one()
        ), False
    winner = (
        session.query(PaymentAttempt)
        .filter_by(id=attempt_pk)
        .with_for_update()
        .one()
    )
    return winner, True


# ---------------------------------------------------------------------------
# State transitions (snapshot semantics: no downgrades from terminal states)
# ---------------------------------------------------------------------------


def _transition_payment(attempt: PaymentAttempt, proposal: str) -> bool:
    current = attempt.status or "CREATED"
    if proposal == current:
        return False
    if proposal in _PAYMENT_TRANSITIONS.get(current, set()):
        attempt.status = proposal
        return True
    return False  # late/out-of-order snapshot: ignored, never a downgrade


def _advance_order(order: Order, paid_evidence: bool) -> bool:
    current = order.status or "created"
    target = "paid" if paid_evidence else (
        "attempted" if current == "created" else current
    )
    if target == current:
        return False
    if target in _ORDER_TRANSITIONS.get(current, set()):
        order.status = target
        return True
    return False


def _record_failure_fields(attempt: PaymentAttempt, entity: dict) -> None:
    """Write failure details only when the snapshot actually provides them."""
    code = _string_field(entity, "error_code")
    description = _string_field(entity, "error_description")
    reason = _string_field(entity, "error_reason")
    if code is not None:
        attempt.failure_code = code[:100]
    text = description if description is not None else reason
    if text is not None:
        attempt.failure_reason = text


# ---------------------------------------------------------------------------
# Recovery episode synchronisation (Order row lock serialises this)
# ---------------------------------------------------------------------------


def _sync_recovery_case(
    session: Session,
    order_pk: int,
    *,
    failure: bool,
    paid: bool,
) -> str | None:
    """Ensure <=1 active case; open on failure; close naturally on paid.

    The caller holds FOR UPDATE on the canonical Order row, so concurrent
    episodes can never both observe "no active case".
    """
    active = (
        session.query(RecoveryCase)
        .filter_by(order_id=order_pk, status=ACTIVE_CASE_STATUS)
        .all()
    )
    if len(active) > 1:  # defensive: must be impossible via this service
        raise RuntimeError(
            "data integrity violation: multiple active recovery cases "
            f"for order pk {order_pk}"
        )

    if failure:
        if active:
            return None  # reuse the existing episode
        session.add(
            RecoveryCase(
                order_id=order_pk,
                status=ACTIVE_CASE_STATUS,
                amount_at_risk_paise=_at_risk_amount(session, order_pk),
            )
        )
        return "created"

    if paid and active:
        case = active[0]
        case.status = CASE_CLOSED
        case.resolution = RESOLUTION_NATURAL_RECOVERY
        case.closed_at = datetime.now(UTC)
        return "closed_natural"

    return None


def _at_risk_amount(session: Session, order_pk: int) -> int:
    order = session.query(Order).filter_by(id=order_pk).one()
    return order.amount_paise


# ---------------------------------------------------------------------------
# Event application
# ---------------------------------------------------------------------------


def apply_webhook_to_domain(
    session: Session, event: WebhookEvent
) -> CorrelationOutcome:
    """Correlate one persisted WebhookEvent into deterministic domain state.

    Runs inside the CALLER'S transaction (no commit here). Raises on
    unusable payloads (PayloadStructureError) and unrelated database
    errors; returns CORRELATION_INCOMPLETE/DATA_MISMATCH without any
    mutation for those cases so the caller can keep the event replayable.
    """
    event_type = event.event_type
    if event_type == "order.paid":
        return _apply_order_paid(session, event)
    if event_type in _PAYMENT_EVENTS:
        return _apply_payment_event(
            session, event, _PROPOSALS[event_type]
        )
    raise ValueError(f"unsupported domain event type {event_type!r}")


def _apply_payment_event(
    session: Session, event: WebhookEvent, proposal: str
) -> CorrelationOutcome:
    parsed = _parse_payload(event)
    payment_entity = _entity(parsed, "payment", "entity")
    order_entity = _entity(parsed, "order", "entity")

    payment_id = (
        _string_field(payment_entity, "id") if payment_entity else None
    )
    if payment_id is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE

    order_ref = (
        _string_field(payment_entity, "order_id")
        if payment_entity
        else None
    )
    if order_ref is None and order_entity is not None:
        order_ref = _string_field(order_entity, "id")
    if order_ref is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE

    amount = (
        _int_field(payment_entity, "amount") if payment_entity else None
    )
    currency = (
        _string_field(payment_entity, "currency") if payment_entity else None
    )
    if amount is None or currency is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE

    mutated = False

    order, order_created = _resolve_order(session, order_ref, order_entity)
    if order is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE
    mutated = mutated or order_created
    if not order_created and order_entity is not None:
        if _order_data_conflicts(order, order_entity):
            return CorrelationOutcome.DATA_MISMATCH

    attempt, attempt_created = _resolve_attempt(
        session, payment_id, order.id, amount, currency
    )
    if not attempt_created and (
        attempt.amount_paise != amount or attempt.currency != currency
    ):
        return CorrelationOutcome.DATA_MISMATCH

    changed = _transition_payment(attempt, proposal)
    # A REJECTED failure snapshot (late/out-of-order) must not open a
    # recovery episode -- episodes exist only for accepted failures.
    accepted_failure = changed and proposal == "FAILED"
    if accepted_failure and payment_entity is not None:
        _record_failure_fields(attempt, payment_entity)
    mutated = mutated or attempt_created or changed

    order_changed = _advance_order(order, paid_evidence=proposal == "CAPTURED")
    mutated = mutated or order_changed

    case_sync = _sync_recovery_case(
        session,
        order.id,
        failure=accepted_failure,
        paid=proposal == "CAPTURED",
    )
    if case_sync == "closed_natural":
        return CorrelationOutcome.NATURAL_RECOVERY

    return (
        CorrelationOutcome.STATE_UPDATED
        if mutated
        else CorrelationOutcome.STATE_NOOP
    )


def _apply_order_paid(
    session: Session, event: WebhookEvent
) -> CorrelationOutcome:
    parsed = _parse_payload(event)
    order_entity = _entity(parsed, "order", "entity")
    payment_entity = _entity(parsed, "payment", "entity")

    order_ref = (
        _string_field(order_entity, "id") if order_entity else None
    )
    if order_ref is None and payment_entity is not None:
        order_ref = _string_field(payment_entity, "order_id")
    if order_ref is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE

    mutated = False

    order, order_created = _resolve_order(session, order_ref, order_entity)
    if order is None:
        return CorrelationOutcome.CORRELATION_INCOMPLETE
    mutated = mutated or order_created
    if not order_created and order_entity is not None:
        if _order_data_conflicts(order, order_entity):
            return CorrelationOutcome.DATA_MISMATCH

    # Attach/mark the payment attempt when the snapshot provides one.
    if payment_entity is not None:
        payment_id = _string_field(payment_entity, "id")
        amount = _int_field(payment_entity, "amount")
        currency = _string_field(payment_entity, "currency")
        if payment_id is None or amount is None or currency is None:
            return CorrelationOutcome.CORRELATION_INCOMPLETE
        attempt, attempt_created = _resolve_attempt(
            session, payment_id, order.id, amount, currency
        )
        if not attempt_created and (
            attempt.amount_paise != amount or attempt.currency != currency
        ):
            return CorrelationOutcome.DATA_MISMATCH
        # NOTE: call unconditionally -- never fold a mutating call into a
        # short-circuiting boolean expression.
        attempt_changed = _transition_payment(attempt, "CAPTURED")
        mutated = mutated or attempt_created or attempt_changed

    order_changed = _advance_order(order, paid_evidence=True)
    mutated = mutated or order_changed

    case_sync = _sync_recovery_case(
        session, order.id, failure=False, paid=True
    )
    if case_sync == "closed_natural":
        return CorrelationOutcome.NATURAL_RECOVERY

    return (
        CorrelationOutcome.STATE_UPDATED
        if mutated
        else CorrelationOutcome.STATE_NOOP
    )
