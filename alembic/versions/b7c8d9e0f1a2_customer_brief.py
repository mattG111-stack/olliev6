"""Store customer-confirmed needs alongside existing search preferences."""
from alembic import op
import sqlalchemy as sa
revision = "b7c8d9e0f1a2"
down_revision = "a1c2e3d4f5b6"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column("users", sa.Column("hunt_brief", sa.Text(), nullable=True))

def downgrade():
    op.drop_column("users", "hunt_brief")
