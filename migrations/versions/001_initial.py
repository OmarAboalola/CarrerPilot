"""Initial one-owner demo data schema."""
from alembic import op
from app.db import Base
revision = "001"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    Base.metadata.create_all(bind=op.get_bind())

def downgrade():
    # Destructive downgrade intentionally requires explicit database backup/operator handling.
    raise RuntimeError("Restore a backup instead of dropping candidate data.")

