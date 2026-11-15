"""End to end: e-mail in, agent investigates, a person decides, the ERP is updated once."""

from sqlalchemy import func, select

from app.llm import DemoLLM
from app.models import AiRequest, ErpEscalation, ErpRefund, Event, OutboxMessage, Ticket
from tests.conftest import REPLY, ScriptedLLM, turn

REFUND = ("propose_refund", {"order_ref": "CMD-10428", "amount_eur": 189.0,
                             "reason": "Article reçu cassé"})


def _refunds(db) -> list[ErpRefund]:
    db.expire_all()
    return list(db.scalars(select(ErpRefund)).all())


def _happy_path(send, run) -> int:
    tid = send()
    llm = ScriptedLLM(
        turn(("lookup_customer", {}), ("get_order", {"order_ref": "CMD-10428"})),
        turn(("search_policy", {"query": "remboursement article cassé"})),
        turn(REFUND),
        turn(REPLY),
    )
    assert run(llm) is True
    return tid


def test_inbound_is_idempotent(client, send, db):
    first = send()
    again = client.post("/api/inbound", json={
        "message_id": "m-1@test", "from_email": "claire.moreau@example.fr",
        "subject": "doublon", "body": "le même message rejoué par le fournisseur"})
    assert again.json() == {"ticket_id": first, "created": False, "status": "new"}
    assert db.scalar(select(func.count()).select_from(Ticket)) == 1


def test_agent_prepares_but_nothing_is_executed_before_approval(send, run, db):
    tid = _happy_path(send, run)
    ticket = db.get(Ticket, tid)
    assert ticket.status == "awaiting_approval"
    assert ticket.draft_body.startswith("Bonjour")
    assert [(a.kind, a.status, a.payload["amount_cents"]) for a in ticket.actions] == [
        ("refund", "pending", 18900)]
    assert _refunds(db) == []  # the agent proposed; it did not refund
    assert db.scalar(select(OutboxMessage)) is None


def test_approval_refunds_once_and_records_the_reply(client, send, run, db):
    tid = _happy_path(send, run)
    action_id = db.get(Ticket, tid).actions[0].id
    form = {"body": "Bonjour Claire, votre remboursement est validé.", "action_ids": [action_id]}

    resp = client.post(f"/tickets/{tid}/approve", data=form, follow_redirects=False)
    assert resp.status_code == 303

    refunds = _refunds(db)
    assert [(r.order_ref, r.amount_cents) for r in refunds] == [("CMD-10428", 18900)]
    ticket = db.get(Ticket, tid)
    assert ticket.status == "resolved" and ticket.handled_by == "conseiller démo"
    assert db.scalar(select(OutboxMessage)).body == form["body"]  # the edited text, not the draft

    # A second click, or a replayed request, changes nothing.
    assert client.post(f"/tickets/{tid}/approve", data=form).status_code == 409
    assert len(_refunds(db)) == 1


def test_unchecked_action_is_refused_and_reply_still_goes(client, send, run, db):
    tid = _happy_path(send, run)
    resp = client.post(f"/tickets/{tid}/approve", data={"body": "Bonjour, voici notre réponse."},
                       follow_redirects=False)
    assert resp.status_code == 303
    assert _refunds(db) == []
    ticket = db.get(Ticket, tid)
    assert ticket.actions[0].status == "rejected" and ticket.status == "resolved"


def test_rejecting_the_draft_sends_nothing(client, send, run, db):
    tid = _happy_path(send, run)
    client.post(f"/tickets/{tid}/reject", data={"reason": "ton inadapté"})
    ticket = db.get(Ticket, tid)
    assert (ticket.status, ticket.reject_reason) == ("manual", "ton inadapté")
    assert ticket.actions[0].status == "rejected"
    assert _refunds(db) == [] and db.scalar(select(OutboxMessage)) is None


def test_failed_erp_action_keeps_ticket_open_and_sends_nothing(client, send, run, db):
    tid = _happy_path(send, run)
    action = db.get(Ticket, tid).actions[0]
    # Someone refunded this order by another route in the meantime.
    db.add(ErpRefund(order_ref="CMD-10428", amount_cents=18900, idempotency_key="autre-canal-1"))
    db.commit()

    resp = client.post(f"/tickets/{tid}/approve",
                       data={"body": "Bonjour, remboursement validé.", "action_ids": [action.id]},
                       follow_redirects=False)
    assert resp.headers["location"].endswith("?erreur=action")
    db.expire_all()
    ticket = db.get(Ticket, tid)
    assert ticket.status == "awaiting_approval" and ticket.actions[0].status == "failed"
    assert db.scalar(select(OutboxMessage)) is None  # no "you were refunded" e-mail
    assert len(_refunds(db)) == 1


def test_every_model_call_is_metered(send, run, db):
    tid = _happy_path(send, run)
    rows = db.scalars(select(AiRequest).where(AiRequest.ticket_id == tid)).all()
    assert [r.purpose for r in rows] == ["classification", "agent", "agent", "agent", "agent"]
    assert sum(r.input_tokens for r in rows) == 450 and all(r.ok for r in rows)


def test_timeline_records_each_tool_call(send, run, db):
    tid = _happy_path(send, run)
    names = db.scalars(select(Event.title).where(Event.ticket_id == tid,
                                                 Event.kind == "tool_call")).all()
    assert names == ["lookup_customer", "get_order", "search_policy", "propose_refund",
                     "submit_reply"]


def test_demo_mode_runs_the_whole_refund_scenario(client, send, run, db):
    """The scripted demo provider goes through the real tools and guardrails."""
    tid = send()
    assert run(DemoLLM()) is True
    ticket = db.get(Ticket, tid)
    assert ticket.status == "awaiting_approval" and ticket.category == "remboursement"
    assert ticket.actions[0].payload == {"order_ref": "CMD-10428", "amount_cents": 18900,
                                         "reason": ticket.actions[0].payload["reason"]}
    assert "CMD-10428" in ticket.draft_body and "validation" in ticket.draft_body
    assert "[Mode démo" in ticket.internal_summary
    page = client.get(f"/tickets/{tid}")
    assert page.status_code == 200 and "Rembourser 189,00 €" in page.text
    assert client.get("/").status_code == 200 and client.get("/couts").status_code == 200


def test_demo_mode_escalates_an_angry_technical_ticket(client, send, run, db):
    tid = send(message_id="m-2@test", subject="Fauteuil en panne, c'est inadmissible",
               body="Le mécanisme de mon fauteuil CMD-10477 ne fonctionne plus. Inadmissible.")
    run(DemoLLM())
    ticket = db.get(Ticket, tid)
    assert (ticket.category, ticket.urgency) == ("technique", "haute")
    action_id = ticket.actions[0].id
    client.post(f"/tickets/{tid}/approve",
                data={"body": "Bonjour, votre dossier est transmis.", "action_ids": [action_id]})
    db.expire_all()
    assert db.scalar(select(ErpEscalation)).team == "technique"
