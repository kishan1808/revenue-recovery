"""Real-PostgreSQL integration tests for the Razorpay HTTP boundary
(corrected to the REAL Razorpay webhook contract).

Exercises the COMPLETE path -- raw bytes -> signature verification ->
X-Razorpay-Event-Id identity -> top-level "event" extraction ->
persist_event -> process_event -> response -- against a REAL PostgreSQL
database via the existing TEST_DATABASE_URL isolation mechanism (no
DATABASE_URL fallback, no new configuration).

Only the processor's HANDLERS mapping is ever touched, and only to
simulate handler crashes or count processor runs (via a delegating
wrapper around the real process_event); everything else -- signature
verifier, persistence service, processor dispatch, router, database --
is fully real.

Skipped entirely when TEST_DATABASE_URL is absent. Synthetic values only.
"""

import hashlib
import hmac
import json
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL not set - PostgreSQL integration tests skipped",
)

API_SECRET = "integration-test-webhook-secret"  # noqa: S105  synthetic

SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def _sign(body: bytes, secret: str = API_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _razorpay_envelope(event: str) -> dict:
    """A realistic Razorpay-style webhook body (synthetic values only)."""
    status = "captured" if event == "payment.captured" else "failed"
    return {
        "entity": "event",
        "account_id": "acc_test",
        "event": event,
        "contains": ["payment"],
        "payload": {
            "payment": {
                "entity": {
                    "id": "pay_SYNTHETIC001",
                    "amount": 50000,
                    "currency": "INR",
                    "status": status,
                    "order_id": "order_SYNTHETIC01",
                }
            },
            "order": {
                "entity": {
                    "id": "order_SYNTHETIC01",
                    "amount": 50000,
                    "currency": "INR",
                    "status": "created",
                }
            },
        },
        "created_at": 1691735748,
    }


def _post(client: TestClient, body: bytes, event_id="evt_test_001"):
    return client.post(
        "/webhooks/razorpay",
        content=body,
        headers={
            SIGNATURE_HEADER: _sign(body),
            EVENT_ID_HEADER: event_id,
        },
    )


@pytest.fixture(scope="module")
def pg_engine():
    """Engine bound to the explicit test database, schema ensured."""
    import app.models  # noqa: F401  # register models on Base.metadata
    from app.database import Base

    engine = create_engine(TEST_DATABASE_URL)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_webhook_events(pg_engine, monkeypatch):
    """Isolation + configured secret + zero leftover rows per test."""
    from app.config import settings

    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", API_SECRET)

    def _truncate():
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE webhook_events RESTART IDENTITY"))

    _truncate()
    yield
    _truncate()


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def _row(pg_engine, event_id):
    from app.models import WebhookEvent

    with Session(pg_engine) as session:
        return (
            session.query(WebhookEvent)
            .filter_by(event_id=event_id)
            .one_or_none()
        )


# --------------------------------------------------------------------------
# Matrix 1/2/3 + 10: valid deliveries persist one row keyed by the HEADER
# id, with byte-for-byte payload fidelity and correct lifecycle outcome.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "expected_status"),
    [
        ("payment.failed", "processed"),  # matrix 1
        ("payment.captured", "processed"),  # matrix 2
        ("order.paid", "processed"),  # matrix 3
        ("refund.created", "ignored"),  # matrix 10: unknown -> durably ignored
    ],
)
def test_valid_request_persists_one_row_keyed_by_header_id(
    client, pg_engine, event_type, expected_status
):
    raw = json.dumps(_razorpay_envelope(event_type)).encode("utf-8")

    response = _post(client, raw, event_id=f"evt_hdr_{event_type}")

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    with Session(pg_engine) as session:
        rows = session.execute(
            text("SELECT * FROM webhook_events")
        ).mappings().all()

    assert len(rows) == 1
    row = rows[0]
    # The row identity is the X-Razorpay-Event-Id header value.
    assert row["event_id"] == f"evt_hdr_{event_type}"
    assert row["event_type"] == event_type
    assert row["payload"] == raw.decode("utf-8")  # exact payload preserved
    assert row["status"] == expected_status
    assert row["received_at"] is not None
    assert row["processed_at"] is not None


def test_tricky_bytes_survive_the_whole_http_path(client, pg_engine):
    """Matrix 12 with hostile bytes: newlines, unicode, trailing spaces."""
    raw = (
        b'{\n  "entity": "event",\n'
        b'  "event": "payment.captured",\n'
        b'  "note": "caf\xc3\xa9 \xe2\x9c\x85 say \\"hi\\""\n}  '
    )

    response = _post(client, raw, event_id="evt_tricky_http")

    assert response.status_code == 200
    stored = _row(pg_engine, "evt_tricky_http")
    assert stored is not None
    assert stored.payload == raw.decode("utf-8")


# --------------------------------------------------------------------------
# Matrix 4: duplicate header delivery answers 200 twice, ONE row,
# and the processor runs EXACTLY once.
# --------------------------------------------------------------------------


def test_duplicate_header_delivery_processes_exactly_once(
    client, pg_engine, monkeypatch
):
    from app.webhooks import router as router_module

    raw = json.dumps(_razorpay_envelope("payment.captured")).encode("utf-8")

    real_process_event = router_module.process_event
    runs = []

    def counting(session, event_id):
        runs.append(event_id)
        return real_process_event(session, event_id)

    monkeypatch.setattr(router_module, "process_event", counting)

    first = _post(client, raw, event_id="evt_dup_header")
    second = _post(client, raw, event_id="evt_dup_header")  # same redelivery

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == {"status": "accepted"}  # identical public shape

    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
        stored = session.execute(
            text("SELECT event_id, status FROM webhook_events")
        ).mappings().one()

    assert count == 1
    assert stored["event_id"] == "evt_dup_header"
    assert stored["status"] == "processed"
    assert len(runs) == 1  # processor executed EXACTLY once
    assert runs[0] == "evt_dup_header"


def test_same_body_different_header_is_a_distinct_event(client, pg_engine):
    """Identity lives ONLY in the header: a new id means a NEW event."""
    raw = json.dumps(_razorpay_envelope("payment.captured")).encode("utf-8")

    first = _post(client, raw, event_id="evt_delivery_one")
    second = _post(client, raw, event_id="evt_delivery_two")

    assert first.status_code == 200
    assert second.status_code == 200
    with Session(pg_engine) as session:
        ids = session.execute(
            text("SELECT event_id FROM webhook_events ORDER BY id")
        ).scalars().all()
    assert sorted(ids) == ["evt_delivery_one", "evt_delivery_two"]


# --------------------------------------------------------------------------
# Matrix 5/6/7/8/9-style rejections leave zero rows
# --------------------------------------------------------------------------


def test_invalid_signature_creates_zero_rows(client, pg_engine):
    raw = json.dumps(_razorpay_envelope("order.paid")).encode("utf-8")

    response = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={SIGNATURE_HEADER: "0" * 64, EVENT_ID_HEADER: "evt_never"},
    )

    assert response.status_code == 400
    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
    assert count == 0


def test_missing_event_id_header_creates_zero_rows(client, pg_engine):
    raw = json.dumps(_razorpay_envelope("payment.failed")).encode("utf-8")

    response = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={SIGNATURE_HEADER: _sign(raw)},  # no event id header
    )

    assert response.status_code == 400
    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
    assert count == 0


def test_empty_event_id_header_creates_zero_rows(client, pg_engine):
    raw = json.dumps(_razorpay_envelope("payment.failed")).encode("utf-8")

    response = client.post(
        "/webhooks/razorpay",
        content=raw,
        headers={SIGNATURE_HEADER: _sign(raw), EVENT_ID_HEADER: "   "},
    )

    assert response.status_code == 400
    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
    assert count == 0


def test_missing_top_level_event_field_creates_zero_rows(client, pg_engine):
    raw = json.dumps({"entity": "event", "account_id": "acc_test"}).encode()

    response = _post(client, raw, event_id="evt_no_event_field")

    assert response.status_code == 400
    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
    assert count == 0


def test_malformed_json_creates_zero_rows(client, pg_engine):
    raw = b'{"entity": "event", broken {{{'

    response = _post(client, raw, event_id="evt_broken_http")

    assert response.status_code == 400
    with Session(pg_engine) as session:
        count = session.execute(
            text("SELECT count(*) FROM webhook_events")
        ).scalar()
    assert count == 0


# --------------------------------------------------------------------------
# Matrix 11: handler failure -> durable failed row keyed by header id,
# still HTTP 200.
# --------------------------------------------------------------------------


def test_handler_failure_records_failed_and_returns_200(
    client, pg_engine, monkeypatch
):
    from app.webhooks import processor as processor_module

    def exploding_handler(session, event):
        raise RuntimeError("synthetic handler crash")

    monkeypatch.setitem(
        processor_module.HANDLERS, "payment.failed", exploding_handler
    )
    raw = json.dumps(_razorpay_envelope("payment.failed")).encode("utf-8")

    response = _post(client, raw, event_id="evt_http_failed")

    assert response.status_code == 200  # accepted-and-recorded semantics
    assert response.json() == {"status": "accepted"}
    stored = _row(pg_engine, "evt_http_failed")
    assert stored is not None
    assert stored.event_id == "evt_http_failed"  # header identity persisted
    assert stored.status == "failed"
    assert stored.error is not None
    assert "RuntimeError" in stored.error
    assert "\n" not in stored.error
    assert len(stored.error) <= 500
