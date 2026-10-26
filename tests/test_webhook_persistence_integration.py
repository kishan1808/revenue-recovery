"""WebhookEvent PostgreSQL persistence integration tests (Phase 2, Slice B).

Runs against a REAL PostgreSQL database and is strictly opt-in: skipped
unless TEST_DATABASE_URL is set (see tests/conftest.py for the isolation
policy). Destructive Alembic cycle coverage lives in
test_migrations_integration.py, which shares the same guard.

Synthetic values only.
"""

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
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


def _make_event(event_id: str = "evt_synthetic_001",
                event_type: str = "payment.failed"):
    from app.models import WebhookEvent

    return WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=(
            '{"event":"payment.failed","payload":{"payment":'
            '{"entity":{"id":"pay_synthetic_001","amount":150000}}}}'
        ),
    )


def test_webhook_event_insert_read_round_trip(pg_engine):
    """1. WebhookEvent survives a commit and reads back identically."""
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        session.add(_make_event())
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_synthetic_001"
        ).one()
        assert row.id > 0
        assert row.event_id == "evt_synthetic_001"
        assert row.event_type == "payment.failed"


def test_payload_preserved_exactly(pg_engine):
    """5. payload round-trips byte-for-byte, including unicode/newlines/quotes."""
    from app.models import WebhookEvent

    tricky_payload = (
        '{\n  "note": "line one\nline two\t(tab)",'
        ' "unicode": "\\u00e9\\u2705", "quote": "say \\"hi\\"",'
        ' "path": "back\\\\slash"\n}'
    )
    with Session(pg_engine) as session:
        session.add(
            WebhookEvent(
                event_id="evt_payload_fidelity",
                event_type="payment.captured",
                payload=tricky_payload,
            )
        )
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_payload_fidelity"
        ).one()
        assert row.payload == tricky_payload


def test_duplicate_event_id_rejected_by_postgres(pg_engine):
    """2 + 3. The UNIQUE constraint fires IntegrityError on a duplicate."""
    with Session(pg_engine) as first:
        first.add(_make_event("evt_dup_check"))
        first.commit()

    with Session(pg_engine) as second:
        second.add(_make_event("evt_dup_check"))
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()


def test_session_usable_after_duplicate_rollback(pg_engine):
    """4. Rollback after the violation leaves one row and a healthy session."""
    from app.models import WebhookEvent

    with Session(pg_engine) as first:
        first.add(_make_event("evt_dup_recovery"))
        first.commit()

    with Session(pg_engine) as second:
        second.add(_make_event("evt_dup_recovery"))
        with pytest.raises(IntegrityError):
            second.commit()
        second.rollback()
        assert second.query(WebhookEvent).filter_by(
            event_id="evt_dup_recovery"
        ).count() == 1
        assert second.execute(text("SELECT 1")).scalar() == 1


def test_received_at_is_timezone_aware_server_default(pg_engine):
    """6. received_at is populated by the server default and tz-aware."""
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        event = _make_event("evt_tz_check")
        session.add(event)
        assert event.received_at is None  # not set client-side
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_tz_check"
        ).one()
        assert row.received_at is not None
        assert row.received_at.tzinfo is not None


def test_default_status_is_received(pg_engine):
    """7. status defaults to 'received' when not provided."""
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        session.add(_make_event("evt_status_default"))
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_status_default"
        ).one()
        assert row.status == "received"


def test_processed_at_and_error_null_initially(pg_engine):
    """8 + 9. processed_at and error are nullable and start as NULL."""
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        session.add(_make_event("evt_initially_null"))
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_initially_null"
        ).one()
        assert row.processed_at is None
        assert row.error is None


def test_no_rows_left_behind(pg_engine):
    """Cleanup fixture guarantees an empty table after tests."""
    from app.models import WebhookEvent

    with Session(pg_engine) as reader:
        assert reader.query(WebhookEvent).count() == 0
