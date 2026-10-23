"""access codes carry the learner's name, and an emailed invite: its address and link

Revision ID: c41d8e2a7f13
Revises: a7c3e1f09b52
Create Date: 2026-10-23 22:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c41d8e2a7f13'
down_revision: Union[str, Sequence[str], None] = 'a7c3e1f09b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('access_codes', sa.Column('name', sa.String(length=80), server_default='', nullable=False))
    op.add_column('access_codes', sa.Column('email', sa.String(length=320), nullable=True))
    op.add_column('access_codes', sa.Column('link', sa.String(length=64), nullable=True))
    op.create_unique_constraint('uq_access_codes_link', 'access_codes', ['link'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('uq_access_codes_link', 'access_codes', type_='unique')
    op.drop_column('access_codes', 'link')
    op.drop_column('access_codes', 'email')
    op.drop_column('access_codes', 'name')
