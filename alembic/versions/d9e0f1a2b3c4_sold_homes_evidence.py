"""Keep Homes reference estimates separate on sold records."""
from alembic import op
import sqlalchemy as sa

revision = 'd9e0f1a2b3c4'
down_revision = 'c8d9e0f1a2b3'
branch_labels = None
depends_on = None


def upgrade():
    for name in ('homes_valuation','homes_valuation_low','homes_valuation_high'):
        op.add_column('properties_sold',sa.Column(name,sa.Float(),nullable=True))
    op.add_column('properties_sold',sa.Column('homes_url',sa.String(500),nullable=True))


def downgrade():
    for name in ('homes_url','homes_valuation_high','homes_valuation_low','homes_valuation'):
        op.drop_column('properties_sold',name)
