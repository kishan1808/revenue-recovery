"""initial_schema

Revision ID: 558077c16b3a
Revises:
Create Date: 2026-08-22 04:00:02.625033

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '558077c16b3a'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
