"""Schema management (shared/schema.py).

`create_all` creates tables but never alters them. This suite exists because that gap bit
during the build: `token_nonces.idempotency_key` was added in Phase 2, and a container
running against a Phase 1 volume failed at runtime with "no such column" rather than at
startup with something a reader could act on.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from shared.db import get_engine, session_scope
from shared.models import AuditEvent
from shared.schema import SchemaDrift, ensure_schema, init_schema_and_audit


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(get_engine()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {index["name"] for index in inspect(get_engine()).get_indexes(table)}


def _drop_column(table: str, column: str) -> None:
    """Simulate a database created before `column` existed.

    SQLite refuses to drop a column an index still references, so the index goes first --
    which is precisely the state an older database would have been in.
    """
    engine = get_engine()
    with engine.begin() as connection:
        for index in inspect(engine).get_indexes(table):
            if column in index["column_names"]:
                connection.execute(text(f'DROP INDEX "{index["name"]}"'))
        connection.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))


def test_a_fresh_database_needs_no_migration(db_path):
    """The clean-machine path (acceptance criterion 9) applies nothing."""
    assert ensure_schema() == []


def test_a_missing_nullable_column_is_added(db_path):
    _drop_column("approvals", "edited_body")
    assert "edited_body" not in _columns("approvals")

    applied = ensure_schema()

    assert [change.column for change in applied] == ["edited_body"]
    assert "edited_body" in _columns("approvals")


def test_a_missing_not_null_column_is_added_with_a_refusing_backfill(db_path):
    """This is the exact failure that prompted the module.

    A backfilled `idempotency_key` of '' can never equal a real request's key, so an old
    nonce reads as consumed by some other request and check 3 refuses. Failing towards a
    refusal is the only acceptable direction for this column.
    """
    _drop_column("token_nonces", "idempotency_key")
    assert "ix_token_nonces_idempotency_key" not in _indexes("token_nonces")

    applied = ensure_schema()

    assert len(applied) == 1
    change = applied[0]
    assert (change.table, change.column) == ("token_nonces", "idempotency_key")
    assert change.backfill == "''"
    assert "NOT NULL DEFAULT ''" in change.ddl
    assert "idempotency_key" in _columns("token_nonces")
    assert "ix_token_nonces_idempotency_key" in _indexes("token_nonces"), (
        "the column's index must come back with it, or a constraint the code relies on "
        "would silently not exist"
    )


def test_an_integer_column_backfills_with_zero(db_path):
    _drop_column("proposals", "days_overdue")

    applied = ensure_schema()

    assert applied[0].backfill == "0"


def test_migration_is_idempotent(db_path):
    _drop_column("approvals", "note")

    assert len(ensure_schema()) == 1
    assert ensure_schema() == [], "a second run must find nothing to do"


def test_a_missing_table_is_created(db_path):
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE outbox_messages"))

    ensure_schema()

    assert "outbox_messages" in inspect(get_engine()).get_table_names()


def test_a_dropped_index_is_recreated(db_path):
    """An index is part of the schema, not an optimisation detail.

    `ux_proposals_one_pending_per_invoice` is what enforces "one pending proposal per
    invoice" in the store rather than in the prompt. Losing it would not break any query.
    """
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text('DROP INDEX "ux_proposals_one_pending_per_invoice"'))
    assert "ux_proposals_one_pending_per_invoice" not in _indexes("proposals")

    ensure_schema()

    assert "ux_proposals_one_pending_per_invoice" in _indexes("proposals")


def test_an_extra_column_is_reported_but_not_fatal(db_path, caplog):
    """An orphan column from an older version is inert. Say so; do not fail."""
    engine = get_engine()
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE runs ADD COLUMN legacy_thing TEXT"))

    with caplog.at_level("WARNING"):
        assert ensure_schema() == []

    assert any("legacy_thing" in record.getMessage() for record in caplog.records)


def test_a_new_primary_key_column_refuses_rather_than_guessing(db_path, monkeypatch):
    """SQLite cannot add a primary key column. Fail loudly with instructions."""
    from sqlalchemy import inspect as sa_inspect

    real = sa_inspect(get_engine())

    class Pretend:
        """The real inspector, but with token_nonces.nonce hidden.

        SQLite cannot actually get into this state through DDL -- it refuses to drop a
        primary key column -- so the only way to exercise the guard is to describe the
        state to it. Everything else delegates to the real inspector.
        """

        def get_columns(self, table, **kwargs):
            columns = real.get_columns(table, **kwargs)
            if table == "token_nonces":
                return [c for c in columns if c["name"] != "nonce"]
            return columns

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr("shared.schema.inspect", lambda _engine: Pretend())

    with pytest.raises(SchemaDrift, match="PRIMARY KEY"):
        ensure_schema()


# ----------------------------------------------------------------------------------
# A migration is a fact about the system, so it is in the record
# ----------------------------------------------------------------------------------


def test_a_migration_is_appended_to_the_audit_log(db_path):
    _drop_column("approvals", "note")

    init_schema_and_audit(service="app", version="0.1.0")

    with session_scope() as session:
        events = [row.event for row in session.query(AuditEvent).order_by(AuditEvent.id)]
    assert "system.schema.migrated" in events
    assert events[-1] == "system.started"


def test_startup_with_no_migration_records_only_the_start(db_path):
    init_schema_and_audit(service="gateway", version="0.1.0")

    with session_scope() as session:
        events = [row.event for row in session.query(AuditEvent).order_by(AuditEvent.id)]
    assert events == ["system.started"]
