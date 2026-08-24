---
tags: [reference, api]
---

# 06 — API Reference

Versioned under `/v1` and published as OpenAPI at `http://localhost:8000/docs`. The generated
document is also committed at `docs/openapi.json` — 15 operations, 17 schemas.

The web UI is **one client of this API and holds no privileges of its own**. Every read goes
through the same read models `/v1` publishes, and every write through the same service
functions `/v1` calls. That is what allows a future mobile client to be an additional client
rather than a second implementation.

## Endpoints

| Verb | Path | Purpose |
|---|---|---|
| POST | `/v1/runs` | Start a run. Body: `goal`, `min_days_overdue`, `max_proposals`. Returns `run_id`; executes asynchronously (202). |
| GET | `/v1/runs` | List runs with status and counts. |
| GET | `/v1/runs/{id}` | Run detail with the full ordered transcript. |
| GET | `/v1/runs/{id}/stream` | SSE stream of steps as they occur. Takes `?after=` to resume. |
| GET | `/v1/proposals` | Filter by `status` and `run_id`. Default pending. Sorted by amount descending. |
| GET | `/v1/proposals/{id}` | Full proposal: letter, rationale, invoice facts, approvals, executions. |
| POST | `/v1/proposals/{id}/approve` | Body: `actor`, optional `note`, optional `edited_body`. Records the approval, mints the token, calls the gateway, returns the outcome. |
| POST | `/v1/proposals/{id}/reject` | Body: `actor`, `note` (**required**). Terminal. |
| POST | `/v1/proposals/{id}/edit` | Save an edited letter and re-hash it. |
| POST | `/v1/proposals/{id}/attempt-unapproved` | Ask the gateway to send something nobody approved. It refuses. |
| GET | `/v1/invoices/overdue` | Read-only passthrough, so the UI can show ground truth beside the agent's output. |
| GET | `/v1/audit` | Paged audit log, newest first, with chain-verification status. |
| GET | `/v1/audit/verify` | Recompute the whole chain; reports intact or broken with the first bad row. |
| GET | `/v1/outbox` | Letters the gateway captured. |
| GET | `/healthz` | Liveness plus reachability of Stripe, Anthropic and the gateway. |

**There is no endpoint that sends anything.** A test asserts no path contains "send". The
closest is `/approve`, which records a human decision and asks the gateway to decide.

## The error contract

```json
{ "error": { "code": "not_approved",
             "message": "Proposal is in status 'pending'.",
             "proposal_id": "..." } }
```

Gateway refusal codes surface **unchanged**, and the refusal's HTTP status is mirrored, because
the refusal is the feature. When a refusal happens the UI shows the code prominently rather
than a generic failure toast.

A refusal envelope from the gateway also carries `failed_check` and `checks_passed`, so a
caller can see how far the request got before it was stopped.

| Code | Status | Meaning |
|---|---|---|
| `invalid_signature` | 401 | Check 1. The token was not minted with the shared secret. |
| `malformed_token` | 401 | Not two base64url segments carrying valid claims. |
| `token_expired` | 401 | Check 2. |
| `token_replayed` | 409 | Check 3. The same token, used for a different request. |
| `not_approved` | 403 | Check 4. **This database** does not say the proposal is approved. |
| `approval_mismatch` | 403 | Check 5. No approve record, or a different approver or nonce. |
| `payload_modified` | 409 | Check 6. The stored letter no longer hashes to what was approved. |
| `execution_failed` | 502 | All seven passed; the provider failed. Recorded, not retried silently. |
| `gateway_unreachable` | 502 | A deployment fault, not a refusal. Distinguished deliberately. |
| `proposal_not_found` | 404 | |
| `proposal_not_pending` | 409 | Already decided, or expired. |

## On the edit-then-approve path

If the operator edits the body before approving, the edited text is stored on the approval
record **and the payload hash is recomputed from the edited content before the token is
minted**. The operator approves what they actually read. The token is never minted from the
original draft, and a test decodes the minted token to prove it.

Saving an edit separately (`/edit`) also re-hashes, which invalidates any token minted earlier.
The response says so in words, and the UI repeats it.

## Money in responses

Every amount appears twice: `amount_due` in **minor units** (2500 means $25.00) and
`amount_display` as a preformatted string. Use `amount_display` in anything a human reads and
never re-derive it. See [[04 — Stripe Integration]] for why.

## Streaming a run

```bash
curl -N http://localhost:8000/v1/runs/<id>/stream
```

Events are `step` (one transcript entry, with its `seq`) and a final `done`. The stream **tails
the database** rather than an in-process queue, so `?after=<seq>` resumes exactly where a
dropped connection left off, several clients can watch one run, and the stream is a view over
the persisted transcript rather than a second copy of it.

## Related

- [[02 — The Approval Boundary]] — what `/approve` hands to the gateway
- [[05 — Data Model]] — the tables behind these shapes
- [[07 — Running Locally]] — curl recipes
