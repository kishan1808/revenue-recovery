"""Model-level tests for the revenue domain foundation (Slice F1).

Pure metadata/mapper introspection: no database connection is made, so
these run everywhere. Real PostgreSQL behaviour (constraints, FKs,
timestamps, migration parity) is proven separately in
tests/test_revenue_domain_integration.py and
tests/test_migrations_integration.py.

app imports are deliberately lazy (inside tests) so this module never
touches app.database at collection time; a sqlite:// DATABASE_URL
fallback keeps bare environments importable.
"""

import os

import pytest
from sqlalchemy import Integer, String, Text


def _ensure_importable_database_url() -> None:
    """Keep app.database importable when no DATABASE_URL is configured."""
    os.environ.setdefault("DATABASE_URL", "sqlite://")


@pytest.fixture(scope="module")
def domain():
    _ensure_importable_database_url()
    from app.models import Order, PaymentAttempt, RecoveryCase

    return {
        "Order": Order,
        "PaymentAttempt": PaymentAttempt,
        "RecoveryCase": RecoveryCase,
    }


def _column(model, name):
    return model.__table__.columns[name]


# --------------------------------------------------------------------------
# Spec 1-3: models exist and map to the required tables
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "table_name"),
    [
        ("Order", "orders"),
        ("PaymentAttempt", "payment_attempts"),
        ("RecoveryCase", "recovery_cases"),
    ],
)
def test_model_exists_and_maps_to_table(domain, class_name, table_name):
    assert domain[class_name].__tablename__ == table_name


# --------------------------------------------------------------------------
# Spec 4: required fields exist with correct types/nullability
# --------------------------------------------------------------------------


def test_order_required_fields(domain):
    order = domain["Order"]
    expected = {
        "id": Integer,
        "razorpay_order_id": String,
        "amount_paise": Integer,
        "currency": String,
        "status": String,
        "created_at": None,  # type checked separately below
        "updated_at": None,
    }
    for name, _ in expected.items():
        assert name in order.__table__.columns, f"missing column {name}"
    assert _column(order, "razorpay_order_id").type.length == 255
    assert not _column(order, "razorpay_order_id").nullable
    assert not _column(order, "amount_paise").nullable
    assert _column(order, "currency").type.length == 3
    assert not _column(order, "currency").nullable
    assert _column(order, "status").type.length == 30
    assert not _column(order, "status").nullable


def test_payment_attempt_required_fields(domain):
    attempt = domain["PaymentAttempt"]
    for name in (
        "id",
        "razorpay_payment_id",
        "order_id",
        "amount_paise",
        "currency",
        "status",
        "failure_code",
        "failure_reason",
        "created_at",
        "updated_at",
    ):
        assert name in attempt.__table__.columns, f"missing column {name}"
    assert _column(attempt, "razorpay_payment_id").type.length == 255
    assert not _column(attempt, "razorpay_payment_id").nullable
    assert not _column(attempt, "order_id").nullable
    assert _column(attempt, "failure_code").type.length == 100
    assert isinstance(_column(attempt, "failure_reason").type, Text)


def test_recovery_case_required_fields(domain):
    case = domain["RecoveryCase"]
    for name in (
        "id",
        "order_id",
        "status",
        "amount_at_risk_paise",
        "opened_at",
        "observation_expires_at",
        "closed_at",
        "resolution",
    ):
        assert name in case.__table__.columns, f"missing column {name}"
    assert _column(case, "status").type.length == 30
    assert not _column(case, "status").nullable
    assert _column(case, "resolution").type.length == 40
    assert _column(case, "resolution").nullable
    assert _column(case, "observation_expires_at").nullable
    assert _column(case, "closed_at").nullable


# --------------------------------------------------------------------------
# Spec 5: exactly one uniqueness mechanism per external identity
# --------------------------------------------------------------------------


def test_external_razorpay_ids_are_unique_columns(domain):
    order = domain["Order"]
    attempt = domain["PaymentAttempt"]

    assert _column(order, "razorpay_order_id").unique is True
    assert _column(attempt, "razorpay_payment_id").unique is True

    # Exactly ONE uniqueness mechanism per external identity: SQLAlchemy
    # normalises the column-level unique=True into a single named
    # table-level UniqueConstraint (uq_orders_razorpay_order_id /
    # uq_payment_attempts_razorpay_payment_id). Assert there is exactly
    # that one -- and nothing else.
    for model, column_name in (
        (order, "razorpay_order_id"),
        (attempt, "razorpay_payment_id"),
    ):
        uniques = [
            u for u in model.__table__.constraints
            if u.__class__.__name__ == "UniqueConstraint"
        ]
        assert len(uniques) == 1
        assert [c.name for c in uniques[0].columns] == [column_name]


def test_recovery_case_allows_multiple_cases_per_order(domain):
    """F1.1: order_id carries NO uniqueness -- recovery history is allowed.

    Multiple historical RecoveryCase rows per Order are legal; when a new
    case may be opened is deferred to the F2/F3 business layer.
    """
    case = domain["RecoveryCase"]
    assert not _column(case, "order_id").unique
    uniques = [
        u for u in case.__table__.constraints
        if u.__class__.__name__ == "UniqueConstraint"
    ]
    assert uniques == []


# --------------------------------------------------------------------------
# Spec 6: foreign keys target orders.id
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("class_name", "column_name"),
    [
        ("PaymentAttempt", "order_id"),
        ("RecoveryCase", "order_id"),
    ],
)
def test_foreign_keys_point_at_orders_id(domain, class_name, column_name):
    model = domain[class_name]
    column = _column(model, column_name)
    foreign_keys = list(column.foreign_keys)
    assert len(foreign_keys) == 1
    fk = foreign_keys[0]
    assert fk.column.table.name == "orders"
    assert fk.column.name == "id"


def test_recovery_case_has_no_payment_attempt_fk(domain):
    """A RecoveryCase belongs to the ORDER, never to a payment attempt."""
    case = domain["RecoveryCase"]
    for column in case.__table__.columns:
        assert not column.foreign_keys or column.name == "order_id"


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------


def test_relationships_wired_both_ways(domain):
    order = domain["Order"]
    attempt = domain["PaymentAttempt"]
    case = domain["RecoveryCase"]

    assert "payment_attempts" in order.__dict__ or hasattr(
        order, "payment_attempts"
    )
    mapper_order = order.__mapper__
    mapper_attempt = attempt.__mapper__
    mapper_case = case.__mapper__

    rel_names_order = {r.key for r in mapper_order.relationships}
    rel_names_attempt = {r.key for r in mapper_attempt.relationships}
    rel_names_case = {r.key for r in mapper_case.relationships}

    assert {"payment_attempts", "recovery_cases"} <= rel_names_order
    assert "order" in rel_names_attempt
    assert "order" in rel_names_case

    # back_populates pairs are mutually wired.
    by_key_order = {r.key: r for r in mapper_order.relationships}
    assert by_key_order["payment_attempts"].back_populates == "order"
    assert by_key_order["recovery_cases"].back_populates == "order"
    assert (
        mapper_attempt.relationships["order"].back_populates
        == "payment_attempts"
    )
    assert (
        mapper_case.relationships["order"].back_populates == "recovery_cases"
    )

    # Relationship targets resolve to the right mappers.
    assert by_key_order["payment_attempts"].mapper is mapper_attempt
    assert by_key_order["recovery_cases"].mapper is mapper_case


# --------------------------------------------------------------------------
# Money / time rules
# --------------------------------------------------------------------------


def test_money_columns_are_integers_never_floats(domain):
    money_columns = {
        "Order": ["amount_paise"],
        "PaymentAttempt": ["amount_paise"],
        "RecoveryCase": ["amount_at_risk_paise"],
    }
    for class_name, names in money_columns.items():
        model = domain[class_name]
        for name in names:
            assert type(_column(model, name).type) is Integer


@pytest.mark.parametrize(
    ("class_name", "timestamp_columns"),
    [
        ("Order", ["created_at", "updated_at"]),
        ("PaymentAttempt", ["created_at", "updated_at"]),
        ("RecoveryCase", ["opened_at"]),
    ],
)
def test_timestamps_are_timezone_aware_with_server_default(
    domain, class_name, timestamp_columns
):
    from sqlalchemy.sql import functions as sa_functions

    model = domain[class_name]
    for name in timestamp_columns:
        column = _column(model, name)
        assert column.type.timezone is True
        assert column.server_default is not None
        assert isinstance(column.server_default.arg, sa_functions.now)


def test_updated_at_uses_onupdate_convention(domain):
    for class_name in ("Order", "PaymentAttempt"):
        column = _column(domain[class_name], "updated_at")
        assert column.onupdate is not None


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------


def test_declared_defaults(domain):
    order = domain["Order"]
    case = domain["RecoveryCase"]
    assert _column(order, "currency").default.arg == "INR"
    assert _column(order, "status").default.arg == "created"
    assert _column(case, "status").default.arg == "at_risk"

    # PaymentAttempt carries NO client defaults for currency/status:
    # those always arrive with real event data.
    attempt = domain["PaymentAttempt"]
    assert _column(attempt, "currency").default is None
    assert _column(attempt, "status").default is None
