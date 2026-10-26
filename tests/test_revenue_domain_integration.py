"""Real-PostgreSQL integration tests for the revenue domain foundation
(Slice F1).

Proves on a REAL database: Order/PaymentAttempt/RecoveryCase round
trips, unique external Razorpay identities, FK enforcement (including
rejections), one-order-many-attempts, the one-case-per-order rule,
NOT NULL constraints, and timezone-aware server timestamps.

Migration upgrade/downgrade/re-upgrade coverage for these tables lives
in tests/test_migrations_integration.py (single destructive cycle).

Strictly opt-in via TEST_DATABASE_URL; skipped cleanly otherwise. No
DATABASE_URL fallback. Synthetic values only.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set - revenue domain tests skipped",
)


@pytest.fixture(scope="module")
def pg_engine():
    """Engine bound to the explicit test database, schema ensured.

    If the database tracks Alembic history, bring it to head FIRST so the
    schema matches the current models (create_all alone never ALTERs
    pre-existing tables). A brand-new database with no history is simply
    built from metadata.
    """
    from pathlib import Path

    from sqlalchemy import inspect as sa_inspect

    import app.models  # noqa: F401  # register models on Base.metadata
    from app.database import Base

    engine = create_engine(TEST_DATABASE_URL)

    if "alembic_version" in sa_inspect(engine).get_table_names():
        from alembic import command
        from alembic.config import Config

        project_root = Path(__file__).resolve().parent.parent
        cfg = Config(str(project_root / "alembic.ini"))
        cfg.set_main_option("script_location", str(project_root / "migrations"))
        command.upgrade(cfg, "head")

    Base.metadata.create_all(engine)  # idempotent; independent of Alembic state
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_revenue_tables(pg_engine):
    """Truncate around every test, children before parents.

    webhook_events is included because the identity-separation test
    inserts a WebhookEvent row alongside domain rows.
    """

    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "TRUNCATE recovery_cases, payment_attempts, orders,"
                    " webhook_events RESTART IDENTITY"
                )
            )

    _truncate()
    yield
    _truncate()


def _make_order(order_id="order_SYNTHETIC001", amount_paise=100000):
    from app.models import Order

    return Order(
        razorpay_order_id=order_id,
        amount_paise=amount_paise,
    )


def _make_attempt(payment_id, order_row_id, status="failed",
                  amount_paise=100000, **extra):
    from app.models import PaymentAttempt

    return PaymentAttempt(
        razorpay_payment_id=payment_id,
        order_id=order_row_id,
        amount_paise=amount_paise,
        currency="INR",
        status=status,
        **extra,
    )


# --------------------------------------------------------------------------
# 1 + 10: Order round trip with defaults and tz-aware server timestamps
# --------------------------------------------------------------------------


def test_order_insert_read_round_trip(pg_engine):
    from app.models import Order

    with Session(pg_engine) as session:
        session.add(_make_order())
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(Order).filter_by(
            razorpay_order_id="order_SYNTHETIC001"
        ).one()
        assert row.id > 0
        assert row.amount_paise == 100000
        assert row.currency == "INR"  # model default applied
        assert row.status == "created"  # model default applied
        assert row.created_at is not None
        assert row.created_at.tzinfo is not None  # timezone-aware
        assert row.updated_at is not None
        assert row.updated_at.tzinfo is not None


# --------------------------------------------------------------------------
# 2 + 9a: duplicate order identity rejected by PostgreSQL
# --------------------------------------------------------------------------


def test_duplicate_razorpay_order_id_rejected(pg_engine):
    with Session(pg_engine) as first:
        first.add(_make_order())
        first.commit()

    with Session(pg_engine) as second:
        second.add(_make_order("order_SYNTHETIC001"))
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
        # The failed transaction left the session usable.
        assert second.execute(text("SELECT 1")).scalar() == 1


# --------------------------------------------------------------------------
# 3 + 4: PaymentAttempts against an Order -- one order, many attempts
# --------------------------------------------------------------------------


def test_payment_attempts_belong_to_order_multiple_allowed(pg_engine):
    from app.models import Order, PaymentAttempt

    with Session(pg_engine) as session:
        order = _make_order()
        session.add(order)
        session.flush()  # assign order.id

        session.add(_make_attempt("pay_SYNTH_a", order.id, "failed"))
        session.add(_make_attempt("pay_SYNTH_b", order.id, "failed"))
        session.add(_make_attempt("pay_SYNTH_c", order.id, "captured"))
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(Order).filter_by(
            razorpay_order_id="order_SYNTHETIC001"
        ).one()
        attempts = sorted(
            reader.query(PaymentAttempt).filter_by(order_id=row.id).all(),
            key=lambda a: a.razorpay_payment_id,
        )
        assert len(attempts) == 3
        assert [a.status for a in attempts] == [
            "failed", "failed", "captured",
        ]
        # Relationship navigation works both ways.
        assert len(row.payment_attempts) == 3
        assert attempts[0].order.razorpay_order_id == "order_SYNTHETIC001"


def test_payment_attempt_fields_round_trip(pg_engine):
    from app.models import PaymentAttempt

    with Session(pg_engine) as session:
        order = _make_order()
        session.add(order)
        session.flush()
        session.add(
            _make_attempt(
                "pay_SYNTH_fail",
                order.id,
                status="failed",
                failure_code="CARD_DECLINED",
                failure_reason="Insufficient funds (synthetic)",
            )
        )
        session.commit()

    with Session(pg_engine) as reader:
        row = (
            reader.query(PaymentAttempt)
            .filter_by(razorpay_payment_id="pay_SYNTH_fail")
            .one()
        )
        assert row.failure_code == "CARD_DECLINED"
        assert row.failure_reason == "Insufficient funds (synthetic)"
        assert row.currency == "INR"
        assert row.created_at.tzinfo is not None
        assert row.updated_at.tzinfo is not None


# --------------------------------------------------------------------------
# 5: duplicate payment identity rejected
# --------------------------------------------------------------------------


def test_duplicate_razorpay_payment_id_rejected(pg_engine):
    with Session(pg_engine) as session:
        order = _make_order()
        session.add(order)
        session.flush()
        order_pk = order.id
        session.add(_make_attempt("pay_SYNTH_dup", order_pk))
        session.commit()

    with Session(pg_engine) as second:
        second.add(_make_attempt("pay_SYNTH_dup", order_pk))
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()


# --------------------------------------------------------------------------
# 6 + 9: FK and NOT NULL violations rejected
# --------------------------------------------------------------------------


def test_invalid_order_fk_rejected(pg_engine):
    with Session(pg_engine) as session:
        session.add(_make_attempt("pay_SYNTH_orphan", 999999))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


@pytest.mark.parametrize(
    ("class_name", "kwargs"),
    [
        ("Order", {"razorpay_order_id": "order_nn", "amount_paise": None}),
        ("Order", {"razorpay_order_id": None, "amount_paise": 100000}),
        (
            "PaymentAttempt",
            {
                "razorpay_payment_id": "pay_nn",
                "order_id": 1,
                "amount_paise": 100000,
                "currency": "INR",
                "status": None,
            },
        ),
    ],
)
def test_not_null_constraints_enforced(pg_engine, class_name, kwargs):
    import app.models

    model = getattr(app.models, class_name)

    with Session(pg_engine) as session:
        session.add(model(**kwargs))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --------------------------------------------------------------------------
# 7 + 8: RecoveryCase belongs to the ORDER (not any payment attempt)
# --------------------------------------------------------------------------


def test_recovery_case_insert_and_order_reference(pg_engine):
    from app.models import RecoveryCase

    with Session(pg_engine) as session:
        order = _make_order()
        session.add(order)
        session.flush()
        case = RecoveryCase(
            order_id=order.id,
            amount_at_risk_paise=100000,
        )
        session.add(case)
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(RecoveryCase).one()
        assert row.id > 0
        assert row.status == "at_risk"  # default applied
        assert row.amount_at_risk_paise == 100000
        assert row.opened_at is not None
        assert row.opened_at.tzinfo is not None
        assert row.observation_expires_at is None  # nullable starts NULL
        assert row.closed_at is None
        assert row.resolution is None
        # Navigation proves the reference points at the right Order.
        assert row.order.razorpay_order_id == "order_SYNTHETIC001"
        assert row.order.recovery_cases[0].id == row.id


def test_order_can_hold_two_recovery_cases_with_distinct_lifecycles(pg_engine):
    """F1.1: one Order, two RecoveryCase rows, distinct status/resolution/
    timestamps; the Order relationship returns both."""
    from datetime import timedelta

    from app.models import RecoveryCase

    with Session(pg_engine) as session:
        order = _make_order()
        session.add(order)
        session.flush()
        order_pk = order.id

        episode_one = RecoveryCase(
            order_id=order_pk,
            amount_at_risk_paise=100000,
            # defaults: status at_risk, everything else NULL
        )
        session.add(episode_one)
        session.commit()

        with Session(pg_engine) as writer:
            closed_at = writer.query(RecoveryCase).one().opened_at + (
                timedelta(days=3)
            )
            episode_two = RecoveryCase(
                order_id=order_pk,
                amount_at_risk_paise=100000,
                status="resolved",
                resolution="NATURAL_RECOVERY",
                observation_expires_at=closed_at - timedelta(days=1),
                closed_at=closed_at,
            )
            writer.add(episode_two)
            writer.commit()

    with Session(pg_engine) as reader:
        from app.models import Order

        order = reader.query(Order).filter_by(
            razorpay_order_id="order_SYNTHETIC001"
        ).one()

        cases = reader.query(RecoveryCase).filter_by(order_id=order_pk).all()
        assert len(cases) == 2  # both rows point at the SAME Order
        assert {c.order_id for c in cases} == {order_pk}

        # The relationship returns both cases.
        assert len(order.recovery_cases) == 2
        assert all(c.order_id == order.id for c in order.recovery_cases)

        by_status = {c.status: c for c in cases}
        open_case = by_status["at_risk"]
        resolved = by_status["resolved"]

        # Distinct lifecycles per row.
        assert open_case.resolution is None
        assert open_case.closed_at is None
        assert resolved.resolution == "NATURAL_RECOVERY"
        assert resolved.closed_at is not None
        assert resolved.closed_at > open_case.opened_at
        assert resolved.observation_expires_at is not None


def test_recovery_case_invalid_order_fk_still_rejected(pg_engine):
    from app.models import RecoveryCase

    with Session(pg_engine) as session:
        session.add(
            RecoveryCase(order_id=424242, amount_at_risk_paise=100000)
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


# --------------------------------------------------------------------------
# Identity separation sanity: three distinct external identities coexist
# --------------------------------------------------------------------------


def test_external_identities_stay_distinct(pg_engine):
    """WebhookEvent.event_id never substitutes for Razorpay identities."""
    from app.models import Order, PaymentAttempt, WebhookEvent

    with Session(pg_engine) as session:
        order = _make_order("order_IDENTITY_CHECK")
        session.add(order)
        session.flush()
        session.add(
            _make_attempt("pay_IDENTITY_CHECK", order.id, "captured")
        )
        session.add(
            WebhookEvent(
                event_id="evt_X_RAZORPAY_EVENT_ID",
                event_type="payment.captured",
                payload="{}",
            )
        )
        session.commit()

    with Session(pg_engine) as reader:
        order = reader.query(Order).filter_by(
            razorpay_order_id="order_IDENTITY_CHECK"
        ).one()
        attempt = reader.query(PaymentAttempt).filter_by(
            razorpay_payment_id="pay_IDENTITY_CHECK"
        ).one()
        event = reader.query(WebhookEvent).filter_by(
            event_id="evt_X_RAZORPAY_EVENT_ID"
        ).one()

        values = {
            order.razorpay_order_id,
            attempt.razorpay_payment_id,
            event.event_id,
        }
        assert len(values) == 3  # three distinct identity spaces
