"""The audit chain (CLAUDE.md rule 7, brief section 3).

`audit_events` is append-only and hash-chained. These tests assert the four ways the
chain can be broken are all detected, and that the codebase itself contains no way to
break it -- the last two tests are structural guards against a future contributor, not
behavioural tests.
"""

from __future__ import annotations

import inspect

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from shared import audit
from shared.audit import ChainStatus, append, verify_chain
from shared.db import run_in_transaction, session_scope
from shared.hashing import GENESIS_HASH
from shared.models import AuditEvent


def _append_three(session) -> list[AuditEvent]:
    return [
        append(
            session,
            actor="system",
            event=audit.SYSTEM_STARTED,
            subject_type="system",
            subject_id="app",
            detail={"version": "0.1.0"},
        ),
        append(
            session,
            actor="agent",
            event=audit.PROPOSAL_CREATED,
            subject_type="proposal",
            subject_id="p-1",
            detail={"invoice": "in_1", "tone": "friendly"},
        ),
        append(
            session,
            actor="operator",
            event=audit.APPROVAL_GRANTED,
            subject_type="proposal",
            subject_id="p-1",
            detail={"actor": "operator@servicia.ai"},
        ),
    ]


# ----------------------------------------------------------------------------------
# Shape of the chain
# ----------------------------------------------------------------------------------


def test_empty_chain_is_intact(session):
    status = verify_chain(session)
    assert status == ChainStatus(intact=True, length=0, head_hash=None)


def test_first_row_links_to_genesis(session):
    first = append(
        session,
        actor="system",
        event=audit.SYSTEM_STARTED,
        subject_type="system",
        subject_id="app",
    )
    assert first.id == 1
    assert first.prev_hash == GENESIS_HASH
    assert first.hash.startswith("sha256:")


def test_each_row_links_to_its_predecessor(session):
    rows = _append_three(session)
    assert [r.id for r in rows] == [1, 2, 3]
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash


def test_healthy_chain_verifies_intact(session):
    rows = _append_three(session)
    status = verify_chain(session)
    assert status.intact is True
    assert status.length == 3
    assert status.head_hash == rows[-1].hash
    assert status.broken_at_id is None


def test_chain_survives_being_reread_from_a_second_connection(session):
    """The gateway verifies a chain the app wrote. Different process, same answer."""
    _append_three(session)
    session.commit()
    with session_scope() as other:
        assert verify_chain(other).intact is True
        assert verify_chain(other).length == 3


# ----------------------------------------------------------------------------------
# The four ways to break it
# ----------------------------------------------------------------------------------


def test_editing_a_rows_content_is_detected(session):
    """Tampering with a detail field breaks that row's own hash."""
    _append_three(session)
    session.commit()
    session.execute(
        text("UPDATE audit_events SET detail = :d WHERE id = 2"),
        {"d": '{"invoice":"in_STOLEN","tone":"final"}'},
    )
    session.commit()

    status = verify_chain(session)
    assert status.intact is False
    assert status.broken_at_id == 2
    assert "does not match its stored hash" in status.reason


def test_deleting_a_row_is_detected(session):
    """Removing a row leaves a gap in the id sequence."""
    _append_three(session)
    session.commit()
    session.execute(text("DELETE FROM audit_events WHERE id = 2"))
    session.commit()

    status = verify_chain(session)
    assert status.intact is False
    assert status.broken_at_id == 3
    assert "id sequence broken" in status.reason


def test_relinking_a_row_onto_an_existing_predecessor_is_refused_by_the_database(session):
    """Re-pointing prev_hash at a row that already has a successor cannot even be stored.

    Two rows claiming the same predecessor is a fork, and UNIQUE(prev_hash) rejects it at
    the storage layer. `verify_chain` never has to catch this one.
    """
    rows = _append_three(session)
    session.commit()
    with pytest.raises(IntegrityError):
        session.execute(
            text("UPDATE audit_events SET prev_hash = :h WHERE id = 3"),
            {"h": rows[0].hash},  # row 2 already claims this predecessor
        )
        session.commit()
    session.rollback()


def test_relinking_a_row_to_a_forged_hash_is_detected(session):
    """A unique-but-wrong prev_hash gets past the constraint and is caught on verify."""
    _append_three(session)
    session.commit()
    session.execute(
        text("UPDATE audit_events SET prev_hash = :h WHERE id = 3"),
        {"h": "sha256:" + "c" * 64},
    )
    session.commit()

    status = verify_chain(session)
    assert status.intact is False
    assert status.broken_at_id == 3
    assert "prev_hash does not match" in status.reason


def test_replacing_the_genesis_row_is_detected(session):
    """A forged first row cannot claim to be the start of the chain."""
    _append_three(session)
    session.commit()
    session.execute(
        text("UPDATE audit_events SET prev_hash = :h WHERE id = 1"),
        {"h": "sha256:" + "f" * 64},
    )
    session.commit()

    status = verify_chain(session)
    assert status.intact is False
    assert status.broken_at_id == 1


# ----------------------------------------------------------------------------------
# No forked chain, ever
# ----------------------------------------------------------------------------------


def test_two_writers_cannot_fork_the_chain(session):
    """prev_hash is UNIQUE, so a second append from the same tail is refused.

    This is the guarantee that makes the chain safe across two processes: not "unlikely
    to fork", but "cannot fork".
    """
    first = append(
        session,
        actor="system",
        event=audit.SYSTEM_STARTED,
        subject_type="system",
        subject_id="app",
    )
    session.commit()

    forged = AuditEvent(
        id=99,
        actor="gateway",
        event="action.execution.started",
        subject_type="proposal",
        subject_id="p-1",
        detail={},
        prev_hash=first.prev_hash,  # same predecessor as row 1 -> a fork
        hash="sha256:" + "a" * 64,
    )
    session.add(forged)
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_run_in_transaction_retries_a_losing_appender(db_path):
    """A writer that loses the race retries the whole unit of work and succeeds."""
    attempts = {"n": 0}

    def work(session):
        attempts["n"] += 1
        if attempts["n"] == 1:
            # Simulate the other process winning: commit a row on a separate session
            # after we have already read the tail, then collide with it.
            append(
                session,
                actor="system",
                event=audit.SYSTEM_STARTED,
                subject_type="system",
                subject_id="first",
            )
            session.flush()
            with session_scope() as other:
                other.add(
                    AuditEvent(
                        id=50,
                        actor="gateway",
                        event="action.refused",
                        subject_type="proposal",
                        subject_id="p-x",
                        detail={},
                        prev_hash=GENESIS_HASH,  # collides with our row 1
                        hash="sha256:" + "b" * 64,
                    )
                )
        return append(
            session,
            actor="operator",
            event=audit.APPROVAL_GRANTED,
            subject_type="proposal",
            subject_id="p-1",
        )

    result = run_in_transaction(work)
    assert attempts["n"] == 2, "the losing writer should have retried exactly once"
    assert result.hash.startswith("sha256:")


# ----------------------------------------------------------------------------------
# Structural guards -- rule 7 against a future contributor
# ----------------------------------------------------------------------------------


def test_audit_module_exposes_no_mutation_api():
    """There is no update and no delete function. Appending is the only verb."""
    public = {
        name
        for name, value in vars(audit).items()
        if not name.startswith("_") and inspect.isfunction(value)
    }
    forbidden = {n for n in public if any(v in n for v in ("update", "delete", "edit", "remove"))}
    assert forbidden == set(), f"audit.py must expose no mutation API, found: {forbidden}"


def _executable_code(source: str) -> str:
    """The source with docstrings, string literals and comments removed.

    Scanning raw text would flag CLAUDE.md-style prose such as "never UPDATE
    audit_events". Only real code counts.
    """
    import io
    import tokenize

    skip = {tokenize.STRING, tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE}
    for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END"):
        if hasattr(tokenize, name):
            skip.add(getattr(tokenize, name))

    pieces: list[str] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in skip:
            continue
        pieces.append(token.string)
    return " ".join(pieces).lower()


def test_no_update_or_delete_of_audit_events_anywhere_in_the_codebase():
    """Scans the shipped source for any mutation of audit_events. There must be none.

    This is the guard that stops a future contributor from "fixing a status" with an
    UPDATE. The correcting-event pattern is the only remedy the design allows.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    patterns = (
        "update ( auditevent",
        "delete ( auditevent",
        "update audit_events",
        "delete from audit_events",
        "auditevent ) . delete",
        "auditevent ) . update",
    )
    for directory in ("app", "gateway", "shared", "scripts"):
        base = root / directory
        if not base.exists():
            continue
        for file in base.rglob("*.py"):
            code = _executable_code(file.read_text(encoding="utf-8"))
            for pattern in patterns:
                if pattern in code:
                    offenders.append(f"{file.relative_to(root)}: {pattern}")
    assert offenders == [], f"audit_events must be append-only, found: {offenders}"
