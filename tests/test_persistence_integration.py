"""PostgreSQL persistence integration tests (Phase 1 foundation).

These tests run against a REAL PostgreSQL database and are strictly
opt-in: they are skipped unless the TEST_DATABASE_URL environment
variable is set (see tests/conftest.py for the isolation policy).

Never set TEST_DATABASE_URL to a production database.
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
def clean_tables(pg_engine):
    """Truncate around every test: full isolation + zero leftover rows."""

    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE payments, customers RESTART IDENTITY"))

    _truncate()
    yield
    _truncate()


def test_database_connection_is_real_postgres(pg_engine):
    """1. Connection works and is genuinely PostgreSQL via psycopg."""
    assert pg_engine.dialect.name == "postgresql"
    assert pg_engine.dialect.driver == "psycopg"
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT 1")).scalar() == 1
        version = conn.execute(text("SELECT version()")).scalar()
        assert "PostgreSQL" in version


def test_customer_insert_read_round_trip(pg_engine):
    """2. Customer survives a commit and reads back identically."""
    from app.models import Customer

    email = "synthetic.customer@example.test"
    with Session(pg_engine) as session:
        session.add(Customer(email=email, name="Synthetic Customer"))
        session.commit()

    with Session(pg_engine) as reader:
        row = reader.query(Customer).filter_by(email=email).one()
        assert row.email == email
        assert row.name == "Synthetic Customer"
        assert row.created_at is not None
        assert row.created_at.tzinfo is not None


def test_payment_insert_read_round_trip(pg_engine):
    """3. Payment survives a commit and reads back identically."""
    from app.models import Customer, Payment

    email = "synthetic.payer@example.test"
    reference = "synthetic-payment-001"
    with Session(pg_engine) as session:
        session.add(Customer(email=email, name="Synthetic Payer"))
        session.add(
            Payment(
                reference_id=reference,
                amount_paise=150000,
                currency="INR",
                status="pending",
                customer_email=email,
                description="integration round trip row",
            )
        )
        session.commit()

    with Session(pg_engine) as reader:
        payment = reader.query(Payment).filter_by(reference_id=reference).one()
        assert payment.amount_paise == 150000
        assert payment.currency == "INR"
        assert payment.status == "pending"
        assert payment.customer_email == email
        assert payment.description == "integration round trip row"
        assert payment.created_at is not None
        assert payment.created_at.tzinfo is not None


def test_duplicate_reference_id_rejected(pg_engine):
    """4. The payments.reference_id UNIQUE constraint is enforced by PG."""
    from app.models import Payment

    reference = "dup-check-001"
    with Session(pg_engine) as session:
        session.add(
            Payment(
                reference_id=reference,
                amount_paise=100,
                customer_email="first@example.test",
            )
        )
        session.commit()

        session.add(
            Payment(
                reference_id=reference,  # duplicate on purpose
                amount_paise=200,
                customer_email="second@example.test",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    # Session/connection still usable after the failed transaction.
    with Session(pg_engine) as probe:
        remaining = probe.query(Payment).filter_by(reference_id=reference).all()
        assert len(remaining) == 1
        assert probe.execute(text("SELECT 1")).scalar() == 1


def test_rollback_discards_uncommitted_row(pg_engine):
    """5. A rolled-back transaction leaves no trace."""
    from app.models import Payment

    ghost_reference = "rolled-back-999"
    with Session(pg_engine) as session:
        session.add(
            Payment(
                reference_id=ghost_reference,
                amount_paise=1,
                customer_email="ghost@example.test",
            )
        )
        session.rollback()

    with Session(pg_engine) as reader:
        assert (
            reader.query(Payment).filter_by(reference_id=ghost_reference).first()
            is None
        )


def test_no_rows_left_behind(pg_engine):
    """7. Cleanup fixture guarantees an empty domain state after tests."""
    from app.models import Customer, Payment

    with Session(pg_engine) as reader:
        assert reader.query(Payment).count() == 0
        assert reader.query(Customer).count() == 0
