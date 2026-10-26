"""Alembic migration integration tests against real PostgreSQL.

DESTRUCTIVE BY DESIGN (full upgrade/downgrade/upgrade cycles) and
therefore strictly opt-in via TEST_DATABASE_URL. tests/conftest.py pins
the process-wide DATABASE_URL to that explicit target, so these cycles
can never touch an unintended database.

Skipped cleanly when TEST_DATABASE_URL is not set.
"""

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set - migration integration tests skipped",
)

EXPECTED_HEAD = "c9e4f7a2b5d8"

# Every domain table the migrations must manage, in sorted order.
ALL_DOMAIN_TABLES = [
    "customers",
    "orders",
    "payment_attempts",
    "payments",
    "recovery_cases",
    "webhook_events",
]


@pytest.fixture(scope="module")
def alembic_cfg():
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    return cfg


@pytest.fixture(scope="module", autouse=True)
def clean_slate(alembic_cfg):
    """Start the destructive cycle from a deterministic empty schema.

    Other integration modules build their tables via
    Base.metadata.create_all, which can leave tables (or a stale
    alembic_version) behind independent of migration history. This
    module owns full up/down/upgrade authority over the schema, so it
    wipes every managed table first.
    """
    engine = _engine()
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "DROP TABLE IF EXISTS recovery_cases, payment_attempts,"
                    " orders, webhook_events, payments, customers CASCADE"
                )
            )
            conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    finally:
        engine.dispose()
    yield


def _engine():
    return create_engine(TEST_DATABASE_URL)


def _domain_tables(engine):
    insp = inspect(engine)
    return sorted(
        t for t in insp.get_table_names() if t in set(ALL_DOMAIN_TABLES)
    )


def _applied_revision(engine):
    with engine.connect() as conn:
        if "alembic_version" not in inspect(conn).get_table_names():
            return None
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
        return rows[0][0] if rows else None


def test_guard_settings_point_at_test_database():
    """Belt-and-braces: the process DATABASE_URL must be the opt-in target."""
    from app.config import settings

    assert settings.DATABASE_URL == TEST_DATABASE_URL


def test_upgrade_head_creates_schema(alembic_cfg):
    """6a. Alembic upgrade head applies the full schema on PostgreSQL."""
    from alembic import command

    command.upgrade(alembic_cfg, "head")

    engine = _engine()
    try:
        assert _domain_tables(engine) == ALL_DOMAIN_TABLES
        assert _applied_revision(engine) == EXPECTED_HEAD
    finally:
        engine.dispose()


def test_downgrade_base_then_reupgrade_restores_schema(alembic_cfg):
    """6b. Downgrade removes exactly what upgrade created; re-upgrade restores."""
    from alembic import command

    command.downgrade(alembic_cfg, "base")

    engine = _engine()
    try:
        assert _domain_tables(engine) == []
        assert _applied_revision(engine) is None

        command.upgrade(alembic_cfg, "head")
        assert _domain_tables(engine) == ALL_DOMAIN_TABLES
        assert _applied_revision(engine) == EXPECTED_HEAD
    finally:
        engine.dispose()


def test_migration_cycle_leaves_no_test_rows():
    """7. The cycle ends clean: fresh tables contain zero rows."""
    import sqlalchemy as sa

    engine = _engine()
    try:
        with engine.connect() as conn:
            for table in (
                "customers",
                "payments",
                "orders",
                "payment_attempts",
                "recovery_cases",
                "webhook_events",
            ):
                rows = conn.execute(
                    sa.select(
                        sa.func.count().label("n")
                    ).select_from(sa.table(table))
                ).scalar()
                assert rows == 0
    finally:
        engine.dispose()
