from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from app.config import settings
from app.db import engine
from app.models import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

#: The FTS5 search index and its shadow tables are created by app/search.py,
#: not by the ORM. Alembic must not see them: autogenerate treats them as
#: schema drift and emits drop_table() for each, which silently corrupts the
#: index (it dropped title_fts_idx/_config/_docsize and left the rest, after
#: which the virtual table could not even be dropped). The index is derived
#: data and is rebuilt by `dramadex reindex`.
_IGNORED_PREFIXES = ("title_fts", "title_search")


#: `title.merged_into_id` declares a self-referential FK that the deduplication
#: migration creates on Postgres and deliberately SKIPS on SQLite -- adding a
#: constraint there forces a table rebuild, and rebuilding `title` under
#: `PRAGMA foreign_keys=ON` fires ON DELETE CASCADE on nine child tables and
#: wipes the catalogue (see that migration's comment).
#:
#: So on SQLite the model and the schema differ by exactly this one constraint,
#: for a good reason. Left unsuppressed, `alembic check` reports it as drift on
#: every run, and permanent expected noise is how real drift stops being noticed.
_SQLITE_OMITTED_FKS = {("title", "merged_into_id")}


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    if type_ == "table" and name and name.startswith(_IGNORED_PREFIXES):
        return False
    if type_ == "foreign_key_constraint" and engine.dialect.name == "sqlite":
        columns = tuple(c.name for c in obj.columns)
        if (obj.table.name, *columns) in {
            (table, column) for table, column in _SQLITE_OMITTED_FKS
        }:
            return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=settings.db_url,
        target_metadata=target_metadata,
        include_object=include_object,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            # SQLite cannot ALTER most things in place; batch mode rewrites the
            # table instead. Harmless on Postgres, essential here.
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
