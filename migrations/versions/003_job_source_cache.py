"""Persistent public-source cache and bounded request accounting."""
from alembic import op
from app.db import JobSourceCache, JobSourceUsage

revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None


def upgrade():
    JobSourceCache.__table__.create(op.get_bind(), checkfirst=True)
    JobSourceUsage.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a backup rather than discarding source request accounting.")
