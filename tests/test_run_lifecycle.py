"""Run status accuracy.

Every test here corresponds to a state the dashboard was actually found in:

* four runs showing "awaiting approval" whose every proposal had already been executed;
* one run stuck at "running" and one at "queued", left behind by a container restart.

The common cause is that a run's status is a row in a database while the thing advancing it
is a thread in a process. A status that only ever gets written forwards, by a process that
may not survive, will be wrong the first time anything unusual happens.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.store.repositories import (
    RunStore,
    abandon_orphaned_runs,
    settle_decided_runs,
    settle_run_if_decided,
)
from shared.db import session_scope
from shared.models import AuditEvent, Proposal, Run
from tests.factories import letter_payload, make_proposal


def a_run(status: str = "awaiting_approval") -> str:
    run_id = RunStore.create(goal="g", operator_id="op@x", params={})
    with session_scope() as session:
        session.get(Run, run_id).status = status
    return run_id


def proposals_for(run_id: str, *statuses: str) -> list[str]:
    ids = []
    with session_scope() as session:
        run = session.get(Run, run_id)
        for index, status in enumerate(statuses):
            proposal = make_proposal(
                session,
                run=run,
                status="pending",
                payload=letter_payload(invoice_id=f"in_{run_id[:6]}_{index}"),
            )
            proposal.status = status
            ids.append(proposal.id)
    return ids


def status_of(run_id: str) -> str:
    with session_scope() as session:
        return session.get(Run, run_id).status


# ----------------------------------------------------------------------------------
# A: awaiting_approval must not outlive the queue it is waiting on
# ----------------------------------------------------------------------------------


def test_a_run_whose_proposals_were_all_executed_settles(db_path):
    """The reported bug, exactly: approved and executed, still saying awaiting approval."""
    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "executed", "executed")

    with session_scope() as session:
        assert settle_run_if_decided(session, run_id) is True

    assert status_of(run_id) == "completed"


@pytest.mark.parametrize(
    "outcomes",
    [
        ("executed",),
        ("rejected",),
        ("executed", "rejected"),
        ("expired", "expired"),
        ("failed",),
        ("executed", "rejected", "expired", "failed"),
    ],
)
def test_any_mix_of_terminal_outcomes_settles_the_run(db_path, outcomes):
    """A run whose proposals were all rejected is just as settled as one all executed."""
    run_id = a_run("awaiting_approval")
    proposals_for(run_id, *outcomes)

    with session_scope() as session:
        settle_run_if_decided(session, run_id)

    assert status_of(run_id) == "completed"


def test_a_run_with_one_proposal_still_pending_does_not_settle(db_path):
    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "executed", "pending")

    with session_scope() as session:
        assert settle_run_if_decided(session, run_id) is False

    assert status_of(run_id) == "awaiting_approval"


def test_settling_is_idempotent(db_path):
    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "executed")

    with session_scope() as session:
        assert settle_run_if_decided(session, run_id) is True
        assert settle_run_if_decided(session, run_id) is False, "already settled"


@pytest.mark.parametrize("status", ["queued", "running", "completed", "failed"])
def test_settling_never_touches_a_run_in_any_other_status(db_path, status):
    """Only awaiting_approval is settled. A running run is not finished by a read."""
    run_id = a_run(status)
    proposals_for(run_id, "executed")

    with session_scope() as session:
        assert settle_run_if_decided(session, run_id) is False

    assert status_of(run_id) == status


def test_settling_records_the_outcome_in_the_audit_log(db_path):
    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "executed", "rejected")

    with session_scope() as session:
        settle_run_if_decided(session, run_id)

    with session_scope() as session:
        event = session.execute(
            select(AuditEvent).where(AuditEvent.event == "run.settled")
        ).scalar_one()
    assert event.subject_id == run_id
    assert event.detail["outcomes"] == {"executed": 1, "rejected": 1}


def test_a_run_that_proposed_nothing_is_left_alone(db_path):
    """It was already `completed` by the loop. There is nothing to settle."""
    run_id = a_run("completed")
    with session_scope() as session:
        assert settle_run_if_decided(session, run_id) is False


def test_the_sweep_settles_every_stale_run_at_once(db_path):
    """Historical rows heal without anyone visiting each run."""
    stale = [a_run("awaiting_approval") for _ in range(3)]
    for run_id in stale:
        proposals_for(run_id, "executed")
    live = a_run("awaiting_approval")
    proposals_for(live, "pending")

    assert settle_decided_runs() == 3

    assert all(status_of(run_id) == "completed" for run_id in stale)
    assert status_of(live) == "awaiting_approval"


def test_the_dashboard_read_path_heals_stale_runs(db_path):
    """The screenshot's four rows would have corrected themselves on the next page load."""
    from app.api import queries

    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "executed")

    rows = queries.list_runs()

    assert [row["status"] for row in rows if row["id"] == run_id] == ["completed"]


def test_the_run_detail_read_path_heals_one_run(db_path):
    from app.api import queries

    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "rejected")

    assert queries.get_run(run_id)["status"] == "completed"


# ----------------------------------------------------------------------------------
# B and C: a run whose worker no longer exists
# ----------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["queued", "running"])
def test_a_run_abandoned_by_a_dead_process_is_failed(db_path, status):
    """Both stuck rows from the screenshot. Nothing was ever going to finish them."""
    run_id = a_run(status)

    assert abandon_orphaned_runs() == [run_id]

    with session_scope() as session:
        run = session.get(Run, run_id)
    assert run.status == "failed"
    assert "restarted" in run.error
    assert run.ended_at is not None


def test_abandoning_records_it_in_the_audit_log(db_path):
    run_id = a_run("running")
    abandon_orphaned_runs()

    with session_scope() as session:
        event = session.execute(
            select(AuditEvent).where(AuditEvent.event == "run.abandoned")
        ).scalar_one()
    assert event.subject_id == run_id


@pytest.mark.parametrize("status", ["completed", "failed", "awaiting_approval"])
def test_a_finished_run_is_never_re_failed(db_path, status):
    run_id = a_run(status)
    assert abandon_orphaned_runs() == []
    assert status_of(run_id) == status


def test_abandoning_keeps_whatever_the_run_managed_to_produce(db_path):
    """A partial transcript and its proposals survive. Only the status is corrected."""
    run_id = a_run("running")
    ids = proposals_for(run_id, "pending")
    RunStore(run_id).append_step(type="message", payload={"text": "got this far"})

    abandon_orphaned_runs()

    with session_scope() as session:
        assert session.get(Proposal, ids[0]).status == "pending"
        assert session.get(Run, run_id).status == "failed"


def test_startup_reconciles_both_problems_together(db_path):
    """What the app does on boot, and what would have cleaned up the screenshot."""
    orphan = a_run("running")
    stale = a_run("awaiting_approval")
    proposals_for(stale, "executed")

    abandoned = abandon_orphaned_runs()
    settled = settle_decided_runs()

    assert abandoned == [orphan]
    assert settled == 1
    assert status_of(orphan) == "failed"
    assert status_of(stale) == "completed"


def test_the_chain_stays_intact_through_all_of_it(db_path):
    from shared.audit import verify_chain

    orphan = a_run("running")
    stale = a_run("awaiting_approval")
    proposals_for(stale, "executed")
    abandon_orphaned_runs()
    settle_decided_runs()

    with session_scope() as session:
        assert verify_chain(session).intact is True
    assert status_of(orphan) == "failed"


def test_a_run_whose_last_proposal_merely_expired_settles(db_path):
    """Expiry is a decision too, and it happens without anyone clicking anything.

    Settling had to expire first: otherwise a run whose only pending letter had lapsed kept
    reporting that it was waiting on it, until some other page happened to run the sweep.
    """
    from shared.clock import now_utc
    from shared.models import Proposal as ProposalModel

    run_id = a_run("awaiting_approval")
    proposals_for(run_id, "pending")
    with session_scope() as session:
        session.query(ProposalModel).one().expires_at = now_utc().replace(year=2020)

    assert settle_decided_runs() == 1
    assert status_of(run_id) == "completed"
    with session_scope() as session:
        assert session.query(ProposalModel).one().status == "expired"
