"""Shared test fixtures.

Every test gets its own SQLite file. An in-memory database is deliberately not used: the
real system has two processes sharing one file, and several tests open a second connection
to simulate that.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def db_path(tmp_path: Path) -> Iterator[Path]:
    """Point the engine at a fresh database file for the duration of one test."""
    from shared import db as db_module

    path = tmp_path / "collections.db"
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{path.as_posix()}"
    db_module.reset_engine_for_tests()
    db_module.init_db()
    try:
        yield path
    finally:
        db_module.reset_engine_for_tests()
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous


@pytest.fixture()
def session(db_path: Path) -> Iterator:
    from shared.db import session_scope

    with session_scope() as s:
        yield s


@pytest.fixture()
def signing_secret() -> Iterator[str]:
    secret = "test-secret-" + "0" * 40
    previous = os.environ.get("APPROVAL_SIGNING_SECRET")
    os.environ["APPROVAL_SIGNING_SECRET"] = secret
    try:
        yield secret
    finally:
        if previous is None:
            os.environ.pop("APPROVAL_SIGNING_SECRET", None)
        else:
            os.environ["APPROVAL_SIGNING_SECRET"] = previous
