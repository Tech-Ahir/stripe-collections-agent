"""The approval token codec (brief section 5).

`shared/approval_token.py` is ~60 lines and everything downstream trusts it. These tests
exist because "the signature verifies" has to mean exactly one thing.
"""

from __future__ import annotations

import base64
import json

import pytest

from shared.approval_token import (
    ApprovalTokenClaims,
    InvalidSignature,
    MalformedToken,
    decode,
    mint,
)
from shared.clock import now_utc

SECRET = "a" * 64
OTHER_SECRET = "b" * 64


def _claims(**overrides) -> ApprovalTokenClaims:
    now = int(now_utc().timestamp())
    values = {
        "proposal_id": "11111111-1111-1111-1111-111111111111",
        "payload_hash": "sha256:" + "c" * 64,
        "action_type": "send_collection_letter",
        "approver": "operator@servicia.ai",
        "nonce": "22222222-2222-2222-2222-222222222222",
        "iat": now,
        "exp": now + 900,
    }
    values.update(overrides)
    return ApprovalTokenClaims(**values)


def _segments(token: str) -> tuple[str, str]:
    head, sig = token.split(".")
    return head, sig


def _reencode(claims: dict, signature: str) -> str:
    head = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return head + "." + signature


# ----------------------------------------------------------------------------------
# Round trip
# ----------------------------------------------------------------------------------


def test_a_minted_token_decodes_to_the_same_claims():
    claims = _claims()
    assert decode(mint(claims, SECRET), SECRET) == claims


def test_the_token_is_two_base64url_segments():
    token = mint(_claims(), SECRET)
    head, signature = _segments(token)
    assert "=" not in token, "padding is stripped so the token is header-safe"
    assert json.loads(base64.urlsafe_b64decode(head + "==="))["approver"] == "operator@servicia.ai"
    assert len(signature) >= 40


def test_minting_is_deterministic_for_identical_claims():
    """Same claims, same secret, same token -- so nothing hidden varies per call."""
    claims = _claims()
    assert mint(claims, SECRET) == mint(claims, SECRET)


# ----------------------------------------------------------------------------------
# The signature actually covers every claim
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("proposal_id", "99999999-9999-9999-9999-999999999999"),
        ("payload_hash", "sha256:" + "0" * 64),
        ("action_type", "something_else"),
        ("approver", "attacker@evil.test"),
        ("nonce", "33333333-3333-3333-3333-333333333333"),
        ("exp", 99999999999),
        ("iat", 0),
    ],
)
def test_changing_any_single_claim_invalidates_the_signature(field, value):
    """Every field is covered. Not just the interesting-looking ones."""
    token = mint(_claims(), SECRET)
    head, signature = _segments(token)
    claims = json.loads(base64.urlsafe_b64decode(head + "==="))
    claims[field] = value

    with pytest.raises(InvalidSignature):
        decode(_reencode(claims, signature), SECRET)


def test_a_token_minted_with_a_different_secret_does_not_verify():
    with pytest.raises(InvalidSignature):
        decode(mint(_claims(), OTHER_SECRET), SECRET)


def test_swapping_signatures_between_two_tokens_fails():
    first = mint(_claims(proposal_id="aaaa"), SECRET)
    second = mint(_claims(proposal_id="bbbb"), SECRET)
    head, _ = _segments(first)
    _, other_signature = _segments(second)

    with pytest.raises(InvalidSignature):
        decode(head + "." + other_signature, SECRET)


# ----------------------------------------------------------------------------------
# There is no algorithm to confuse
# ----------------------------------------------------------------------------------


def test_there_is_no_alg_field_to_downgrade():
    """A hand-rolled token has no `alg` header, so `alg: none` is not a thing here.

    This is the concrete reason a JWT library was not used: the most common JWT
    vulnerability is a field this format does not have.
    """
    head, _ = _segments(mint(_claims(), SECRET))
    claims = json.loads(base64.urlsafe_b64decode(head + "==="))
    assert "alg" not in claims
    assert "typ" not in claims
    assert set(claims) == {
        "proposal_id",
        "payload_hash",
        "action_type",
        "approver",
        "nonce",
        "iat",
        "exp",
    }


def test_an_unexpected_claim_is_refused_rather_than_ignored():
    """extra="forbid": a token carrying a field we do not know is not trusted."""
    claims = _claims().model_dump()
    claims["bypass_checks"] = True
    head = (
        base64.urlsafe_b64encode(json.dumps(claims, sort_keys=True, separators=(",", ":")).encode())
        .decode()
        .rstrip("=")
    )
    # Sign it properly, so only the extra field can be the reason for refusal.
    import hashlib
    import hmac

    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SECRET.encode(), head.encode("ascii"), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )

    with pytest.raises(MalformedToken):
        decode(head + "." + signature, SECRET)


# ----------------------------------------------------------------------------------
# Malformed input
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["", "abc", "a.b.c", ".", "a.", ".b", "!!!.???", "eyJhIjoxfQ"],
)
def test_malformed_tokens_raise_malformed_not_invalid_signature(raw):
    with pytest.raises((MalformedToken, InvalidSignature)):
        decode(raw, SECRET)


def test_a_valid_signature_over_non_json_is_malformed():
    """The signature can hold while the payload is nonsense. That is not a valid token."""
    import hashlib
    import hmac

    head = base64.urlsafe_b64encode(b"not json at all").decode().rstrip("=")
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(SECRET.encode(), head.encode("ascii"), hashlib.sha256).digest()
        )
        .decode()
        .rstrip("=")
    )
    with pytest.raises(MalformedToken):
        decode(head + "." + signature, SECRET)


# ----------------------------------------------------------------------------------
# Expiry is the gateway's check 2, not this module's job
# ----------------------------------------------------------------------------------


def test_decode_does_not_reject_an_expired_token():
    """Section 5 needs to distinguish invalid_signature from token_expired.

    If decode() also enforced expiry, the gateway could not report which of the two
    failed -- so expiry lives in check 2, where it can have its own refusal code.
    """
    stale = _claims(iat=0, exp=1)
    claims = decode(mint(stale, SECRET), SECRET)
    assert claims.is_expired() is True


def test_is_expired_is_inclusive_at_the_boundary():
    at = int(now_utc().timestamp())
    assert _claims(exp=at).is_expired(at=at) is True
    assert _claims(exp=at + 1).is_expired(at=at) is False


# ----------------------------------------------------------------------------------
# An empty secret is a configuration failure, not a permissive default
# ----------------------------------------------------------------------------------


def test_minting_with_an_empty_secret_is_refused():
    with pytest.raises(ValueError, match="empty secret"):
        mint(_claims(), "")


def test_verifying_with_an_empty_secret_is_refused():
    """Otherwise an unconfigured deployment would verify every forged token identically."""
    with pytest.raises(ValueError, match="empty secret"):
        decode(mint(_claims(), SECRET), "")
