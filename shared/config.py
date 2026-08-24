"""Settings common to both services.

The credential split is expressed here as *types*, not as discipline. This class holds
only what both services legitimately share. ``AppSettings`` (agent service) adds the
read-only Stripe key and the Anthropic key; ``GatewaySettings`` adds the write-capable
Stripe key and the email adapter configuration. Neither class can read the other's
secrets, because neither declares a field for them.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class CommonSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    #: SQLAlchemy URL. SQLite by default; Postgres is a URL change, not a rewrite.
    database_url: str = "sqlite:////data/collections.db"

    #: HMAC key for approval tokens. Shared by the two services and by nothing else.
    approval_signing_secret: str = ""

    #: How long a pending proposal stays approvable. Stale approvals are worse than none.
    proposal_ttl_hours: int = 72

    #: How long a minted approval token stays valid. Section 5: +15 minutes.
    approval_token_ttl_seconds: int = 900

    #: The single operator identity for the trial. No user registration (section 1).
    operator_id: str = "operator@servicia.ai"

    log_level: str = "INFO"

    def require_signing_secret(self) -> str:
        """Fail loudly at startup rather than mint unverifiable tokens."""
        if len(self.approval_signing_secret.encode("utf-8")) < 32:
            raise RuntimeError(
                "APPROVAL_SIGNING_SECRET must be at least 32 bytes. "
                'Generate one with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return self.approval_signing_secret
