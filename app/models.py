from sqlalchemy import Column, DateTime, Integer, String, Text
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
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


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
