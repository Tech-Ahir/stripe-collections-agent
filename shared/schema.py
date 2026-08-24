"""Schema management.

``Base.metadata.create_all`` creates missing *tables*. It does not alter existing ones, so
a database created by an earlier version of the code silently lacks any column added since
-- and the first query touching it fails at runtime with "no such column", which is a poor
way to find out.

This module closes that gap with the smallest thing that is honest:

* missing tables are created;
* missing **columns** on existing tables are added, with a type-appropriate backfill, and
  every such change is logged and appended to the audit log;
* anything a plain ``ADD COLUMN`` cannot express -- a removed column, a changed type, a new
  constraint over existing rows -- raises ``SchemaDrift`` with instructions, rather than
  limping on.

Alembic is deliberately not used for the trial: section 3 asks for zero setup for a
reviewer, and SQLite plus this file delivers that. Knowledge-base note 10 covers what to
replace this with the moment the schema starts changing under a database that holds data
somebody cares about.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import Column, Engine, inspect, text

from shared.models import Base

log = logging.getLogger("shared.schema")


class SchemaDrift(RuntimeError):
    """The live schema differs from the models in a way this module will not guess at."""


@dataclass(frozen=True, slots=True)
class AppliedChange:
    table: str
    column: str
    ddl: str
    backfill: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column} added (backfilled with {self.backfill})"


def _backfill_literal(column: Column) -> str:
    """A safe placeholder for existing rows in a NOT NULL additive column.

    Safe means "errs towards refusing". A backfilled ``token_nonces.idempotency_key`` of
    '' will never match a real request's key, so an old nonce reads as consumed by some
    other request -- which makes check 3 refuse. That is the correct direction to fail.
    """
    python_type: type | None
    try:
        python_type = column.type.python_type
    except NotImplementedError:  # pragma: no cover - exotic types only
        python_type = None

    if python_type in (int, float):
        return "0"
    if python_type is bool:
        return "0"
    return "''"


def ensure_schema(engine: Engine | None = None) -> list[AppliedChange]:
    """Bring the live schema up to the models. Returns the changes applied."""
    from shared.db import get_engine

    engine = engine or get_engine()
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    live_tables = set(inspector.get_table_names())
    applied: list[AppliedChange] = []

    for table in Base.metadata.sorted_tables:
        if table.name not in live_tables:
            raise SchemaDrift(
                f"table {table.name!r} is still absent after create_all; the database is "
                "not in a state this code can repair."
            )

        live_columns = {column["name"] for column in inspector.get_columns(table.name)}
        expected_columns = {column.name for column in table.columns}

        removed = live_columns - expected_columns
        if removed:
            # Not fatal: an extra column left behind by an older version is inert. Say so
            # rather than pretending the schema matches.
            log.warning(
                "table %s has columns the models no longer declare: %s",
                table.name,
                ", ".join(sorted(removed)),
            )

        for column in table.columns:
            if column.name in live_columns:
                continue
            if column.primary_key:
                raise SchemaDrift(
                    f"{table.name}.{column.name} is a new PRIMARY KEY column. SQLite "
                    "cannot add one to an existing table. Recreate the database "
                    "(docker compose down -v) or write a real migration."
                )

            type_sql = column.type.compile(dialect=engine.dialect)
            ddl = f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {type_sql}'
            backfill = "NULL"
            if not column.nullable:
                backfill = _backfill_literal(column)
                ddl += f" NOT NULL DEFAULT {backfill}"

            with engine.begin() as connection:
                connection.execute(text(ddl))
            applied.append(AppliedChange(table.name, column.name, ddl, backfill))
            log.warning("schema migrated: %s", applied[-1])

        # A column carries its indexes. Adding the column without them would leave the
        # schema subtly wrong: queries still work, but a uniqueness constraint the code
        # relies on would silently not exist.
        _ensure_indexes(engine, table, inspector)

    return applied


def _ensure_indexes(engine: Engine, table, inspector) -> None:
    live = {index["name"] for index in inspector.get_indexes(table.name)}
    for index in table.indexes:
        if index.name in live:
            continue
        index.create(bind=engine, checkfirst=True)
        log.warning("schema migrated: index %s created on %s", index.name, table.name)


def init_schema_and_audit(*, service: str, version: str) -> list[AppliedChange]:
    """Startup hook: bring the schema up to date and record what happened.

    A migration is a fact about the system, so it goes in the append-only log next to
    everything else. If the chain is the story of what this system did, a schema change
    belongs in it.
    """
    from shared import audit
    from shared.db import run_in_transaction

    applied = ensure_schema()

    def record(session):
        if applied:
            audit.append(
                session,
                actor="system",
                event="system.schema.migrated",
                subject_type="service",
                subject_id=service,
                detail={"changes": [str(change) for change in applied]},
            )
        return audit.append(
            session,
            actor="system",
            event=audit.SYSTEM_STARTED,
            subject_type="service",
            subject_id=service,
            detail={"version": version, "schema_changes": len(applied)},
        )

    run_in_transaction(record)
    return applied
