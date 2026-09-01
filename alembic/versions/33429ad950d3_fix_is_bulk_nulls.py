"""fix_is_bulk_nulls

Revision ID: 33429ad950d3
Revises: ac805176904f
Create Date: 2026-06-05 22:11:51.848341

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '33429ad950d3'
down_revision: Union[str, Sequence[str], None] = 'ac805176904f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE sms_logs SET is_bulk = false WHERE is_bulk IS NULL")
    op.alter_column('sms_logs', 'is_bulk', server_default=sa.text('false'), nullable=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column('sms_logs', 'is_bulk', server_default=None, nullable=True)

