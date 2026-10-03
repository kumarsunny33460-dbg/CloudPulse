"""Idempotent schema synchronisation.

``db.create_all()`` only creates tables that do not exist yet. When a column is
added to a model, an existing SQLite or PostgreSQL database keeps the old shape
and every query against the new column fails. This module closes that gap by
adding missing columns and indexes in place, so upgrading the application never
requires dropping a database.

For production PostgreSQL deployments, prefer Alembic (``flask db migrate``);
this remains the safety net that keeps SQLite development databases usable.
"""

from __future__ import annotations

import datetime
import logging

from extensions import db
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger("cloudpulse.schema")


def _existing_columns(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}
def _constant_default(column) -> str | None:
    """Return a SQL literal default, or `None` when the default is dynamic.

    Only literal defaults are usable in `ALTER TABLE ... ADD COLUMN`.
    `CURRENT_TIMESTAMP` is rejected by SQLite, and quoting it as a string
    would be wrong on PostgreSQL, so dynamic defaults are backfilled instead.
    """
    default = column.default
    if default is None or not getattr(default, "is_scalar", False):
        return None

    value = default.arg
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"

    return None


def _backfill_literal(column) -> str | None:
    """A literal used to fill pre-existing rows when a column is added."""
    try:
        python_type = column.type.python_type
    except (NotImplementedError, AttributeError):
        return None

    if issubclass(python_type, bool):
        # Not "0". SQLite coerces an integer default into a boolean happily;
        # PostgreSQL refuses it outright:
        #   ERROR: column "is_verified" is of type boolean but default
        #          expression is of type integer
        # and the column is then never created, which breaks every query against
        # the table afterwards. Spelling it as a keyword is accepted by both.
        return "false"
    if issubclass(python_type, (int, float)):
        return "0"
    if issubclass(python_type, str):
        return "''"
    if issubclass(python_type, datetime.datetime):
        # utcnow, not now. The column is read back through UTCDateTime, which
        # treats a naive stored value as UTC, so writing local time here would
        # skew every backfilled row by the host's UTC offset -- every uptime
        # report, SLA calculation and retention prune on those rows would then
        # be wrong by that much.
        return "'" + str(datetime.datetime.now(datetime.UTC).replace(tzinfo=None)) + "'"
    if issubclass(python_type, datetime.date):
        return "'" + str(datetime.datetime.now(datetime.UTC).date()) + "'"

    return None


def _column_ddl(engine: Engine, table_name: str, column, constant: str | None) -> str:
    column_type = column.type.compile(dialect=engine.dialect)
    statement = f'ALTER TABLE {table_name} ADD COLUMN "{column.name}" {column_type}'

    if constant is not None:
        statement += f" DEFAULT {constant}"
        if not column.nullable:
            statement += " NOT NULL"

    return statement


def _backfill(engine: Engine, connection, table_name: str, column) -> None:
    """Fill a newly added nullable column, then restore NOT NULL on PostgreSQL."""
    literal = _backfill_literal(column)
    if literal is None:
        return

    connection.execute(
        text(
            f'UPDATE "{table_name}" SET "{column.name}" = {literal} '
            f'WHERE "{column.name}" IS NULL'
        )
    )

    if engine.dialect.name == "postgresql":
        connection.execute(
            text(
                f'ALTER TABLE "{table_name}" '
                f'ALTER COLUMN "{column.name}" SET NOT NULL'
            )
        )


def _add_missing_columns(engine: Engine) -> list[str]:
    applied: list[str] = []

    for table in db.metadata.sorted_tables:
        table_name = table.name
        present = _existing_columns(engine, table_name)
        if not present:
            continue

        for column in table.columns:
            if column.name in present:
                continue

            try:
                column.type.compile(dialect=engine.dialect)
            except Exception:  # pragma: no cover - exotic types
                logger.warning(
                    "Schema sync | Cannot render type for %s.%s", table_name, column.name
                )
                continue

            constant = _constant_default(column)

            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(_column_ddl(engine, table_name, column, constant))
                    )
                    if constant is None and not column.nullable:
                        _backfill(engine, connection, table_name, column)
                applied.append(f"{table_name}.{column.name}")
                logger.info("Schema sync | Added column %s.%s", table_name, column.name)
            except Exception:  # pragma: no cover - depends on the live database
                logger.exception(
                    "Schema sync | Could not add column %s.%s", table_name, column.name
                )

    return applied


def _create_missing_indexes(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    created: list[str] = []

    for table in db.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue

        for index in table.indexes:
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            f'CREATE INDEX IF NOT EXISTS "{index.name}" '
                            f'ON "{table.name}" ('
                            + ", ".join(f'"{column.name}"' for column in index.columns)
                            + ")"
                        )
                    )
                created.append(index.name)
            except Exception:  # pragma: no cover - depends on the live database
                logger.exception("Schema sync | Could not create index %s", index.name)

    return created


def sync_schema() -> dict:
    """Bring an existing database in line with the current models."""
    engine = db.engine

    db.create_all()
    logger.info("Schema sync | Tables verified")

    added_columns = _add_missing_columns(engine)
    created_indexes = _create_missing_indexes(engine)

    if added_columns or created_indexes:
        logger.info(
            "Schema sync complete | Columns added: %s | Indexes created: %s",
            len(added_columns),
            len(created_indexes),
        )
    else:
        logger.info("Schema sync complete | Database already up to date")

    return {
        "columns_added": added_columns,
        "indexes_created": created_indexes,
    }
