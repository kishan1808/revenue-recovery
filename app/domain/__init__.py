"""Revenue domain package (Phase 2, Slice F2).

Deterministic correlation of verified Razorpay webhook events into the
revenue domain: Order -> PaymentAttempt(s) -> RecoveryCase.

Independent of FastAPI, HTTP, and Razorpay SDK details.
"""
