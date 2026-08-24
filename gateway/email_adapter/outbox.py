"""The captured outbox -- the default adapter (brief section 8).

Writes the message to the database and to ``/data/outbox/``. Nothing leaves the machine.
This is the default precisely so that cloning the repository and running the demo cannot
email a real customer.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from gateway.email_adapter.base import DeliveryResult
from shared.clock import now_utc
from shared.db import new_session
from shared.models import OutboxMessage
from shared.types import format_utc

#: Anything outside this set is collapsed to a dash. Dots are excluded deliberately:
#: the extension is ours to add, and a proposal id has no business contributing one.
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]+")


class OutboxAdapter:
    """Captures the letter. Viewable in the UI and on disk."""

    name = "outbox"

    def __init__(
        self,
        directory: str = "/data/outbox",
        *,
        session_factory: Callable[[], Session] = new_session,
    ) -> None:
        self.directory = Path(directory)
        self._session_factory = session_factory

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        meta: dict[str, Any],
    ) -> DeliveryResult:
        proposal_id = str(meta.get("proposal_id", "unknown"))
        file_path = self._write_file(proposal_id=proposal_id, to=to, subject=subject, body=body)

        record = OutboxMessage(
            proposal_id=proposal_id,
            to_email=to,
            subject=subject,
            body=body,
            meta=meta,
            adapter=self.name,
            file_path=str(file_path) if file_path else None,
        )
        # Its own transaction: the letter is captured even if a later step fails, which is
        # the honest record of what this process did.
        session = self._session_factory()
        try:
            session.add(record)
            session.commit()
            message_id = record.id
        finally:
            session.close()

        return DeliveryResult(
            adapter=self.name,
            accepted=True,
            provider_message_id=message_id,
            detail={
                "captured": True,
                "file_path": str(file_path) if file_path else None,
                "note": "Captured locally. Nothing left this machine.",
            },
        )

    def _write_file(self, *, proposal_id: str, to: str, subject: str, body: str) -> Path | None:
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = format_utc(now_utc()).replace(":", "-")
            name = f"{stamp}_{_UNSAFE.sub('-', proposal_id)[:40]}.eml"
            path = self.directory / name
            path.write_text(
                "\n".join(
                    [
                        f"To: {to}",
                        f"Subject: {subject}",
                        f"X-Proposal-Id: {proposal_id}",
                        "X-Adapter: outbox",
                        "",
                        body,
                    ]
                ),
                encoding="utf-8",
            )
            return path
        except OSError:
            # The database row is the authoritative capture; the file is a convenience for
            # a reviewer with a shell. A read-only volume must not fail an execution.
            return None
