"""spend alerts, why a learner is blocked

Revision ID: a7c3e1f09b52
Revises: dd75f3e149a3
Create Date: 2026-10-23 21:30:00

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e1f09b52'
down_revision: Union[str, Sequence[str], None] = 'dd75f3e149a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('spend_alerts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('learner_id', sa.Uuid(), nullable=False),
        sa.Column('sid', sa.String(length=80), nullable=False),
        sa.Column('lang', sa.String(length=8), nullable=False),
        sa.Column('usd_by_window', sa.JSON(), nullable=False),
        sa.Column('session_usd', sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column('verdict', sa.String(length=8), nullable=False),
        sa.Column('reason', sa.String(length=500), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['learner_id'], ['learners.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_spend_alerts_learner_id'), 'spend_alerts', ['learner_id'], unique=False)
    op.create_index(op.f('ix_spend_alerts_created_at'), 'spend_alerts', ['created_at'], unique=False)
    op.add_column('learners', sa.Column('blocked_reason', sa.String(length=500), server_default='', nullable=False))
    op.add_column('learners', sa.Column('blocked_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('learners', 'blocked_at')
    op.drop_column('learners', 'blocked_reason')
    op.drop_index(op.f('ix_spend_alerts_created_at'), table_name='spend_alerts')
    op.drop_index(op.f('ix_spend_alerts_learner_id'), table_name='spend_alerts')
    op.drop_table('spend_alerts')
