from alembic import context
from app.db import Base
config = context.config
connection = config.attributes["connection"]
context.configure(connection=connection, target_metadata=Base.metadata)
with context.begin_transaction():
    context.run_migrations()

