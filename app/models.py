from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    # Uniqueness is enforced by the column-level unique=True constraint
    # (named uq_payments_reference_id via the metadata naming convention).
    reference_id = Column(String(255), unique=True, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(3), default="INR", nullable=False)
    status = Column(String(50), default="pending", nullable=False)
    customer_email = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class WebhookEvent(Base):
    __tablename__ = "webhook_events"

    id = Column(Integer, primary_key=True, index=True)
    # Uniqueness is enforced by the column-level unique=True constraint
    # (named uq_webhook_events_event_id via the metadata naming convention).
    # This single constraint is the authoritative webhook idempotency key.
    event_id = Column(String(255), unique=True, nullable=False)
    event_type = Column(String(100), nullable=False)
    payload = Column(Text, nullable=False)
    received_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    status = Column(String(20), default="received", nullable=False)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    error = Column(Text, nullable=True)


# ---------------------------------------------------------------------------
# Revenue domain foundation (Phase 2, Slice F1).
#
# Identity separation (never interchangeable):
#   WebhookEvent.event_id        = X-Razorpay-Event-Id (delivery identity)
#   Order.razorpay_order_id      = commercial order/opportunity identity
#   PaymentAttempt.razorpay_payment_id = one Razorpay payment entity
#   RecoveryCase.id              = our internal recovery-case identity
#
# One Order = ONE revenue opportunity; it may carry MULTIPLE
# PaymentAttempt rows. A RecoveryCase belongs to the ORDER, never to a
# single payment attempt.
# ---------------------------------------------------------------------------


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    # Authoritative external order identity. Uniqueness is enforced by the
    # column-level unique=True constraint (named uq_orders_razorpay_order_id
    # via the metadata naming convention).
    razorpay_order_id = Column(String(255), unique=True, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(3), default="INR", nullable=False)
    status = Column(String(30), default="created", nullable=False)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    payment_attempts = relationship("PaymentAttempt", back_populates="order")
    recovery_cases = relationship("RecoveryCase", back_populates="order")


class PaymentAttempt(Base):
    __tablename__ = "payment_attempts"

    id = Column(Integer, primary_key=True, index=True)
    # One Razorpay payment entity/attempt. Uniqueness is enforced by the
    # column-level unique=True constraint (named
    # uq_payment_attempts_razorpay_payment_id).
    razorpay_payment_id = Column(String(255), unique=True, nullable=False)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(30), nullable=False)
    failure_code = Column(String(100), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    order = relationship("Order", back_populates="payment_attempts")


class RecoveryCase(Base):
    __tablename__ = "recovery_cases"

    id = Column(Integer, primary_key=True, index=True)
    # LIFECYCLE NOTE (F1.1): an Order may accumulate MULTIPLE historical
    # RecoveryCase rows over its lifetime -- each row represents one
    # recovery episode (e.g. NATURAL_RECOVERY now, a later episode later).
    # No uniqueness and no "active case" constraint exists on order_id:
    # when a new case may be opened is a business decision deferred to the
    # F2/F3 layer. FK integrity and NOT NULL remain authoritative here.
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    status = Column(String(30), default="at_risk", nullable=False)
    amount_at_risk_paise = Column(Integer, nullable=False)
    opened_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    observation_expires_at = Column(DateTime(timezone=True), nullable=True)
    closed_at = Column(DateTime(timezone=True), nullable=True)
    resolution = Column(String(40), nullable=True)

    order = relationship("Order", back_populates="recovery_cases")


class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True, index=True)
    # Uniqueness is enforced by the column-level unique=True constraint
    # (named uq_customers_email via the metadata naming convention).
    email = Column(String(255), unique=True, nullable=False)
    name = Column(String(255), nullable=True)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
