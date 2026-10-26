"""Unit tests for webhook signature verification (app.webhooks.signature).

Pure unit tier: no database, no HTTP, no network. Synthetic secrets only.
"""

import hashlib
import hmac

from app.webhooks.signature import verify

TEST_SECRET = "synthetic-webhook-secret"  # noqa: S105 - fake value, never real
BODY = b'{"event": "payment.failed", "payload": {"synthetic": true}}'


def _sign(body: bytes, secret: str = TEST_SECRET) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def test_valid_signature_returns_true():
    assert verify(BODY, _sign(BODY), TEST_SECRET) is True


def test_modified_body_returns_false():
    forged_body = b'{"event": "payment.captured", "payload": {"synthetic": true}}'
    assert verify(forged_body, _sign(BODY), TEST_SECRET) is False


def test_modified_signature_returns_false():
    valid = _sign(BODY)
    flipped = ("0" if valid[0] != "0" else "1") + valid[1:]
    assert flipped != valid
    assert verify(BODY, flipped, TEST_SECRET) is False


def test_missing_signature_returns_false():
    assert verify(BODY, None, TEST_SECRET) is False


def test_empty_signature_returns_false():
    assert verify(BODY, "", TEST_SECRET) is False
    assert verify(BODY, "   ", TEST_SECRET) is False


def test_wrong_length_signature_returns_false():
    assert verify(BODY, "abc123", TEST_SECRET) is False
    assert verify(BODY, _sign(BODY) + "ff", TEST_SECRET) is False
    assert verify(BODY, _sign(BODY)[:-1], TEST_SECRET) is False


def test_uppercase_and_lowercase_hex_handled_consistently():
    assert verify(BODY, _sign(BODY).upper(), TEST_SECRET) is True
    assert verify(BODY, _sign(BODY).lower(), TEST_SECRET) is True


def test_non_hex_signature_returns_false():
    assert verify(BODY, "z" * 64, TEST_SECRET) is False
    assert verify(BODY, "0x" + _sign(BODY), TEST_SECRET) is False


def test_wrong_secret_returns_false():
    assert verify(BODY, _sign(BODY, "another-synthetic-secret"), TEST_SECRET) is False


def test_empty_secret_fails_closed():
    assert verify(BODY, _sign(BODY), "") is False


def test_verify_never_raises_on_degenerate_signatures():
    degenerate = [
        "",
        "\t\n ",
        "not-even-hex",
        "x" * 10_000,
        _sign(BODY)[:32] + "!" + _sign(BODY)[33:],
        "é" * 64,
    ]
    for candidate in degenerate:
        assert verify(BODY, candidate, TEST_SECRET) is False


def test_verify_is_exact_over_raw_bytes():
    trailing_byte = BODY + b" "
    leading_byte = b" " + BODY
    assert verify(trailing_byte, _sign(BODY), TEST_SECRET) is False
    assert verify(leading_byte, _sign(BODY), TEST_SECRET) is False
    assert verify(trailing_byte, _sign(trailing_byte), TEST_SECRET) is True
