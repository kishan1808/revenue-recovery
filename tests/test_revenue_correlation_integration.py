"""Real-PostgreSQL integration tests for webhook -> revenue correlation
(Phase 2, Slice F2).

Covers the full deterministic lifecycle (A-P) INCLUDING genuine
concurrency races executed on separate connections/threads against the
real Neon test database:

I. concurrent Order creation (same razorpay_order_id)
J. concurrent PaymentAttempt creation (same razorpay_payment_id)
K. concurrent RecoveryCase activation (two failures, one order)

Strictly opt-in via TEST_DATABASE_URL; skipped otherwise. No DATABASE_URL
fallback. Synthetic data only.
"""

import json
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set - F2 correlation tests skipped",
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def pg_engine():
    """Engine bound to the explicit test database, schema ensured."""
    from sqlalchemy import inspect as sa_inspect

    import app.models  # noqa: F401
    from app.database import Base

    engine = create_engine(TEST_DATABASE_URL)
    if "alembic_version" in sa_inspect(engine).get_table_names():
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
        cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
        command.upgrade(cfg, "head")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(pg_engine):
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


# ---------------------------------------------------------------------------
# Payload builders (realistic Razorpay shapes, synthetic values)
# ---------------------------------------------------------------------------


def _payment_entity(payment_id, order_id, *, amount=50000, currency="INR",
                    status="failed", include=True, **extra):
    if not include:
        return None
    entity = {
        "id": payment_id,
        "order_id": order_id,
        "amount": amount,
        "currency": currency,
        "status": status,
    }
    entity.update(extra)
    return {"entity": entity}


def _order_entity(order_id, *, amount=50000, currency="INR", include=True):
    if not include:
        return None
    return {
        "entity": {
            "id": order_id,
            "amount": amount,
            "currency": currency,
            "status": "created",
        }
    }


def _body(event_type, payment, order):
    payload = {}
    if payment is not None:
        payload["payment"] = payment
    if order is not None:
        payload["order"] = order
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": event_type,
        "contains": ["payment"],
        "payload": payload,
        "created_at": 1691735748,
    }


def _seed(pg_engine, event_id, event_type, body):
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        event = WebhookEvent(
            event_id=event_id,
            event_type=event_type,
            payload=json.dumps(body),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
        return event


def _apply(pg_engine, event):
    """Apply inside a fresh transaction and commit (replay-tool style)."""
    from app.domain.revenue import apply_webhook_to_domain

    with Session(pg_engine) as session:
        attached = session.merge(event)
        outcome = apply_webhook_to_domain(session, attached)
        session.commit()
        return outcome


def _counts(pg_engine):
    import sqlalchemy as sa

    with pg_engine.connect() as conn:
        return tuple(
            conn.execute(
                sa.select(sa.func.count()).select_from(sa.table(t))
            ).scalar()
            for t in ("orders", "payment_attempts", "recovery_cases",
                      "webhook_events")
        )


def _one(pg_engine, model, **criteria):
    with Session(pg_engine) as session:
        return session.query(model).filter_by(**criteria).one()


def _all(pg_engine, model, **criteria):
    with Session(pg_engine) as session:
        return session.query(model).filter_by(**criteria).all()


# ---------------------------------------------------------------------------
# A. payment.failed -> payment.captured  (THE natural-recovery path)
# ---------------------------------------------------------------------------


def test_failed_then_captured_natural_recovery(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, RecoveryCase

    failed = _seed(
        pg_engine, "evt_A_fail", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_A", "order_A",
                              error_code="CARD_DECLINED",
                              error_description="synthetic decline"),
              _order_entity("order_A")),
    )
    captured = _seed(
        pg_engine, "evt_A_cap", "payment.captured",
        _body("payment.captured",
              _payment_entity("pay_A", "order_A", status="captured"),
              _order_entity("order_A")),
    )

    assert _apply(pg_engine, failed) is CorrelationOutcome.STATE_UPDATED
    assert (
        _apply(pg_engine, captured) is CorrelationOutcome.NATURAL_RECOVERY
    )

    order = _one(pg_engine, Order, razorpay_order_id="order_A")
    assert order.status == "paid"
    from app.models import PaymentAttempt

    attempt = _one(pg_engine, PaymentAttempt, razorpay_payment_id="pay_A")
    assert attempt.status == "CAPTURED"
    case = _one(pg_engine, RecoveryCase, order_id=order.id)
    assert case.status == "CLOSED"
    assert case.resolution == "NATURAL_RECOVERY"
    assert case.closed_at is not None
    # Failure fields were recorded from the failed snapshot only.
    assert attempt.failure_code == "CARD_DECLINED"


# ---------------------------------------------------------------------------
# B/C/D/E: convergence + snapshot safety
# ---------------------------------------------------------------------------


def _seed_pair(pg_engine, prefix, order_id="order_conv"):
    cap = _seed(
        pg_engine, f"{prefix}_cap", "payment.captured",
        _body("payment.captured",
              _payment_entity(f"{prefix}_pay", order_id, status="captured"),
              _order_entity(order_id)),
    )
    paid = _seed(
        pg_engine, f"{prefix}_paid", "order.paid",
        _body("order.paid",
              _payment_entity(f"{prefix}_pay", order_id, status="captured"),
              _order_entity(order_id)),
    )
    return cap, paid


def test_b_captured_then_order_paid_is_noop(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import PaymentAttempt

    cap, paid = _seed_pair(pg_engine, "convB")
    assert _apply(pg_engine, cap) is CorrelationOutcome.STATE_UPDATED
    assert _apply(pg_engine, paid) is CorrelationOutcome.STATE_NOOP
    assert (
        _one(pg_engine, PaymentAttempt,
             razorpay_payment_id="convB_pay").status
        == "CAPTURED"
    )


def test_c_order_paid_then_captured_is_noop(pg_engine):
    from app.domain.revenue import CorrelationOutcome

    cap, paid = _seed_pair(pg_engine, "convC")
    assert _apply(pg_engine, paid) is CorrelationOutcome.STATE_UPDATED
    assert _apply(pg_engine, cap) is CorrelationOutcome.STATE_NOOP


def test_d_captured_then_failed_never_downgrades(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    cap = _seed(
        pg_engine, "evt_D_cap", "payment.captured",
        _body("payment.captured",
              _payment_entity("pay_D", "order_D", status="captured"),
              _order_entity("order_D")),
    )
    failed = _seed(
        pg_engine, "evt_D_fail", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_D", "order_D", status="failed"),
              _order_entity("order_D")),
    )

    assert _apply(pg_engine, cap) is CorrelationOutcome.STATE_UPDATED
    assert _apply(pg_engine, failed) is CorrelationOutcome.STATE_NOOP

    assert (
        _one(pg_engine, PaymentAttempt,
             razorpay_payment_id="pay_D").status
        == "CAPTURED"
    )
    assert _one(pg_engine, Order, razorpay_order_id="order_D").status == "paid"
    assert _counts(pg_engine)[2] == 0  # failure after capture opens NO case


def test_e_authorized_then_captured(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import PaymentAttempt

    authorized = _seed(
        pg_engine, "evt_E_auth", "payment.authorized",
        _body("payment.authorized",
              _payment_entity("pay_E", "order_E", status="authorized"),
              _order_entity("order_E")),
    )
    captured = _seed(
        pg_engine, "evt_E_cap", "payment.captured",
        _body("payment.captured",
              _payment_entity("pay_E", "order_E", status="captured"),
              _order_entity("order_E")),
    )

    assert _apply(pg_engine, authorized) is CorrelationOutcome.STATE_UPDATED
    assert _apply(pg_engine, captured) is CorrelationOutcome.STATE_UPDATED

    attempt = _one(pg_engine, PaymentAttempt, razorpay_payment_id="pay_E")
    assert attempt.status == "CAPTURED"


# ---------------------------------------------------------------------------
# F/G/H: multi-attempt orders + recovery episodes
# ---------------------------------------------------------------------------


def test_f_two_payment_attempts_one_order(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order, PaymentAttempt

    for index, payment_id in enumerate(("pay_F1", "pay_F2")):
        event = _seed(
            pg_engine, f"evt_F{index}", "payment.failed",
            _body("payment.failed",
                  _payment_entity(payment_id, "order_F"),
                  _order_entity("order_F")),
        )
        assert _apply(pg_engine, event) is CorrelationOutcome.STATE_UPDATED

    assert len(_all(pg_engine, Order)) == 1
    assert len(_all(pg_engine, PaymentAttempt)) == 2


def test_g_two_historical_recovery_cases_same_order(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import RecoveryCase

    sequence = [
        ("evt_G_f1", "payment.failed", "pay_G1", "failed"),
        ("evt_G_c1", "payment.captured", "pay_G1", "captured"),
        ("evt_G_f2", "payment.failed", "pay_G2", "failed"),
    ]
    outcomes = []
    for _index, (eid, etype, pid, status) in enumerate(sequence):
        event = _seed(
            pg_engine, eid, etype,
            _body(etype,
                  _payment_entity(pid, "order_G", status=status),
                  _order_entity("order_G")),
        )
        outcomes.append(_apply(pg_engine, event))

    assert outcomes[1] is CorrelationOutcome.NATURAL_RECOVERY
    cases = sorted(_all(pg_engine, RecoveryCase), key=lambda c: c.id)
    assert [c.status for c in cases] == ["CLOSED", "OBSERVING"]


def test_h_maximum_one_active_recovery_case(pg_engine):
    from app.models import RecoveryCase

    for index, payment_id in enumerate(("pay_H1", "pay_H2")):
        event = _seed(
            pg_engine, f"evt_H{index}", "payment.failed",
            _body("payment.failed",
                  _payment_entity(payment_id, "order_H"),
                  _order_entity("order_H")),
        )
        _apply(pg_engine, event)

    active = [
        c for c in _all(pg_engine, RecoveryCase)
        if c.status == "OBSERVING"
    ]
    assert len(active) == 1
    assert len(_all(pg_engine, RecoveryCase)) == 1


# ---------------------------------------------------------------------------
# L/M/N: mismatch visibility + no fabrication
# ---------------------------------------------------------------------------


def test_l_amount_mismatch_visible_without_overwrite(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order

    first = _seed(
        pg_engine, "evt_L1", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_L", "order_L"),
              _order_entity("order_L")),
    )
    assert _apply(pg_engine, first) is CorrelationOutcome.STATE_UPDATED

    conflicting = _seed(
        pg_engine, "evt_L2", "payment.authorized",
        _body("payment.authorized",
              _payment_entity("pay_L", "order_L", status="authorized"),
              _order_entity("order_L", amount=99000)),
    )
    assert (
        _apply(pg_engine, conflicting) is CorrelationOutcome.DATA_MISMATCH
    )
    assert _one(pg_engine, Order, razorpay_order_id="order_L").amount_paise \
        == 50000


def test_m_currency_mismatch_visible_without_overwrite(pg_engine):
    from app.domain.revenue import CorrelationOutcome
    from app.models import Order

    first = _seed(
        pg_engine, "evt_M1", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_M", "order_M"),
              _order_entity("order_M")),
    )
    _apply(pg_engine, first)

    conflicting = _seed(
        pg_engine, "evt_M2", "payment.authorized",
        _body("payment.authorized",
              _payment_entity("pay_M", "order_M", currency="USD",
                              status="authorized"),
              _order_entity("order_M", currency="USD")),
    )
    assert (
        _apply(pg_engine, conflicting) is CorrelationOutcome.DATA_MISMATCH
    )
    assert _one(pg_engine, Order, razorpay_order_id="order_M").currency == \
        "INR"


def test_n_missing_order_entity_creates_nothing(pg_engine):
    from app.domain.revenue import CorrelationOutcome

    event = _seed(
        pg_engine, "evt_N", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_N", "order_unknown"),
              _order_entity("order_unknown", include=False)),
    )
    assert (
        _apply(pg_engine, event) is CorrelationOutcome.CORRELATION_INCOMPLETE
    )
    orders, attempts, _, _ = _counts(pg_engine)
    assert (orders, attempts) == (0, 0)


# ---------------------------------------------------------------------------
# O: atomicity -- crash mid-correlation leaves zero partial rows
# ---------------------------------------------------------------------------


def test_o_domain_crash_leaves_zero_partial_rows(pg_engine, monkeypatch):
    from app.domain.revenue import apply_webhook_to_domain

    event = _seed(
        pg_engine, "evt_O", "payment.failed",
        _body("payment.failed",
              _payment_entity("pay_O", "order_O"),
              _order_entity("order_O")),
    )

    with Session(pg_engine) as session:
        attached = session.merge(event)
        original_add = session.add

        def crashing_after_attempt(obj):
            from app.models import RecoveryCase

            if isinstance(obj, RecoveryCase):
                raise RuntimeError("synthetic crash during case creation")
            return original_add(obj)

        monkeypatch.setattr(session, "add", crashing_after_attempt)
        with pytest.raises(RuntimeError):
            apply_webhook_to_domain(session, attached)
        monkeypatch.undo()
        session.rollback()

    assert _counts(pg_engine) == (0, 0, 0, 1)  # only the WebhookEvent remains


# ---------------------------------------------------------------------------
# P: migrations remain linear with exactly one head
# ---------------------------------------------------------------------------


def test_p_single_linear_migration_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    script = ScriptDirectory.from_config(cfg)
    heads = script.get_heads()
    assert len(heads) == 1


# ---------------------------------------------------------------------------
# I/J/K: TRUE concurrency races on separate connections
# ---------------------------------------------------------------------------


def _run_race(pg_engine, jobs, timeout=45):
    """Run jobs concurrently; each gets its own Session; commit at end."""
    barrier = threading.Barrier(len(jobs))
    results = [None] * len(jobs)

    def worker(index, job):
        session = Session(pg_engine)
        try:
            barrier.wait(timeout=timeout)
            results[index] = job(session)
            session.commit()
        except BaseException as exc:  # noqa: BLE001 - recorded for asserts
            session.rollback()
            results[index] = exc
        finally:
            session.close()

    threads = [
        threading.Thread(target=worker, args=(i, job))
        for i, job in enumerate(jobs)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout)
        assert not thread.is_alive(), "race test deadlocked"

    errors = [r for r in results if isinstance(r, BaseException)]
    assert errors == [], f"unexpected worker errors: {errors!r}"
    return results


def _make_job(event):
    from app.domain.revenue import apply_webhook_to_domain

    def job(session):
        attached = session.merge(event)
        return apply_webhook_to_domain(session, attached)

    return job


def test_i_concurrent_order_creation_exactly_one_order(pg_engine):
    from app.models import Order

    events = [
        _seed(
            pg_engine, f"evt_I{i}", "payment.failed",
            _body("payment.failed",
                  _payment_entity(f"pay_I{i}", "order_I_race"),
                  _order_entity("order_I_race")),
        )
        for i in range(2)
    ]

    _run_race(pg_engine, [_make_job(e) for e in events])

    from app.models import PaymentAttempt

    assert len(_all(pg_engine, Order, razorpay_order_id="order_I_race")) == 1
    assert len(_all(pg_engine, PaymentAttempt)) == 2


def test_j_concurrent_payment_attempt_creation_exactly_one(pg_engine):
    from app.models import Order, PaymentAttempt

    with Session(pg_engine) as session:
        session.add(
            Order(
                razorpay_order_id="order_J",
                amount_paise=50000,
                currency="INR",
            )
        )
        session.commit()

    events = [
        _seed(
            pg_engine, f"evt_J{i}", "payment.captured",
            _body("payment.captured",
                  _payment_entity("pay_J_shared", "order_J",
                                  status="captured"),
                  _order_entity("order_J")),
        )
        for i in range(2)
    ]

    _run_race(pg_engine, [_make_job(e) for e in events])

    attempts = _all(pg_engine, PaymentAttempt,
                    razorpay_payment_id="pay_J_shared")
    assert len(attempts) == 1
    assert attempts[0].status == "CAPTURED"


def test_k_concurrent_failures_produce_single_active_case(pg_engine):
    from app.models import Order, RecoveryCase

    with Session(pg_engine) as session:
        session.add(
            Order(
                razorpay_order_id="order_K",
                amount_paise=50000,
                currency="INR",
            )
        )
        session.commit()

    events = [
        _seed(
            pg_engine, f"evt_K{i}", "payment.failed",
            _body("payment.failed",
                  _payment_entity(f"pay_K{i}", "order_K"),
                  _order_entity("order_K", include=False)),
        )
        for i in range(2)
    ]

    _run_race(pg_engine, [_make_job(e) for e in events])

    active = [
        c for c in _all(pg_engine, RecoveryCase, order_id=_one(
            pg_engine, Order, razorpay_order_id="order_K").id)
        if c.status == "OBSERVING"
    ]
    assert len(active) == 1
    assert len(_all(pg_engine, RecoveryCase)) == 1
