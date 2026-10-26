"""webhook_events

Revision ID: c4f81a92d3b7
Revises: 558077c16b3a
Create Date: 2026-08-24 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4f81a92d3b7'
down_revision: Union[str, Sequence[str], None] = '558077c16b3a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('webhook_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('event_id', sa.String(length=255), nullable=False),
    sa.Column('event_type', sa.String(length=100), nullable=False),
    sa.Column('payload', sa.Text(), nullable=False),
    sa.Column(
        'received_at',
        sa.DateTime(timezone=True),
        server_default=sa.text('(CURRENT_TIMESTAMP)'),
        nullable=False,
    ),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('processed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_webhook_events')),
    sa.UniqueConstraint('event_id', name=op.f('uq_webhook_events_event_id'))
    )
    op.create_index(
        op.f('ix_webhook_events_id'), 'webhook_events', ['id'], unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_webhook_events_id'), table_name='webhook_events')
    op.drop_table('webhook_events')
