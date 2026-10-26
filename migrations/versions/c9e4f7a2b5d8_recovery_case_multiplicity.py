"""recovery_case_multiplicity

Revision ID: c9e4f7a2b5d8
Revises: a7c3e9d1f5b2
Create Date: 2026-08-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9e4f7a2b5d8'
down_revision: Union[str, Sequence[str], None] = 'a7c3e9d1f5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # An Order may now hold MULTIPLE historical RecoveryCase rows.
    # FK + NOT NULL on recovery_cases.order_id are untouched.
    op.drop_constraint(
        op.f('uq_recovery_cases_order_id'),
        'recovery_cases',
        type_='unique',
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Restores the F1 uniqueness rule exactly. Note: this requires the
    # existing data to satisfy one-case-per-order (no duplicate order_id).
    op.create_unique_constraint(
        op.f('uq_recovery_cases_order_id'),
        'recovery_cases',
        ['order_id'],
    )
