from alembic import context
from app.core import engine

with engine().begin() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
