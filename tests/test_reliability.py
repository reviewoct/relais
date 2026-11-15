"""Failures: the model is down, the ERP is flaky, a job is retried."""

import httpx
import pytest
from sqlalchemy import select

from app import kb
from app.erp_client import ERPClient, ERPError, ERPUnavailable
from app.llm import PermanentLLMError, TransientLLMError, cost_micro_usd
from app.models import AiRequest, Job, Ticket, utcnow
from tests.conftest import REPLY, ScriptedLLM, turn


def _make_due(db, tid):
    job = db.scalar(select(Job).where(Job.ticket_id == tid))
    job.run_after = utcnow()
    db.commit()


def test_transient_model_failure_is_retried_with_backoff(send, run, db):
    tid = send()
    assert run(ScriptedLLM(TransientLLMError("529 overloaded"))) is True
    db.expire_all()
    job = db.scalar(select(Job).where(Job.ticket_id == tid))
    assert (job.status, job.attempts) == ("queued", 1) and "overloaded" in job.last_error
    assert db.get(Ticket, tid).status == "new"

    assert run(ScriptedLLM(turn(REPLY))) is False  # not due yet: backoff
    _make_due(db, tid)
    assert run(ScriptedLLM(turn(REPLY))) is True
    db.expire_all()
    assert db.get(Ticket, tid).status == "awaiting_approval"
    failed = db.scalars(select(AiRequest).where(AiRequest.ok.is_(False))).all()
    assert len(failed) == 1  # the failed call is in the ledger too


def test_job_is_dead_lettered_after_max_attempts(send, run, db):
    tid = send()
    for _ in range(3):
        run(ScriptedLLM(TransientLLMError("timeout")))
        _make_due(db, tid)
    db.expire_all()
    assert db.scalar(select(Job.status).where(Job.ticket_id == tid)) == "dead"
    assert db.get(Ticket, tid).status == "failed"
    assert run(ScriptedLLM(turn(REPLY))) is False  # a dead job is not picked up again


def test_permanent_model_error_is_not_retried(send, run, db):
    tid = send()
    run(ScriptedLLM(PermanentLLMError("401 invalid api key")))
    db.expire_all()
    job = db.scalar(select(Job).where(Job.ticket_id == tid))
    assert (job.status, job.attempts) == ("dead", 1)


def test_retry_does_not_duplicate_proposed_actions(send, run, db):
    tid = send()
    refund = ("propose_refund", {"order_ref": "CMD-10428", "amount_eur": 189, "reason": "cassé"})
    run(ScriptedLLM(turn(refund), TransientLLMError("network")))
    _make_due(db, tid)
    run(ScriptedLLM(turn(refund), turn(REPLY)))
    db.expire_all()
    assert len(db.get(Ticket, tid).actions) == 1


def test_failed_ticket_can_be_requeued_from_the_dashboard(client, send, run, db):
    tid = send()
    run(ScriptedLLM(PermanentLLMError("bad request")))
    assert client.post(f"/tickets/{tid}/retry", follow_redirects=False).status_code == 303
    assert run(ScriptedLLM(turn(REPLY))) is True
    db.expire_all()
    assert db.get(Ticket, tid).status == "awaiting_approval"


def _erp_with(handler) -> ERPClient:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ERPClient("http://erp.test", "t", client=client, sleep=lambda _s: None)


def test_erp_client_retries_server_errors_then_succeeds():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ref": "CMD-1", "customer_email": "a@b.fr"})

    assert _erp_with(handler).get_order("CMD-1")["ref"] == "CMD-1"
    assert len(calls) == 3 and calls[0].headers["X-ERP-Token"] == "t"


def test_erp_client_gives_up_after_its_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(ERPUnavailable):
        _erp_with(handler).get_order("CMD-1")


def test_erp_client_does_not_retry_a_definite_refusal():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(422, json={"detail": "Montant supérieur"})

    with pytest.raises(ERPError, match="Montant supérieur"):
        _erp_with(handler).create_refund("CMD-1", 100, "r", "key-12345")
    assert len(calls) == 1 and calls[0].headers["Idempotency-Key"] == "key-12345"


def test_mock_erp_replays_a_refund_sent_twice_with_the_same_key(erp):
    first = erp.create_refund("CMD-10428", 5000, "geste", "same-key-0001")
    second = erp.create_refund("CMD-10428", 5000, "geste", "same-key-0001")
    assert second == {**first, "replayed": True}
    assert erp.get_order("CMD-10428")["refunded_cents"] == 5000


def test_mock_erp_rejects_a_wrong_token(client):
    assert client.get("/mock-erp/orders/CMD-10428").status_code == 401


def test_policy_search_finds_the_relevant_article(db):
    hits = kb.search(db, "délai pour rembourser un article abîmé")
    assert hits[0]["slug"] == "retours-remboursements"
    assert kb.search(db, "xyzzy") == []


def test_cost_is_computed_in_millionths_of_a_dollar():
    assert cost_micro_usd("claude-opus-5-5", 1_000_000, 0) == 4_000_000
    assert cost_micro_usd("claude-opus-5-5", 1500, 300) == 12_000
    assert cost_micro_usd("modele-inconnu", 10, 10) is None  # no price, no invented number
