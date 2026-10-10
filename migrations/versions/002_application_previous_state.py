"""Remember the state restored when an applied mark is undone."""
from alembic import op
import sqlalchemy as sa

revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None


def upgrade():
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns("opportunity_status")}
    if "previous_state" not in columns:
        op.add_column("opportunity_status", sa.Column("previous_state", sa.String(), nullable=True))


def downgrade():
    raise RuntimeError("Restore a backup rather than discarding application history.")
