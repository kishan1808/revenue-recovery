"""Pure Razorpay webhook signature verification.

Standard library only: no I/O, no JSON parsing, no database access.
The webhook secret must never be logged or exposed by this module.
"""

import hashlib
import hmac


def verify(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Return True only when signature is a valid HMAC-SHA256 of raw_body.

    Fails closed (False, never raises) for missing/empty signatures,
    missing/empty secrets, malformed hex, or length mismatches.
    Hex case is normalized before the constant-time comparison.
    """
    if not secret or not signature:
        return False

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    candidate = signature.strip().lower()
    return hmac.compare_digest(
        expected.encode("ascii"),
        candidate.encode("utf-8", errors="replace"),
    )
