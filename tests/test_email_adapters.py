"""The email adapters (brief section 8).

The default must capture rather than deliver, because a reviewer who clones this repository
and runs the demo must not be able to email a real customer by accident.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import GatewaySettings
from gateway.email_adapter.base import DeliveryError, DeliveryResult, EmailAdapter
from gateway.email_adapter.factory import build_adapter
from gateway.email_adapter.outbox import OutboxAdapter
from shared.models import OutboxMessage

# ----------------------------------------------------------------------------------
# Selection
# ----------------------------------------------------------------------------------


def test_the_default_adapter_is_the_captured_outbox():
    adapter = build_adapter(GatewaySettings(email_adapter="outbox", outbox_dir="/tmp/x"))
    assert adapter.name == "outbox"
    assert isinstance(adapter, EmailAdapter)


def test_an_unknown_adapter_name_is_rejected_by_the_settings_type():
    """A typo in EMAIL_ADAPTER must not silently change what happens to a letter.

    The Literal annotation catches it at startup, before any request is served.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        GatewaySettings(email_adapter="outbx")


def test_the_factory_also_refuses_an_unknown_adapter():
    """Defence in depth: even if a value bypasses validation, selection still refuses."""
    smuggled = GatewaySettings.model_construct(email_adapter="outbx")
    with pytest.raises(ValueError, match="unknown EMAIL_ADAPTER"):
        build_adapter(smuggled)


def test_the_real_delivery_adapter_refuses_to_start_without_a_key():
    with pytest.raises(ValueError, match="RESEND_API_KEY"):
        build_adapter(GatewaySettings(email_adapter="resend", resend_api_key=""))


def test_selecting_smtp_points_at_the_configured_host():
    adapter = build_adapter(
        GatewaySettings(email_adapter="smtp", smtp_host="mailpit", smtp_port=1025)
    )
    assert adapter.name == "smtp"
    assert adapter.host == "mailpit"
    assert adapter.port == 1025


# ----------------------------------------------------------------------------------
# The outbox captures to both the database and the disk
# ----------------------------------------------------------------------------------


def test_the_outbox_writes_a_database_row(db_path, tmp_path: Path):
    from shared.db import new_session, session_scope

    adapter = OutboxAdapter(str(tmp_path / "outbox"), session_factory=new_session)
    result = adapter.send(
        to="ap@acme.test",
        subject="Invoice INV-1001 is 9 days past due",
        body="Dear Acme,\n\nPlease pay $250.00.\n",
        meta={"proposal_id": "p-1", "invoice_id": "in_1"},
    )

    assert isinstance(result, DeliveryResult)
    assert result.accepted is True
    assert result.adapter == "outbox"

    with session_scope() as session:
        row = session.query(OutboxMessage).one()
        assert row.to_email == "ap@acme.test"
        assert "$250.00" in row.body
        assert row.adapter == "outbox"
        assert row.meta["invoice_id"] == "in_1"


def test_the_outbox_writes_a_file_that_a_reviewer_can_read(db_path, tmp_path: Path):
    directory = tmp_path / "outbox"
    adapter = OutboxAdapter(str(directory))
    result = adapter.send(
        to="ap@acme.test",
        subject="Subject line",
        body="Body text",
        meta={"proposal_id": "p-2"},
    )

    files = list(directory.glob("*.eml"))
    assert len(files) == 1
    content = files[0].read_text(encoding="utf-8")
    assert "To: ap@acme.test" in content
    assert "Subject: Subject line" in content
    assert "Body text" in content
    assert result.detail["file_path"] == str(files[0])


def test_the_outbox_still_captures_when_the_directory_cannot_be_written(
    db_path, tmp_path: Path, monkeypatch
):
    """The database row is the authoritative capture. A read-only volume is not a failure."""

    def explode(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(Path, "mkdir", explode)
    adapter = OutboxAdapter(str(tmp_path / "nope"))

    result = adapter.send(to="a@b.test", subject="s", body="b", meta={"proposal_id": "p-3"})

    assert result.accepted is True
    assert result.detail["file_path"] is None
    from shared.db import session_scope

    with session_scope() as session:
        assert session.query(OutboxMessage).count() == 1


def test_a_proposal_id_with_path_characters_cannot_escape_the_outbox_directory(
    db_path, tmp_path: Path
):
    directory = tmp_path / "outbox"
    adapter = OutboxAdapter(str(directory))

    adapter.send(
        to="a@b.test",
        subject="s",
        body="b",
        meta={"proposal_id": "../../etc/passwd"},
    )

    written = list(directory.glob("*.eml"))
    assert len(written) == 1
    # The property that matters: the resolved path is inside the outbox directory.
    assert written[0].resolve().parent == directory.resolve()
    assert ".." not in written[0].name
    assert "/" not in written[0].name and "\\" not in written[0].name


# ----------------------------------------------------------------------------------
# Failure surfaces as a delivery error, not as a silent success
# ----------------------------------------------------------------------------------


def test_smtp_delivery_failure_raises_delivery_error(monkeypatch):
    from gateway.email_adapter.smtp import SmtpAdapter

    class Boom:
        def __init__(self, *_a, **_k):
            raise OSError("connection refused")

    monkeypatch.setattr("smtplib.SMTP", Boom)
    adapter = SmtpAdapter(host="nowhere", port=1025)

    with pytest.raises(DeliveryError) as raised:
        adapter.send(to="a@b.test", subject="s", body="b", meta={})
    assert raised.value.adapter == "smtp"
