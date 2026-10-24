"""access requests from the landing's form, kept until the admin decides

Revision ID: e5b1f7c2a9d4
Revises: c41d8e2a7f13
Create Date: 2026-10-24 19:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5b1f7c2a9d4'
down_revision: Union[str, Sequence[str], None] = 'c41d8e2a7f13'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('access_requests',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=80), nullable=False),
        sa.Column('email', sa.String(length=320), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email')
    )
    op.create_index(op.f('ix_access_requests_created_at'), 'access_requests', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_access_requests_created_at'), table_name='access_requests')
    op.drop_table('access_requests')
