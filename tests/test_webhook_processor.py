"""Unit tests for the webhook event processor (Phase 2, Slice D).

Pure processor tests: no HTTP, no Razorpay. Runs against in-memory
SQLite so no external database is required; real PostgreSQL behaviour is
proven separately in tests/test_webhook_processor_integration.py.

app imports are deliberately lazy (inside fixtures/tests) so this module
never touches app.database at collection time; a sqlite:// DATABASE_URL
fallback keeps bare environments importable.
"""

import os

import pytest
from sqlalchemy import create_engine
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


def _insert_received(
    session,
    *,
    event_id="evt_proc_001",
    event_type="payment.failed",
    payload='{"event": "%s", "synthetic": true}',
):
    """Insert one status='received' WebhookEvent directly."""
    from app.models import WebhookEvent

    event = WebhookEvent(
        event_id=event_id,
        event_type=event_type,
        payload=payload.replace("%s", event_type),
    )
    session.add(event)
    session.commit()
    return event


def _reload(session, event_id):
    from app.models import WebhookEvent

    session.expire_all()
    return session.query(WebhookEvent).filter_by(event_id=event_id).one()


# --------------------------------------------------------------------------
# Registry shape (spec item 12)
# --------------------------------------------------------------------------


def test_handler_registry_contains_exactly_the_initial_event_types():
    from app.webhooks.processor import HANDLERS

    assert set(HANDLERS) == {
        "payment.failed",
        "payment.authorized",
        "payment.captured",
        "order.paid",
    }
    assert all(callable(handler) for handler in HANDLERS.values())


# --------------------------------------------------------------------------
# Known event types dispatch and reach processed
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        (
            "payment.failed",
            '{"event":"payment.failed","payload":{"payment":{"entity":'
            '{"id":"pay_u1","order_id":"order_u1","amount":150000,'
            '"currency":"INR","status":"failed"}},"order":{"entity":'
            '{"id":"order_u1","amount":150000,"currency":"INR"}}}}',
        ),
        (
            "payment.captured",
            '{"event":"payment.captured","payload":{"payment":{"entity":'
            '{"id":"pay_u2","order_id":"order_u2","amount":150000,'
            '"currency":"INR","status":"captured"}},"order":{"entity":'
            '{"id":"order_u2","amount":150000,"currency":"INR"}}}}',
        ),
        (
            "order.paid",
            '{"event":"order.paid","payload":{"order":{"entity":'
            '{"id":"order_u3","amount":150000,"currency":"INR"}},'
            '"payment":{"entity":{"id":"pay_u3","order_id":"order_u3",'
            '"amount":150000,"currency":"INR","status":"captured"}}}}',
        ),
    ],
)
def test_known_event_types_reach_processed(session, event_type, payload):
    """Spec items 1-3 + 7: dispatched handler -> processed + timestamp."""
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    event_id = f"evt_ok_{event_type.replace('.', '_')}"
    _insert_received(
        session, event_id=event_id, event_type=event_type, payload=payload
    )

    assert process_event(session, event_id) is ProcessOutcome.PROCESSED

    row = _reload(session, event_id)
    assert isinstance(row, WebhookEvent)
    assert row.status == "processed"
    assert row.processed_at is not None
    assert row.error is None


# --------------------------------------------------------------------------
# Unknown / unsupported event types are ignored
# --------------------------------------------------------------------------


def test_unknown_event_type_becomes_ignored(session):
    """Spec item 4 + 6: valid but unsupported type -> ignored + timestamp."""
    from app.webhooks.processor import ProcessOutcome, process_event

    _insert_received(
        session,
        event_id="evt_unknown_type",
        event_type="refund.processed",
        payload='{"event":"refund.processed"}',
    )

    assert process_event(session, "evt_unknown_type") is (
        ProcessOutcome.IGNORED
    )

    row = _reload(session, "evt_unknown_type")
    assert row.status == "ignored"
    assert row.processed_at is not None
    assert row.error is None


# --------------------------------------------------------------------------
# Terminal events are never reprocessed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["processed", "ignored", "failed"])
def test_terminal_events_return_already_terminal_untouched(
    session, terminal_status
):
    """Spec items 5-7 + 3: terminal states are never re-entered."""
    from app.webhooks.processor import ProcessOutcome, process_event

    event_id = f"evt_terminal_{terminal_status}"
    event = _insert_received(session, event_id=event_id)
    event.status = terminal_status
    if terminal_status == "failed":
        event.error = "earlier synthetic failure"
    session.commit()

    frozen_status = event.status
    frozen_error = event.error

    assert process_event(session, event_id) is ProcessOutcome.ALREADY_TERMINAL

    row = _reload(session, event_id)
    assert row.status == frozen_status
    assert row.processed_at is None
    assert row.error == frozen_error


# --------------------------------------------------------------------------
# Handler failures -> failed with safe bounded error text
# --------------------------------------------------------------------------


def test_structurally_invalid_payload_marks_event_failed(session):
    """Spec item 8: genuine handler failure -> failed + error recorded."""
    from app.webhooks.processor import (
        PayloadStructureError,
        ProcessOutcome,
        process_event,
    )

    _insert_received(
        session,
        event_id="evt_bad_payload",
        event_type="payment.captured",
        payload="not json at all {{{",
    )

    outcome = process_event(session, "evt_bad_payload")

    assert outcome is ProcessOutcome.FAILED
    row = _reload(session, "evt_bad_payload")
    assert row.status == "failed"
    assert row.processed_at is not None
    assert row.error is not None
    assert PayloadStructureError.__name__ in row.error


def test_arbitrary_handler_exception_is_contained_and_recorded(
    session, monkeypatch
):
    """Any handler exception -> failed; nothing propagates to the caller's
    transaction state beyond the recorded failure."""
    from app.webhooks import processor
    from app.webhooks.processor import ProcessOutcome, process_event

    def exploding_handler(session, event):
        raise RuntimeError("synthetic handler explosion")

    monkeypatch.setitem(
        processor.HANDLERS, "payment.failed", exploding_handler
    )
    _insert_received(
        session, event_id="evt_boom", event_type="payment.failed"
    )

    assert process_event(session, "evt_boom") is ProcessOutcome.FAILED

    row = _reload(session, "evt_boom")
    assert row.status == "failed"
    assert row.processed_at is not None
    assert "RuntimeError" in row.error
    assert "synthetic handler explosion" in row.error


def test_failed_processing_preserves_the_durable_event_row(session):
    """Spec item 9: failure never deletes or replaces the WebhookEvent."""
    from app.models import WebhookEvent
    from app.webhooks.processor import ProcessOutcome, process_event

    original_payload = '{"event":"payment.failed","broken": true'
    _insert_received(
        session,
        event_id="evt_survives",
        event_type="payment.failed",
        payload=original_payload,
    )

    assert process_event(session, "evt_survives") is ProcessOutcome.FAILED

    assert session.query(WebhookEvent).count() == 1
    row = _reload(session, "evt_survives")
    assert row.payload == original_payload
    assert row.received_at is not None


def test_error_text_is_single_line_bounded_and_leak_free(session, monkeypatch):
    """Spec item 11: safe, bounded error text only."""
    from app.webhooks import processor
    from app.webhooks.processor import (
        _MAX_ERROR_LENGTH,
        ProcessOutcome,
        process_event,
    )

    def leaky_handler(session, event):
        raise ValueError(
            "line one\nline two\twith tab\n"
            "password=hunter2 connection=postgres://user:pass@host/db "
            + "x" * 10_000
        )

    monkeypatch.setitem(processor.HANDLERS, "order.paid", leaky_handler)
    _insert_received(
        session,
        event_id="evt_leak_check",
        event_type="order.paid",
        payload="{}",
    )

    assert process_event(session, "evt_leak_check") is ProcessOutcome.FAILED

    row = _reload(session, "evt_leak_check")
    assert row.error is not None
    assert len(row.error) <= _MAX_ERROR_LENGTH
    assert "\n" not in row.error
    assert "\t" not in row.error
    assert "Traceback" not in row.error
    assert row.error.startswith("ValueError:")


def test_error_text_truncation_marker_present_for_huge_messages(
    session, monkeypatch
):
    from app.webhooks import processor
    from app.webhooks.processor import (
        _MAX_ERROR_LENGTH,
        ProcessOutcome,
        process_event,
    )

    def huge_handler(session, event):
        raise ValueError("y" * (_MAX_ERROR_LENGTH * 3))

    monkeypatch.setitem(processor.HANDLERS, "order.paid", huge_handler)
    _insert_received(
        session, event_id="evt_huge", event_type="order.paid", payload="{}"
    )

    assert process_event(session, "evt_huge") is ProcessOutcome.FAILED

    row = _reload(session, "evt_huge")
    assert len(row.error) == _MAX_ERROR_LENGTH
    assert row.error.endswith("...[truncated]")


# --------------------------------------------------------------------------
# Loading / contract edge cases
# --------------------------------------------------------------------------


def test_missing_event_id_raises_value_error(session):
    from app.webhooks.processor import process_event

    with pytest.raises(ValueError):
        process_event(session, "evt_does_not_exist")


# --------------------------------------------------------------------------
# Spec item 10 across every terminal outcome
# --------------------------------------------------------------------------


def test_processed_at_populated_for_all_terminal_states(session):
    from app.webhooks.processor import ProcessOutcome, process_event

    cases = {
        "evt_term_processed": (
            "payment.captured",
            '{"event":"payment.captured","payload":{"payment":{"entity":'
            '{"id":"pay_term","order_id":"order_term","amount":100,'
            '"currency":"INR","status":"captured"}},"order":{"entity":'
            '{"id":"order_term","amount":100,"currency":"INR"}}}}',
            ProcessOutcome.PROCESSED,
        ),
        "evt_term_ignored": (
            "invoice.paid",
            '{"ok":true}',
            ProcessOutcome.IGNORED,
        ),
        "evt_term_failed": (
            "payment.failed",
            "{invalid",
            ProcessOutcome.FAILED,
        ),
    }
    for event_id, (event_type, payload, _) in cases.items():
        _insert_received(
            session, event_id=event_id, event_type=event_type, payload=payload
        )

    for event_id, (_, _, expected_outcome) in cases.items():
        assert process_event(session, event_id) is expected_outcome
        row = _reload(session, event_id)
        assert row.processed_at is not None
