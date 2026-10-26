"""Real-PostgreSQL tests for the webhook event processor (Slice D).

Proves the received -> processed / ignored / failed lifecycle, terminal
reprocessing refusal, rollback/session health, and processed_at
persistence against a REAL database.

Runs against a REAL PostgreSQL database and is strictly opt-in: skipped
unless TEST_DATABASE_URL is set (see tests/conftest.py for the isolation
policy). Uses the existing isolation mechanism -- no new database
configuration, no DATABASE_URL fallback. Synthetic values only.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set - PostgreSQL integration tests skipped",
)


@pytest.fixture(scope="module")
def pg_engine():
    """Engine bound to the explicit test database, schema ensured."""
    import app.models  # noqa: F401  # register models on Base.metadata
    from app.database import Base

    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(engine)  # idempotent; independent of Alembic state
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_webhook_events(pg_engine):
    """Truncate around every test: full isolation + zero leftover rows."""

    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE webhook_events RESTART IDENTITY"))

    _truncate()
    yield
    _truncate()


def _insert_received(
    pg_engine,
    *,
    event_id,
    event_type,
    payload='{"event": "synthetic", "ok": true}',
):
    """Insert one status='received' WebhookEvent in its own transaction."""
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        session.add(
            WebhookEvent(
                event_id=event_id, event_type=event_type, payload=payload
            )
        )
        session.commit()


# --------------------------------------------------------------------------
# 1. received -> processed on real PostgreSQL
# --------------------------------------------------------------------------


def test_received_becomes_processed_on_postgres(pg_engine):
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        pg_engine,
        event_id="evt_pg_processed",
        event_type="order.paid",
        payload=(
            '{"event":"order.paid","payload":{"order":{"entity":'
            '{"id":"order_pg_processed","amount":150000,"currency":"INR"}},'
            '"payment":{"entity":{"id":"pay_pg_processed",'
            '"order_id":"order_pg_processed","amount":150000,'
            '"currency":"INR","status":"captured"}}}}'
        ),
    )

    with Session(pg_engine) as session:
        assert (
            process_event(session, "evt_pg_processed")
            is ProcessOutcome.PROCESSED
        )

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_pg_processed"
        ).one()
        assert row.status == "processed"
        assert row.error is None
        assert row.processed_at is not None
        assert row.processed_at.tzinfo is not None  # timestamptz persisted


# --------------------------------------------------------------------------
# 2. received -> ignored on real PostgreSQL
# --------------------------------------------------------------------------


def test_unknown_type_becomes_ignored_on_postgres(pg_engine):
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        pg_engine,
        event_id="evt_pg_ignored",
        event_type="refund.processed",
    )

    with Session(pg_engine) as session:
        assert (
            process_event(session, "evt_pg_ignored") is ProcessOutcome.IGNORED
        )

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_pg_ignored"
        ).one()
        assert row.status == "ignored"
        assert row.processed_at is not None
        assert row.error is None


# --------------------------------------------------------------------------
# 3. received -> failed on real PostgreSQL (handler raises)
# --------------------------------------------------------------------------


def test_handler_failure_becomes_failed_on_postgres(pg_engine):
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    broken_payload = '{"event":"payment.failed","broken": true'
    _insert_received(
        pg_engine,
        event_id="evt_pg_failed",
        event_type="payment.failed",
        payload=broken_payload,
    )

    with Session(pg_engine) as session:
        outcome = process_event(session, "evt_pg_failed")

    assert outcome is ProcessOutcome.FAILED

    # The durable event survived with its failure recorded.
    with Session(pg_engine) as reader:
        rows = reader.query(WebhookEvent).filter_by(
            event_id="evt_pg_failed"
        ).all()
        assert len(rows) == 1  # no replacement row was created
        row = rows[0]
        assert row.status == "failed"
        assert row.payload == broken_payload  # original body intact
        assert row.received_at is not None
        assert row.processed_at is not None
        error_text = row.error
        assert error_text is not None
        assert "\n" not in error_text
        assert len(error_text) <= 500


# --------------------------------------------------------------------------
# 4. Terminal events are never reprocessed on real PostgreSQL
# --------------------------------------------------------------------------


def test_terminal_event_is_not_reprocessed_on_postgres(pg_engine):
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        pg_engine, event_id="evt_pg_terminal", event_type="order.paid",
        payload=(
            '{"event":"order.paid","payload":{"order":{"entity":'
            '{"id":"order_pg_terminal","amount":150000,"currency":"INR"}},'
            '"payment":{"entity":{"id":"pay_pg_terminal",'
            '"order_id":"order_pg_terminal","amount":150000,'
            '"currency":"INR","status":"captured"}}}}'
        ),
    )
    with Session(pg_engine) as session:
        assert (
            process_event(session, "evt_pg_terminal")
            is ProcessOutcome.PROCESSED
        )

    with Session(pg_engine) as before:
        frozen = (
            before.query(WebhookEvent)
            .filter_by(event_id="evt_pg_terminal")
            .one()
        )
        frozen_status = frozen.status
        frozen_processed_at = frozen.processed_at

    with Session(pg_engine) as again:
        assert (
            process_event(again, "evt_pg_terminal")
            is ProcessOutcome.ALREADY_TERMINAL
        )

    with Session(pg_engine) as after:
        row = (
            after.query(WebhookEvent)
            .filter_by(event_id="evt_pg_terminal")
            .one()
        )
        assert row.status == frozen_status == "processed"
        assert row.processed_at == frozen_processed_at


# --------------------------------------------------------------------------
# 5. Rollback / session behaviour stays correct after a failed handler
# --------------------------------------------------------------------------


def test_session_usable_and_consistent_after_failure(pg_engine):
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        pg_engine,
        event_id="evt_pg_recovery",
        event_type="payment.captured",
        payload="not-json{{{",
    )

    with Session(pg_engine) as session:
        assert (
            process_event(session, "evt_pg_recovery") is ProcessOutcome.FAILED
        )
        # The service left THIS session usable after its internal rollback.
        assert session.execute(text("SELECT 1")).scalar() == 1
        assert (
            session.query(WebhookEvent)
            .filter_by(event_id="evt_pg_recovery")
            .count()
            == 1
        )
        assert session.query(WebhookEvent).count() == 1


# --------------------------------------------------------------------------
# 6. processed_at persists identically across independent sessions
# --------------------------------------------------------------------------


def test_processed_at_persists_identically_across_sessions(pg_engine):
    from datetime import UTC, datetime

    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        pg_engine, event_id="evt_pg_timestamp", event_type="order.paid",
        payload=(
            '{"event":"order.paid","payload":{"order":{"entity":'
            '{"id":"order_pg_timestamp","amount":150000,"currency":"INR"}},'
            '"payment":{"entity":{"id":"pay_pg_timestamp",'
            '"order_id":"order_pg_timestamp","amount":150000,'
            '"currency":"INR","status":"captured"}}}}'
        ),
    )

    lower_bound = datetime.now(UTC)

    with Session(pg_engine) as session:
        assert (
            process_event(session, "evt_pg_timestamp")
            is ProcessOutcome.PROCESSED
        )

    upper_bound = datetime.now(UTC)

    first = Session(pg_engine)
    second = Session(pg_engine)
    try:
        row_a = (
            first.query(WebhookEvent)
            .filter_by(event_id="evt_pg_timestamp")
            .one()
        )
        row_b = (
            second.query(WebhookEvent)
            .filter_by(event_id="evt_pg_timestamp")
            .one()
        )
        assert row_a.processed_at == row_b.processed_at
        stored = row_a.processed_at
        assert stored.tzinfo is not None
        assert lower_bound <= stored <= upper_bound
    finally:
        first.close()
        second.close()
