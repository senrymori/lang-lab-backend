"""drift level: how far a learner has gone off the lesson (app/hosting.py: drift)

Revision ID: f3a9c1d27e60
Revises: e5b1f7c2a9d4
Create Date: 2026-10-25 02:10:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a9c1d27e60'
down_revision: Union[str, Sequence[str], None] = 'e5b1f7c2a9d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('learners', sa.Column('drift_level', sa.Integer(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('learners', 'drift_level')
