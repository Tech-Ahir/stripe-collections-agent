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


# ----------------------------------------------------------------------------------
# Gateway fixtures
# ----------------------------------------------------------------------------------

GATEWAY_TEST_SECRET = "test-secret-" + "0" * 40


@pytest.fixture()
def gateway_env(tmp_path: Path) -> Iterator[dict[str, str]]:
    """A gateway configured for the captured outbox, on its own database file."""
    from gateway import config as gateway_config
    from shared import db as db_module

    values = {
        "DATABASE_URL": f"sqlite:///{(tmp_path / 'gw.db').as_posix()}",
        "APPROVAL_SIGNING_SECRET": GATEWAY_TEST_SECRET,
        "EMAIL_ADAPTER": "outbox",
        "OUTBOX_DIR": str(tmp_path / "outbox"),
        "ENABLE_STRIPE_INVOICE_SEND": "false",
        "STRIPE_API_KEY_WRITE": "",
        "PROPOSAL_TTL_HOURS": "72",
    }
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    gateway_config.settings.cache_clear()
    db_module.reset_engine_for_tests()
    db_module.init_db()
    try:
        yield values
    finally:
        gateway_config.settings.cache_clear()
        db_module.reset_engine_for_tests()
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


@pytest.fixture()
def gateway_client(gateway_env):
    """A TestClient over the real gateway app, lifespan and all."""
    from fastapi.testclient import TestClient

    from gateway.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture()
def gw_session(gateway_env) -> Iterator:
    """A session on the same database the gateway under test is using."""
    from shared.db import session_scope

    with session_scope() as session:
        yield session
