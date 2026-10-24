"""spend by service, and the free allowances' usage (azure characters, letters)

Revision ID: a1c4e7b93d20
Revises: f3a9c1d27e60
Create Date: 2026-10-25 03:00:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c4e7b93d20'
down_revision: Union[str, Sequence[str], None] = 'f3a9c1d27e60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # the spend so far was the tutor's and speech together: it stays as the tutor's
    op.add_column('spend', sa.Column('service', sa.String(16), server_default='tutor', nullable=False))
    op.drop_constraint('spend_pkey', 'spend', type_='primary')
    op.create_primary_key('spend_pkey', 'spend', ['learner_id', 'day', 'service'])
    op.create_table('usage',
                    sa.Column('day', sa.Date(), nullable=False),
                    sa.Column('service', sa.String(16), nullable=False),
                    sa.Column('amount', sa.Integer(), nullable=False, server_default='0'),
                    sa.PrimaryKeyConstraint('day', 'service'))


def downgrade() -> None:
    op.drop_table('usage')
    op.execute("DELETE FROM spend WHERE service <> 'tutor'")
    op.drop_constraint('spend_pkey', 'spend', type_='primary')
    op.create_primary_key('spend_pkey', 'spend', ['learner_id', 'day'])
    op.drop_column('spend', 'service')
