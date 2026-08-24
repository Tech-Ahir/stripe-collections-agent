"""Engine and session management.

Both containers open the same SQLite file on the shared ``/data`` volume, so this module
sets the pragmas that make concurrent access safe:

* ``journal_mode=WAL``   -- readers do not block the writer, which matters because the
  SSE transcript endpoint tails ``run_steps`` continuously while a run is writing to it.
* ``busy_timeout=5000``  -- a writer waits for a lock instead of failing immediately.
* ``foreign_keys=ON``    -- SQLite leaves these off by default.

The audit chain's cross-process safety does *not* rest on isolation levels; it rests on
the UNIQUE constraint on ``audit_events.prev_hash`` plus a retry. See ``shared/audit.py``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from shared.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None

DEFAULT_URL = "sqlite:////data/collections.db"

T = TypeVar("T")


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def _apply_sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def get_engine() -> Engine:
    global _engine, _session_factory
    if _engine is None:
        url = database_url()
        connect_args: dict[str, object] = {}
        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            path = url.split("sqlite:///")[-1]
            if path and path not in (":memory:",):
                Path(path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            event.listen(_engine, "connect", _apply_sqlite_pragmas)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


def new_session() -> Session:
    return get_session_factory()()


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transactional scope. Commits on success, rolls back on any exception."""
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    """Create the schema if it is absent. Idempotent, safe to call on every startup.

    Alembic is deliberately not used for the trial: SQLite plus ``create_all`` is zero
    setup for a reviewer, which section 3 asks for explicitly. Knowledge-base note 08
    records the decision and note 10 explains how to add Alembic when the schema starts
    to change under a live database.
    """
    Base.metadata.create_all(get_engine())


def reset_engine_for_tests() -> None:
    """Drop the cached engine so a test can repoint ``DATABASE_URL``."""
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None


# --------------------------------------------------------------------------------------
# Write conflicts
# --------------------------------------------------------------------------------------

_RETRYABLE_SQLITE = ("database is locked", "database table is locked")


def is_retryable_write_conflict(exc: BaseException) -> bool:
    """True for the two conflicts that a retry actually fixes.

    * ``IntegrityError`` on ``audit_events.prev_hash`` -- another process appended to the
      chain between our tail read and our INSERT. See ``shared/audit.py``.
    * SQLite's "database is locked" -- a writer gave up after ``busy_timeout``.

    Every other IntegrityError is a real constraint violation (a duplicate pending
    proposal, a reused idempotency key) and must surface to the caller unchanged.
    """
    from sqlalchemy.exc import IntegrityError, OperationalError

    if isinstance(exc, IntegrityError):
        message = str(getattr(exc, "orig", exc))
        return "prev_hash" in message or "audit_events.hash" in message
    if isinstance(exc, OperationalError):
        message = str(getattr(exc, "orig", exc)).lower()
        return any(fragment in message for fragment in _RETRYABLE_SQLITE)
    return False


def run_in_transaction(work: Callable[[Session], T], *, max_attempts: int = 6) -> T:
    """Run ``work`` in one transaction, retrying the whole unit on a write conflict.

    The audit chain guarantees no fork by making ``prev_hash`` UNIQUE; that guarantee is
    only usable if a losing writer retries. This is where that happens. The unit of work
    is re-run from the start with a fresh session, so it re-reads the new chain tail.
    """
    import random
    import time

    last: BaseException | None = None
    for attempt in range(max_attempts):
        try:
            with session_scope() as session:
                return work(session)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless retryable
            if not is_retryable_write_conflict(exc):
                raise
            last = exc
            time.sleep((0.02 * (2**attempt)) + random.uniform(0, 0.02))
    raise RuntimeError(f"write conflict persisted after {max_attempts} attempts") from last
