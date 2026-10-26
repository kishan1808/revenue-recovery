"""Unit tests for the webhook persistence service (Phase 2, Slice C).

Pure service-layer tests: no HTTP, no Razorpay. Runs against in-memory
SQLite so no external database is required; real PostgreSQL behaviour
is proven separately in tests/test_webhook_service_integration.py.

app imports are deliberately lazy (inside fixtures/tests) so this
module never touches app.database at collection time; a sqlite://
DATABASE_URL fallback keeps bare environments importable.
"""

import os
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool


def _ensure_importable_database_url() -> None:
    """Keep app.database importable when no DATABASE_URL is configured.

    Unit tests build their own in-memory engines and never use the
    global engine; the fallback is only set when the variable is absent
    so real configuration always wins.
    """
    os.environ.setdefault("DATABASE_URL", "sqlite://")


@pytest.fixture()
def session():
    """A Session on a fresh in-memory SQLite database."""
    _ensure_importable_database_url()
    import app.models  # noqa: F401  # register models on Base.metadata
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


def _persist(session, *, event_id="evt_synthetic_001",
             event_type="payment.failed", raw_payload=b'{"ok":true}'):
    from app.webhooks.service import persist_event

    return persist_event(
        session,
        event_id=event_id,
        event_type=event_type,
        raw_payload=raw_payload,
    )


def _count_webhook_events(session):
    import app.models

    return session.query(app.models.WebhookEvent).count()


# --------------------------------------------------------------------------
# New event path
# --------------------------------------------------------------------------


def test_new_event_returns_new_and_persists_row(session):
    """NEW result; exactly one row with the submitted identity."""
    from app.models import WebhookEvent
    from app.webhooks.service import PersistOutcome

    result = _persist(
        session,
        event_id="evt_new_001",
        event_type="payment.captured",
        raw_payload=b'{"event":"payment.captured"}',
    )

    assert result is PersistOutcome.NEW
    assert _count_webhook_events(session) == 1
    row = session.query(WebhookEvent).filter_by(event_id="evt_new_001").one()
    assert row.event_type == "payment.captured"
    assert row.status == "received"


def test_payload_bytes_stored_exactly_never_reserialized(session):
    """Whitespace, key order, unicode and trailing bytes are preserved."""
    from app.models import WebhookEvent

    raw = (
        b'{\n  "b": 2,\n  "a": 1,\n  "note": "caf\xc3\xa9 \xe2\x9c\x85",'
        b'\n  "quote": "say \\"hi\\""\n}  '
    )
    result = _persist(
        session,
        event_id="evt_exact_payload",
        event_type="payment.captured",
        raw_payload=raw,
    )

    assert result is not None
    row = session.query(WebhookEvent).filter_by(
        event_id="evt_exact_payload"
    ).one()
    assert row.payload == raw.decode("utf-8")


def test_empty_and_whitespace_ids_are_not_normalized(session):
    """The service passes values through verbatim -- no fixing-up."""
    from app.models import WebhookEvent

    assert _persist(session, event_id="") is not None
    empty_row = session.query(WebhookEvent).filter_by(event_id="").one()
    assert empty_row.event_id == ""

    padded = "  evt_pad  "
    assert _persist(session, event_id=padded) is not None
    padded_row = session.query(WebhookEvent).filter_by(event_id=padded).one()
    assert padded_row.event_id == padded  # whitespace preserved exactly


# --------------------------------------------------------------------------
# Duplicate path
# --------------------------------------------------------------------------


def test_duplicate_returns_duplicate_one_row_session_usable(session):
    """Same event_id twice: NEW then DUPLICATE; one row; session healthy."""
    from app.webhooks.service import PersistOutcome

    first = _persist(session, event_id="evt_dup_001")
    second = _persist(session, event_id="evt_dup_001")

    assert first is PersistOutcome.NEW
    assert second is PersistOutcome.DUPLICATE
    assert _count_webhook_events(session) == 1
    # The rollback left the session usable for further work.
    assert session.execute(text("SELECT 1")).scalar() == 1


# --------------------------------------------------------------------------
# Unrelated failures must propagate
# --------------------------------------------------------------------------


class _FakeDBAPIError(Exception):
    """Minimal DBAPI error stand-in for classifier unit tests.

    Covers both SQLSTATE attribute conventions: psycopg 2 ``pgcode``
    and psycopg 3 ``sqlstate``.
    """

    def __init__(
        self,
        message,
        pgcode=None,
        sqlstate=None,
        constraint_name=None,
    ):
        super().__init__(message)
        self.pgcode = pgcode
        self.sqlstate = sqlstate
        self.diag = SimpleNamespace(constraint_name=constraint_name)


def _integrity_error(message, pgcode=None, sqlstate=None, constraint_name=None):
    return IntegrityError(
        "INSERT INTO webhook_events ...",
        {},
        _FakeDBAPIError(message, pgcode, sqlstate, constraint_name),
    )


def test_classifier_accepts_only_the_event_id_unique_violation():
    """Only uq_webhook_events_event_id unique violations are DUPLICATE."""
    from app.webhooks.service import _is_duplicate_event_id

    postgres_duplicate = _integrity_error(
        'duplicate key value violates unique constraint '
        '"uq_webhook_events_event_id"',
        pgcode="23505",
        constraint_name="uq_webhook_events_event_id",
    )
    psycopg3_duplicate = _integrity_error(
        'duplicate key value violates unique constraint '
        '"uq_webhook_events_event_id"',
        sqlstate="23505",
        constraint_name="uq_webhook_events_event_id",
    )
    other_postgres_unique = _integrity_error(
        'duplicate key value violates unique constraint "some_other_uq"',
        pgcode="23505",
        constraint_name="some_other_uq",
    )
    postgres_unique_no_diag = _integrity_error(
        'duplicate key value violates unique constraint '
        '"uq_webhook_events_event_id"',
        pgcode="23505",
        constraint_name=None,
    )
    not_null_violation = _integrity_error(
        'null value in column "event_id" violates not-null constraint',
        sqlstate="23502",
        constraint_name=None,
    )
    sqlite_duplicate = _integrity_error(
        "UNIQUE constraint failed: webhook_events.event_id"
    )
    sqlite_not_null = _integrity_error(
        "NOT NULL constraint failed: webhook_events.event_id"
    )
    sqlite_other_table = _integrity_error(
        "UNIQUE constraint failed: payments.reference_id"
    )

    assert _is_duplicate_event_id(postgres_duplicate) is True
    assert _is_duplicate_event_id(psycopg3_duplicate) is True
    assert _is_duplicate_event_id(postgres_unique_no_diag) is True
    assert _is_duplicate_event_id(other_postgres_unique) is False
    assert _is_duplicate_event_id(not_null_violation) is False
    assert _is_duplicate_event_id(sqlite_duplicate) is True
    assert _is_duplicate_event_id(sqlite_not_null) is False
    assert _is_duplicate_event_id(sqlite_other_table) is False


def test_unrelated_integrity_error_propagates_not_swallowed(session):
    """A NOT NULL failure propagates as an error -- never DUPLICATE."""
    with pytest.raises(IntegrityError):
        _persist(session, event_id=None)  # violates NOT NULL, not UNIQUE

    # The service rolled back: nothing persisted, session still usable.
    assert _count_webhook_events(session) == 0
    assert session.execute(text("SELECT 1")).scalar() == 1


# --------------------------------------------------------------------------
# Malformed input fails loudly before touching the database
# --------------------------------------------------------------------------


def test_malformed_utf8_payload_fails_without_persisting(session):
    """Invalid UTF-8 raises UnicodeDecodeError; no row; session usable."""
    with pytest.raises(UnicodeDecodeError):
        _persist(
            session,
            event_id="evt_bad_utf8",
            raw_payload=b"\xff\xfe broken bytes",
        )

    assert _count_webhook_events(session) == 0
    assert session.execute(text("SELECT 1")).scalar() == 1
