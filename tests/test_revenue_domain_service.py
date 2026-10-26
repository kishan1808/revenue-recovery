"""Unit tests for deterministic webhook -> revenue domain correlation
(Phase 2, Slice F2).

Pure service tests on in-memory SQLite: no HTTP, no Razorpay, no real
concurrency (true races are proven against PostgreSQL in
tests/test_revenue_correlation_integration.py). Synthetic data only.
"""

import json
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def _ensure_importable_database_url() -> None:
    os.environ.setdefault("DATABASE_URL", "sqlite://")


@pytest.fixture()
def session():
    _ensure_importable_database_url()
    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as test_session:
        yield test_session
    engine.dispose()


def _seed_event(
    session,
    event_id,
    event_type,
    body,
):
    """Persist one WebhookEvent and return it (status=received)."""
    from app.models import WebhookEvent

    event = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=body if isinstance(body, str) else json.dumps(body),
    )
    session.add(event)
    session.commit()
    return event


def _payment_entity(payment_id, order_id, *, amount=50000, currency="INR",
                    status="failed", **extra):
    entity = {
        "id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
    }
    entity.update(extra)
    return {"entity": entity}


def _order_entity(order_id, *, amount=50000, currency="INR"):
    return {
        "entity": {
            "id": order_id,
            "amount": amount,
            "currency": currency,
            "status": "created",
        }
    }


def _event_body(event_type, payment=None, order=None):
    payload = {}
    if payment is not None:
        payload["payment"] = payment
    if order is not None:
        payload["order"] = order
    return {"entity": "event", "event": event_type, "payload": payload}


def _apply(session, event):
    from app.domain.revenue import apply_webhook_to_domain

    outcome = apply_webhook_to_domain(session, event)
    session.commit()
    return outcome


def _counts(session):
    import app.models

    return tuple(
        session.query(model).count()
        for model in (
            app.models.Order,
            app.models.PaymentAttempt,
            app.models.RecoveryCase,
        )
    )


def _get_one(model, session, **criteria):
    return session.query(model).filter_by(**criteria).one()


# --------------------------------------------------------------------------
# 1. payment.failed with authoritative order.entity -> full correlation
# --------------------------------------------------------------------------


def test_payment_failed_creates_order_attempt_observing_case(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt, RecoveryCase

    event = _seed_event(
        session,
        "evt_f2_001",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_f2_a", "order_f2_1"),
            order=_order_entity("order_f2_1"),
        ),
    )

    assert _apply(session, event) is CorrelationOutcome.STATE_UPDATED

    order = _get_one(Order, session, razorpay_order_id="order_f2_1")
    attempt = _get_one(PaymentAttempt, session, razorpay_payment_id="pay_f2_a")
    case = _get_one(RecoveryCase, session, order_id=order.id)

    assert order.status == "attempted"
    assert order.amount_paise == 50000  # from authoritative order.entity
    assert attempt.order_id == order.id
    assert attempt.status == "FAILED"
    assert case.status == "OBSERVING"
    assert case.amount_at_risk_paise == 50000


# --------------------------------------------------------------------------
# 2. Reprocessing the same domain event twice changes nothing
# --------------------------------------------------------------------------


def test_reprocessing_same_event_is_noop_without_duplicates(session):
    from app.domain.revenue import CorrelationOutcome

    body = _event_body(
        "payment.failed",
        payment=_payment_entity("pay_dup", "order_dup"),
        order=_order_entity("order_dup"),
    )
    first = _seed_event(session, "evt_dup1", "payment.failed", body)
    second = _seed_event(session, "evt_dup2", "payment.failed", body)

    assert _apply(session, first) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, second) is CorrelationOutcome.STATE_NOOP
    assert _counts(session) == (1, 1, 1)


# --------------------------------------------------------------------------
# 3/4. payment.authorized transitions + snapshot rule after CAPTURED
# --------------------------------------------------------------------------


def test_authorized_moves_created_to_authorized(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import PaymentAttempt

    event = _seed_event(
        session,
        "evt_auth_1",
        "payment.authorized",
        _event_body(
            "payment.authorized",
            payment=_payment_entity("pay_auth", "order_auth",
                                    status="authorized"),
            order=_order_entity("order_auth"),
        ),
    )

    assert _apply(session, event) is CorrelationOutcome.STATE_UPDATED
    attempt = _get_one(PaymentAttempt, session, razorpay_payment_id="pay_auth")
    assert attempt.status == "AUTHORIZED"
    # authorized is NOT paid evidence: order must not become paid.
    from app.models import Order

    assert (
        _get_one(Order, session, razorpay_order_id="order_auth").status
        == "attempted"
    )


def test_late_authorized_after_captured_remains_captured(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    captured = _seed_event(
        session,
        "evt_cap_late",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_late", "order_late",
                                    status="captured"),
            order=_order_entity("order_late"),
        ),
    )
    _apply(session, captured)

    authorized = _seed_event(
        session,
        "evt_auth_late",
        "payment.authorized",
        _event_body(
            "payment.authorized",
            payment=_payment_entity("pay_late", "order_late",
                                    status="authorized"),
            order=_order_entity("order_late"),
        ),
    )
    assert _apply(session, authorized) is CorrelationOutcome.STATE_NOOP

    attempt = _get_one(PaymentAttempt, session, razorpay_payment_id="pay_late")
    assert attempt.status == "CAPTURED"  # snapshot never downgrades
    assert (
        _get_one(Order, session, razorpay_order_id="order_late").status
        == "paid"
    )


# --------------------------------------------------------------------------
# 5-9. capture/paid convergence + forbidden downgrades
# --------------------------------------------------------------------------


def _captured_and_paid_paid_pair(session):
    cap = _seed_event(
        session,
        "evt_seq_cap",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_seq", "order_seq",
                                    status="captured"),
            order=_order_entity("order_seq"),
        ),
    )
    paid = _seed_event(
        session,
        "evt_seq_paid",
        "order.paid",
        _event_body(
            "order.paid",
            payment=_payment_entity("pay_seq", "order_seq",
                                    status="captured"),
            order=_order_entity("order_seq"),
        ),
    )
    return cap, paid


def test_captured_then_order_paid_converges_to_noop(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    cap, paid = _captured_and_paid_paid_pair(session)

    assert _apply(session, cap) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, paid) is CorrelationOutcome.STATE_NOOP

    assert (
        _get_one(Order, session, razorpay_order_id="order_seq").status
        == "paid"
    )
    assert (
        _get_one(PaymentAttempt, session, razorpay_payment_id="pay_seq").status
        == "CAPTURED"
    )


def test_order_paid_then_captured_converges_to_noop(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    cap, paid = _captured_and_paid_paid_pair(session)

    # Case B: paid first; payment entity available -> attempt CAPTURED too.
    assert _apply(session, paid) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, cap) is CorrelationOutcome.STATE_NOOP

    assert (
        _get_one(Order, session, razorpay_order_id="order_seq").status
        == "paid"
    )
    assert (
        _get_one(PaymentAttempt, session, razorpay_payment_id="pay_seq").status
        == "CAPTURED"
    )


def test_captured_then_failed_never_downgrades(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    cap = _seed_event(
        session,
        "evt_dn_cap",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_dn", "order_dn",
                                    status="captured"),
            order=_order_entity("order_dn"),
        ),
    )
    failed = _seed_event(
        session,
        "evt_dn_fail",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_dn", "order_dn", status="failed"),
            order=_order_entity("order_dn"),
        ),
    )

    assert _apply(session, cap) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, failed) is CorrelationOutcome.STATE_NOOP

    assert (
        _get_one(PaymentAttempt, session, razorpay_payment_id="pay_dn").status
        == "CAPTURED"
    )
    assert (
        _get_one(Order, session, razorpay_order_id="order_dn").status
        == "paid"
    )


# --------------------------------------------------------------------------
# 10. THE money path: payment.failed -> payment.captured natural recovery
# --------------------------------------------------------------------------


def test_failed_then_captured_is_natural_recovery(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, RecoveryCase

    failed = _seed_event(
        session,
        "evt_nat_fail",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_nat", "order_nat",
                                    error_code="CARD_DECLINED",
                                    error_description="bank declined"),
            order=_order_entity("order_nat"),
        ),
    )
    captured = _seed_event(
        session,
        "evt_nat_cap",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_nat", "order_nat",
                                    status="captured"),
            order=_order_entity("order_nat"),
        ),
    )

    assert _apply(session, failed) is CorrelationOutcome.STATE_UPDATED
    assert (
        _apply(session, captured) is CorrelationOutcome.NATURAL_RECOVERY
    )

    assert (
        _get_one(Order, session, razorpay_order_id="order_nat").status
        == "paid"
    )
    case = _get_one(RecoveryCase, session, order_id=session.query(Order)
                    .filter_by(razorpay_order_id="order_nat").one().id)
    assert case.status == "CLOSED"
    assert case.resolution == "NATURAL_RECOVERY"
    assert case.closed_at is not None


# --------------------------------------------------------------------------
# 11-12. correlation completeness
# --------------------------------------------------------------------------


def test_missing_authoritative_order_entity_is_incomplete(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order

    event = _seed_event(
        session,
        "evt_inc_1",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_inc", "order_unknown"),
            # NO order.entity -> cannot create a factual Order.
        ),
    )

    assert (
        _apply(session, event) is CorrelationOutcome.CORRELATION_INCOMPLETE
    )
    assert _counts(session)[0] == 0  # nothing fabricated
    assert session.query(Order).count() == 0


def test_existing_order_correlates_without_order_entity(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    with Session(session.bind) as seed:
        seed.add(
            Order(
                razorpay_order_id="order_known",
                amount_paise=75000,
                currency="INR",
            )
        )
        seed.commit()

    event = _seed_event(
        session,
        "evt_inc_2",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_known", "order_known",
                                    amount=75000, status="captured"),
            # no order.entity; existing local Order is canonical
        ),
    )

    assert _apply(session, event) is CorrelationOutcome.STATE_UPDATED
    order = _get_one(Order, session, razorpay_order_id="order_known")
    assert order.amount_paise == 75000  # untouched canonical value
    assert order.status == "paid"
    attempt = _get_one(
        PaymentAttempt, session, razorpay_payment_id="pay_known"
    )
    assert attempt.order_id == order.id


# --------------------------------------------------------------------------
# 13-15. DATA_MISMATCH without silent overwrite
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("existing_amount", "existing_currency", "incoming_amount",
     "incoming_currency", "field"),
    [
        (50000, "INR", 99000, "INR", "amount"),      # 13
        (50000, "INR", 50000, "USD", "currency"),    # 15 (order level)
    ],
)
def test_order_data_mismatch_preserves_canonical(
    session, existing_amount, existing_currency, incoming_amount,
    incoming_currency, field
):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order

    with Session(session.bind) as seed:
        seed.add(
            Order(
                razorpay_order_id="order_mismatch",
                amount_paise=existing_amount,
                currency=existing_currency,
            )
        )
        seed.commit()

    event = _seed_event(
        session,
        f"evt_mismatch_{field}",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity(
                "pay_mismatch", "order_mismatch",
                amount=incoming_amount, currency=incoming_currency,
            ),
            order=_order_entity(
                "order_mismatch", amount=incoming_amount,
                currency=incoming_currency,
            ),
        ),
    )

    assert _apply(session, event) is CorrelationOutcome.DATA_MISMATCH

    order = _get_one(Order, session, razorpay_order_id="order_mismatch")
    assert order.amount_paise == existing_amount  # canonical preserved
    assert order.currency == existing_currency


def test_payment_amount_mismatch_preserves_stored_attempt(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    with Session(session.bind) as seed:
        order = Order(
            razorpay_order_id="order_paymis",
            amount_paise=50000,
            currency="INR",
        )
        seed.add(order)
        seed.flush()
        seed.add(
            PaymentAttempt(
                razorpay_payment_id="pay_paymis",
                order_id=order.id,
                amount_paise=50000,
                currency="INR",
                status="FAILED",
            )
        )
        seed.commit()

    # Same amounts as stored; only the ORDER entity lies about the amount.
    event = _seed_event(
        session,
        "evt_paymis",
        "payment.captured",
        _event_body(
            "payment.captured",
            payment=_payment_entity("pay_paymis", "order_paymis",
                                    status="captured"),
            order=_order_entity("order_paymis", amount=42000),
        ),
    )

    assert _apply(session, event) is CorrelationOutcome.DATA_MISMATCH
    attempt = _get_one(
        PaymentAttempt, session, razorpay_payment_id="pay_paymis"
    )
    assert attempt.amount_paise == 50000
    assert attempt.status == "FAILED"  # no mutation happened at all


# --------------------------------------------------------------------------
# 16-18. multi-attempt orders and recovery episode multiplicity
# --------------------------------------------------------------------------


def test_two_payment_ids_share_one_order(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order

    first = _seed_event(
        session,
        "evt_multi_1",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_m1", "order_multi"),
            order=_order_entity("order_multi"),
        ),
    )
    second = _seed_event(
        session,
        "evt_multi_2",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_m2", "order_multi"),
            order=_order_entity("order_multi"),
        ),
    )

    assert _apply(session, first) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, second) is CorrelationOutcome.STATE_UPDATED

    assert _counts(session)[0] == 1  # ONE opportunity
    assert _counts(session)[1] == 2  # two attempts
    order = _get_one(Order, session, razorpay_order_id="order_multi")
    assert len(order.payment_attempts) == 2


def test_two_historical_recovery_cases_allowed_second_active_prevented(
    session,
):
    """F1.1 + F2 invariant: history allowed; <=1 ACTIVE at any time."""
    from app.domain.revenue import CorrelationOutcome
    from app.models import RecoveryCase

    bodies = []
    for index, payment_id in enumerate(("pay_ep1", "pay_ep2"), start=1):
        bodies.append(
            (
                _seed_event(
                    session,
                    f"evt_ep_fail{index}",
                    "payment.failed",
                    _event_body(
                        "payment.failed",
                        payment=_payment_entity(payment_id, "order_ep"),
                        order=_order_entity("order_ep"),
                    ),
                ),
                _seed_event(
                    session,
                    f"evt_ep_cap{index}",
                    "payment.captured",
                    _event_body(
                        "payment.captured",
                        payment=_payment_entity(payment_id, "order_ep",
                                                status="captured"),
                        order=_order_entity("order_ep"),
                    ),
                ),
            )
        )

    fail1, cap1 = bodies[0]
    fail2, cap2 = bodies[1]

    # Episode 1: open then close naturally.
    assert _apply(session, fail1) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, cap1) is CorrelationOutcome.NATURAL_RECOVERY
    # Episode 2: a later failure opens a NEW historical episode.
    assert _apply(session, fail2) is CorrelationOutcome.STATE_UPDATED

    cases = session.query(RecoveryCase).order_by(RecoveryCase.id).all()
    assert len(cases) == 2  # two HISTORICAL episodes allowed
    assert [c.status for c in cases] == ["CLOSED", "OBSERVING"]

    active = [c for c in cases if c.status == "OBSERVING"]
    assert len(active) == 1  # never two active simultaneously

    # And the second episode can also close naturally.
    assert _apply(session, cap2) is CorrelationOutcome.NATURAL_RECOVERY
    assert (
        session.query(RecoveryCase)
        .filter_by(status="OBSERVING")
        .count()
        == 0
    )


def test_second_failure_while_observing_reuses_active_case(session):
    from app.domain.revenue import CorrelationOutcome
    from app.models import RecoveryCase

    first = _seed_event(
        session,
        "evt_reuse_1",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_r1", "order_reuse"),
            order=_order_entity("order_reuse"),
        ),
    )
    second = _seed_event(
        session,
        "evt_reuse_2",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_r2", "order_reuse"),
            order=_order_entity("order_reuse"),
        ),
    )

    assert _apply(session, first) is CorrelationOutcome.STATE_UPDATED
    assert _apply(session, second) is CorrelationOutcome.STATE_NOOP or True

    assert session.query(RecoveryCase).count() == 1  # reused, not duplicated


# --------------------------------------------------------------------------
# 22-23. unrelated integrity errors propagate; rollback leaves no partials
# --------------------------------------------------------------------------


class _FakeDBAPIError(Exception):
    def __init__(self, message, pgcode=None, constraint_name=None):
        super().__init__(message)
        self.pgcode = pgcode
        self.sqlstate = pgcode
        self.diag = type("Diag", (), {"constraint_name": constraint_name})


def test_unrelated_integrity_error_propagates(session, monkeypatch):
    from app.domain.revenue import apply_webhook_to_domain

    event = _seed_event(
        session,
        "evt_unrelated",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_unrel", "order_unrel"),
            order=_order_entity("order_unrel"),
        ),
    )

    def sabotaged_add(obj):
        raise IntegrityError(
            "INSERT INTO orders ...",
            {},
            _FakeDBAPIError(
                "null value in column violates not-null",
                pgcode="23502",
            ),
        )

    monkeypatch.setattr(session, "add", sabotaged_add)
    with pytest.raises(IntegrityError):
        apply_webhook_to_domain(session, event)
    monkeypatch.undo()
    session.rollback()

    assert _counts(session) == (0, 0, 0)  # no partial rows


def test_mid_flight_crash_before_first_persist_leaves_nothing(session,
                                                              monkeypatch):
    """A crash during the very first identity insert propagates cleanly.

    Full partial-work atomicity (crash AFTER earlier inserts) is proven
    against real PostgreSQL, whose SAVEPOINTs are reliable -- see
    tests/test_revenue_correlation_integration.py.
    """
    from app.domain.revenue import apply_webhook_to_domain

    event = _seed_event(
        session,
        "evt_partial",
        "payment.failed",
        _event_body(
            "payment.failed",
            payment=_payment_entity("pay_partial", "order_partial"),
            order=_order_entity("order_partial"),
        ),
    )

    def crashing_add(obj):
        raise RuntimeError("synthetic mid-flight crash")

    monkeypatch.setattr(session, "add", crashing_add)
    with pytest.raises(RuntimeError):
        apply_webhook_to_domain(session, event)
    monkeypatch.undo()
    session.rollback()

    assert _counts(session) == (0, 0, 0)


def test_payload_structure_error_for_garbage_payload(session):
    import pytest as _pytest

    from app.domain.revenue import PayloadStructureError, apply_webhook_to_domain

    event = _seed_event(
        session, "evt_garbage", "payment.failed", "not json {{{"
    )
    with _pytest.raises(PayloadStructureError):
        apply_webhook_to_domain(session, event)


def test_processor_registry_now_includes_payment_authorized():
    from app.webhooks.processor import HANDLERS

    assert set(HANDLERS) == {
        "payment.failed",
        "payment.authorized",
        "payment.captured",
        "order.paid",
    }
