"""API/orchestration tests for the Razorpay webhook HTTP boundary.

Proves the router contract (Phase 2, Slice E, corrected to the REAL
Razorpay webhook contract) with TestClient:

- event identity comes from the X-Razorpay-Event-Id header;
- the event type comes from the top-level JSON "event" field;
- the REAL Slice A signature verifier runs on the exact raw bytes
  (never mocked in the end-to-end signed-path tests);
- only the already-tested Slice C / Slice D boundaries are mocked so
  orchestration can be observed without touching any database.

Real-database behaviour is covered separately in
tests/test_webhook_api_integration.py.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app.config import settings

TEST_SECRET = "unit-test-webhook-secret"  # noqa: S105  synthetic test value

ACCEPTED = {"status": "accepted"}
SIGNATURE_HEADER = "X-Razorpay-Signature"
EVENT_ID_HEADER = "X-Razorpay-Event-Id"


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _razorpay_envelope(event: str, payment_status: str = "failed") -> dict:
    """A realistic Razorpay-style webhook body (synthetic values only)."""
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
                    "status": payment_status,
                    "order_id": "order_SYNTHETIC01",
                }
            }
        },
        "created_at": 1691735748,
    }


def _body(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _post(client: TestClient, body: bytes, signature: str | None,
          event_id: str | None = "evt_test_001"):
    headers = {}
    if signature is not None:
        headers[SIGNATURE_HEADER] = signature
    if event_id is not None:
        headers[EVENT_ID_HEADER] = event_id
    return client.post("/webhooks/razorpay", content=body, headers=headers)


class _Recorder:
    """Callable stand-in recording every invocation."""

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _SequencedRecorder:
    """Stand-in returning queued results; extra calls fail loudly."""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if not self.results:
            raise AssertionError("unexpected extra call")
        return self.results.pop(0)


@pytest.fixture()
def secret(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", TEST_SECRET)
    return TEST_SECRET


@pytest.fixture()
def client(secret):
    from app.main import app

    return TestClient(app)


# --------------------------------------------------------------------------
# Matrix 1-3: valid Razorpay-style deliveries answer 200 and orchestrate
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event_type", "payment_status"),
    [
        ("payment.failed", "failed"),
        ("payment.captured", "captured"),
        ("order.paid", "captured"),
    ],
)
def test_valid_razorpay_delivery_uses_header_identity_and_body_event(
    client, secret, monkeypatch, event_type, payment_status
):
    from app.webhooks import router as router_module
    from app.webhooks.processor import ProcessOutcome
    from app.webhooks.service import PersistOutcome

    body = _body(_razorpay_envelope(event_type, payment_status))
    persist = _Recorder(PersistOutcome.NEW)
    processed = _Recorder(ProcessOutcome.PROCESSED)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", processed)

    response = _post(client, body, _sign(body), event_id="evt_test_header")

    assert response.status_code == 200
    assert response.json() == ACCEPTED
    assert len(persist.calls) == 1
    _, persist_kwargs = persist.calls[0]
    # Identity source is the HEADER, not anything inside the JSON body.
    assert persist_kwargs["event_id"] == "evt_test_header"
    # Event type source is the top-level JSON "event" field.
    assert persist_kwargs["event_type"] == event_type
    assert persist_kwargs["raw_payload"] == body  # byte-for-byte (matrix 12)

    # NEW -> processor called exactly once with the header identity.
    assert len(processed.calls) == 1
    assert processed.calls[0][0][1] == "evt_test_header"


# --------------------------------------------------------------------------
# Matrix 5/6/7/9-style rejections fail closed before parsing/persistence
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("signature", "event_id"),
    [
        ("0" * 64, "evt_test_001"),  # invalid signature value
        (None, "evt_test_001"),  # missing signature header
        ("", "evt_test_001"),  # empty signature header
        (None, None),  # both missing
    ],
)
def test_invalid_or_missing_signature_is_400_without_db_access(
    client, secret, monkeypatch, signature, event_id
):
    from app.webhooks import router as router_module

    body = _body(_razorpay_envelope("order.paid"))
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    processed = _Recorder(None)
    monkeypatch.setattr(router_module, "process_event", processed)

    response = _post(client, body, signature, event_id=event_id)

    assert response.status_code == 400
    assert persist.calls == []
    assert processed.calls == []


@pytest.mark.parametrize("event_id", [None, "", "   "])
def test_missing_or_blank_event_id_header_is_400_without_db_access(
    client, secret, monkeypatch, event_id
):
    """Matrix 6/7: header identity is mandatory once the signature holds."""
    from app.webhooks import router as router_module

    body = _body(_razorpay_envelope("payment.failed"))
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    response = _post(client, body, _sign(body), event_id=event_id)

    assert response.status_code == 400
    assert persist.calls == []


def test_tampered_body_fails_signature_verification(client, secret, monkeypatch):
    """Valid signature over DIFFERENT bytes is rejected."""
    from app.webhooks import router as router_module

    original = _body(_razorpay_envelope("payment.captured", "captured"))
    tampered = original.replace(b"SYNTHETIC001", b"HACKED00009")

    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    response = _post(client, tampered, _sign(original))

    assert response.status_code == 400
    assert persist.calls == []


# --------------------------------------------------------------------------
# Payload validation happens ONLY after the signature succeeds
# --------------------------------------------------------------------------


def test_valid_signature_with_malformed_json_is_400_without_db_access(
    client, secret, monkeypatch
):
    """Matrix 9."""
    from app.webhooks import router as router_module

    raw = b'{"entity": "event", broken json {{{'
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    response = _post(client, raw, _sign(raw))

    assert response.status_code == 400
    assert persist.calls == []


def test_valid_signature_with_json_array_is_400(client, secret, monkeypatch):
    """Non-object JSON is rejected even when signed."""
    from app.webhooks import router as router_module

    raw = _body([{"event": "payment.failed"}])
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    response = _post(client, raw, _sign(raw))

    assert response.status_code == 400
    assert persist.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"entity": "event"},  # missing top-level "event" (matrix 8)
        {"event": ""},  # empty event field
        {"event": "   "},  # whitespace-only event field
        {"event": 123},  # non-string event field
        {},  # nothing at all
    ],
)
def test_invalid_envelope_fields_are_400_without_db_access(
    client, secret, monkeypatch, payload
):
    from app.webhooks import router as router_module

    body = _body(payload)
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    response = _post(client, body, _sign(body))

    assert response.status_code == 400
    assert persist.calls == []


# --------------------------------------------------------------------------
# Duplicate handling and secret configuration
# --------------------------------------------------------------------------


def test_duplicate_delivery_skips_processor_and_accepts(
    client, secret, monkeypatch
):
    """Matrix 4 (orchestration half): DUPLICATE -> same public shape,
    processor never runs again for the same X-Razorpay-Event-Id."""
    from app.webhooks import router as router_module
    from app.webhooks.service import PersistOutcome

    body = _body(_razorpay_envelope("payment.captured", "captured"))
    persist = _SequencedRecorder([PersistOutcome.NEW, PersistOutcome.DUPLICATE])
    monkeypatch.setattr(router_module, "persist_event", persist)
    processed = _SequencedRecorder([None])  # a second call is a bug
    monkeypatch.setattr(router_module, "process_event", processed)

    first = _post(client, body, _sign(body))
    second = _post(client, body, _sign(body))  # identical redelivery

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json() == ACCEPTED  # identical public shape to NEW
    assert len(persist.calls) == 2
    assert len(processed.calls) == 1  # only the NEW delivery processed


def test_missing_webhook_secret_is_500_without_db_access(monkeypatch):
    """Unconfigured secret fails closed with a safe 500."""
    from fastapi.testclient import TestClient as _TestClient

    from app.main import app
    from app.webhooks import router as router_module

    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    persist = _Recorder(None)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", _Recorder(None))

    client = _TestClient(app)
    body = _body(_razorpay_envelope("order.paid"))

    response = _post(client, body, _sign(body))

    assert response.status_code == 500
    assert response.json() == {"detail": "Webhook is not configured"}
    assert persist.calls == []
    assert TEST_SECRET not in response.text


# --------------------------------------------------------------------------
# Matrix 10 + 12: unknown-but-valid type reaches persistence; raw fidelity
# --------------------------------------------------------------------------


def test_unknown_but_valid_event_still_reaches_the_processor(
    client, secret, monkeypatch
):
    """Supported-vs-unknown is the PROCESSOR's decision, not the router's."""
    from app.webhooks import router as router_module
    from app.webhooks.processor import ProcessOutcome
    from app.webhooks.service import PersistOutcome

    body = _body(_razorpay_envelope("refund.processed"))
    persist = _Recorder(PersistOutcome.NEW)
    processed = _Recorder(ProcessOutcome.IGNORED)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(router_module, "process_event", processed)

    response = _post(client, body, _sign(body))

    assert response.status_code == 200
    assert persist.calls[0][1]["event_type"] == "refund.processed"
    assert len(processed.calls) == 1


def test_payload_bytes_reach_persistence_unchanged(client, secret, monkeypatch):
    """Matrix 12: raw bytes preserved exactly through the boundary."""
    from app.webhooks import router as router_module
    from app.webhooks.service import PersistOutcome

    raw = (
        b'{\n  "entity": "event",\n  "account_id": "acc_test",\n'
        b'  "event": "order.paid",\n  "note": "caf\xc3\xa9 \xe2\x9c\x85",\n'
        b'  "payload": {"order": {"entity": {"id": "order_SYN"}}}\n}  '
    )
    persist = _Recorder(PersistOutcome.NEW)
    monkeypatch.setattr(router_module, "persist_event", persist)
    monkeypatch.setattr(
        router_module,
        "process_event",
        _Recorder(None),  # outcome irrelevant here
    )

    response = _post(client, raw, _sign(raw))

    assert response.status_code == 200
    assert persist.calls[0][1]["raw_payload"] == raw
