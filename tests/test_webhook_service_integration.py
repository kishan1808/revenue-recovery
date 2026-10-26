"""Real-PostgreSQL tests for the webhook persistence service (Slice C).

Proves the service-level idempotency contract against a REAL database:
the UNIQUE constraint on webhook_events.event_id stays authoritative
and persist_event classifies only that violation as DUPLICATE.

Runs against a REAL PostgreSQL database and is strictly opt-in:
skipped unless TEST_DATABASE_URL is set (see tests/conftest.py for the
isolation policy). Uses the existing isolation mechanism -- no new
database configuration. Synthetic values only.
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


def test_duplicate_event_id_first_new_then_duplicate_one_row(pg_engine):
    """Two inserts, same event_id: NEW then DUPLICATE; exactly one row."""
    from app.models import WebhookEvent
    from app.webhooks.service import PersistOutcome, persist_event

    payload = b'{"event":"payment.failed","id":"evt_svc_dup_001"}'

    with Session(pg_engine) as first:
        assert (
            persist_event(
                first,
                event_id="evt_svc_dup_001",
                event_type="payment.failed",
                raw_payload=payload,
            )
            is PersistOutcome.NEW
        )

    with Session(pg_engine) as second:
        assert (
            persist_event(
                second,
                event_id="evt_svc_dup_001",
                event_type="payment.failed",
                raw_payload=payload,
            )
            is PersistOutcome.DUPLICATE
        )
        # Rollback inside the service left this session usable.
        assert second.execute(text("SELECT 1")).scalar() == 1
        assert (
            second.query(WebhookEvent)
            .filter_by(event_id="evt_svc_dup_001")
            .count()
            == 1
        )


def test_new_event_persists_exact_verified_data(pg_engine):
    """NEW path on real PostgreSQL stores identity + exact body bytes."""
    from app.models import WebhookEvent
    from app.webhooks.service import PersistOutcome, persist_event

    raw = (
        b'{\n  "event": "payment.captured",\n  "note": "caf\xc3\xa9",'
        b'\n  "nested": {"amount": 150000}\n}  '
    )
    with Session(pg_engine) as session:
        assert (
            persist_event(
                session,
                event_id="evt_svc_new_001",
                event_type="payment.captured",
                raw_payload=raw,
            )
            is PersistOutcome.NEW
        )

    with Session(pg_engine) as reader:
        row = reader.query(WebhookEvent).filter_by(
            event_id="evt_svc_new_001"
        ).one()
        assert row.event_type == "payment.captured"
        assert row.status == "received"
        assert row.payload == raw.decode("utf-8")  # byte-for-byte fidelity


def test_unrelated_integrity_error_propagates_on_postgres(pg_engine):
    """A NOT NULL violation propagates; nothing persisted; session usable."""
    from app.webhooks.service import persist_event

    with Session(pg_engine) as session:
        with pytest.raises(IntegrityError):
            persist_event(
                session,
                event_id=None,  # NOT NULL violation -- unrelated to uniqueness
                event_type="payment.failed",
                raw_payload=b"{}",
            )
        # The service rolled back before re-raising: usable session, no row.
        assert session.execute(text("SELECT 1")).scalar() == 1
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
        assert count == 0
