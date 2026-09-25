"""Retain collection attempts and source evidence before review staging."""
from alembic import op
import sqlalchemy as sa

revision = 'c8d9e0f1a2b3'
down_revision = 'b7c8d9e0f1a2'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('portal_collection_runs',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('kind',sa.String(12),nullable=False),
        sa.Column('status',sa.String(24),nullable=False),
        sa.Column('started_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('finished_at',sa.DateTime(timezone=True)),
        sa.Column('summary_json',sa.Text()),sa.Column('error_type',sa.String(120)))
    op.create_table('portal_observations',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('run_id',sa.Integer(),sa.ForeignKey('portal_collection_runs.id'),nullable=False),
        sa.Column('source',sa.String(24),nullable=False),sa.Column('kind',sa.String(12),nullable=False),
        sa.Column('url',sa.Text(),nullable=False),sa.Column('collected_at',sa.DateTime(timezone=True),nullable=False),
        sa.Column('payload_json',sa.Text(),nullable=False))
    op.create_index('ix_portal_observations_run_id','portal_observations',['run_id'])


def downgrade():
    op.drop_table('portal_observations')
    op.drop_table('portal_collection_runs')
