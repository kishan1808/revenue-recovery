"""revenue_domain_tables

Revision ID: a7c3e9d1f5b2
Revises: c4f81a92d3b7
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7c3e9d1f5b2'
down_revision: Union[str, Sequence[str], None] = 'c4f81a92d3b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Dependency-safe order: orders -> payment_attempts -> recovery_cases.
    op.create_table('orders',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('razorpay_order_id', sa.String(length=255), nullable=False),
    sa.Column('amount_paise', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column(
        'created_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.Column(
        'updated_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_orders')),
    sa.UniqueConstraint(
        'razorpay_order_id', name=op.f('uq_orders_razorpay_order_id')
    )
    )
    op.create_index(op.f('ix_orders_id'), 'orders', ['id'], unique=False)
    op.create_table('payment_attempts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('razorpay_payment_id', sa.String(length=255), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('amount_paise', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('failure_code', sa.String(length=100), nullable=True),
    sa.Column('failure_reason', sa.Text(), nullable=True),
    sa.Column(
        'created_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.Column(
        'updated_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.ForeignKeyConstraint(
        ['order_id'],
        ['orders.id'],
        name=op.f('fk_payment_attempts_orders_order_id'),
    ),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payment_attempts')),
    sa.UniqueConstraint(
        'razorpay_payment_id',
        name=op.f('uq_payment_attempts_razorpay_payment_id'),
    )
    )
    op.create_index(
        op.f('ix_payment_attempts_id'), 'payment_attempts', ['id'],
        unique=False,
    )
    op.create_table('recovery_cases',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('order_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('amount_at_risk_paise', sa.Integer(), nullable=False),
    sa.Column(
        'opened_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.Column('observation_expires_at', sa.DateTime(timezone=True),
              nullable=True),
    sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('resolution', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(
        ['order_id'],
        ['orders.id'],
        name=op.f('fk_recovery_cases_orders_order_id'),
    ),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_recovery_cases')),
    sa.UniqueConstraint('order_id', name=op.f('uq_recovery_cases_order_id'))
    )
    op.create_index(
        op.f('ix_recovery_cases_id'), 'recovery_cases', ['id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Reverse of upgrade: recovery_cases -> payment_attempts -> orders.
    op.drop_index(
        op.f('ix_recovery_cases_id'), table_name='recovery_cases'
    )
    op.drop_table('recovery_cases')
    op.drop_index(
        op.f('ix_payment_attempts_id'), table_name='payment_attempts'
    )
    op.drop_table('payment_attempts')
    op.drop_index(op.f('ix_orders_id'), table_name='orders')
    op.drop_table('orders')
