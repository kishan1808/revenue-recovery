"""initial_schema

Revision ID: 558077c16b3a
Revises:
Create Date: 2026-08-23 01:36:36.358850

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '558077c16b3a'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('customers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=True),
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
    sa.PrimaryKeyConstraint('id', name=op.f('pk_customers')),
    sa.UniqueConstraint('email', name=op.f('uq_customers_email'))
    )
    op.create_index(op.f('ix_customers_id'), 'customers', ['id'], unique=False)
    op.create_table('payments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('reference_id', sa.String(length=255), nullable=False),
    sa.Column('amount_paise', sa.Integer(), nullable=False),
    sa.Column('currency', sa.String(length=3), nullable=False),
    sa.Column('status', sa.String(length=50), nullable=False),
    sa.Column('customer_email', sa.String(length=255), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
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
    sa.PrimaryKeyConstraint('id', name=op.f('pk_payments')),
    sa.UniqueConstraint('reference_id', name=op.f('uq_payments_reference_id'))
    )
    op.create_index(op.f('ix_payments_id'), 'payments', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_payments_id'), table_name='payments')
    op.drop_table('payments')
    op.drop_index(op.f('ix_customers_id'), table_name='customers')
    op.drop_table('customers')
