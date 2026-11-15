"""Human decisions. This is the only module that makes anything happen outside Relais."""

from sqlalchemy.orm import Session

from app.agent import log
from app.erp_client import ERPClient, ERPError, ERPUnavailable
from app.models import ActionStatus, OutboxMessage, ProposedAction, Ticket, TicketStatus, utcnow


class Conflict(Exception):
    """The ticket is not in a state where this decision is possible."""


def _require_awaiting(ticket: Ticket) -> None:
    if ticket.status != TicketStatus.AWAITING_APPROVAL:
        raise Conflict("Ce ticket n'est plus en attente de validation.")


def _execute(erp: ERPClient, ticket: Ticket, action: ProposedAction) -> dict:
    # The key is derived from the action's id, so approving twice, or retrying
    # after a timeout, can never refund twice.
    key = f"relais-action-{action.id}"
    p = action.payload
    if action.kind == "refund":
        return erp.create_refund(p["order_ref"], p["amount_cents"], p["reason"], key)
    if action.kind == "escalation":
        return erp.create_escalation(p["team"], p["reason"], ticket.from_email, key)
    raise ERPError(f"Type d'action inconnu : {action.kind}")


def approve(
    db: Session, ticket: Ticket, user: str, body: str, approved_ids: set[int], erp: ERPClient
) -> bool:
    """Run the approved actions, then record the reply. Returns False if an action failed."""
    _require_awaiting(ticket)
    if not body.strip():
        raise Conflict("La réponse ne peut pas être vide.")
    ticket.draft_body = body
    all_ok = True
    for action in ticket.actions:
        if action.status in (ActionStatus.EXECUTED, ActionStatus.REJECTED):
            continue
        action.decided_by, action.decided_at = user, utcnow()
        if action.id not in approved_ids:
            action.status = ActionStatus.REJECTED
            log(db, ticket, user, "action_rejected", f"Action refusée : {action.kind}",
                action.payload)
            continue
        db.commit()  # release our write lock before calling out
        try:
            action.result = _execute(erp, ticket, action)
            action.status = ActionStatus.EXECUTED
            log(db, ticket, user, "action_executed", f"Action exécutée : {action.kind}",
                {**action.payload, "result": action.result})
        except (ERPError, ERPUnavailable) as exc:
            action.status, action.result = ActionStatus.FAILED, {"error": str(exc)}
            all_ok = False
            log(db, ticket, "système", "action_failed", f"Échec de l'action : {action.kind}",
                {"error": str(exc)})
    if not all_ok:
        # Keep the ticket open and send nothing: the customer must not be told
        # about a refund that did not happen. The reviewer can try again.
        db.commit()
        return False
    db.add(
        OutboxMessage(
            ticket_id=ticket.id,
            to_email=ticket.from_email,
            subject=ticket.draft_subject or f"Re: {ticket.subject}",
            body=body,
        )
    )
    ticket.status, ticket.handled_by, ticket.resolved_at = TicketStatus.RESOLVED, user, utcnow()
    log(db, ticket, user, "reply_sent", "Réponse validée et placée dans la boîte d'envoi")
    return True


def reject(db: Session, ticket: Ticket, user: str, reason: str) -> None:
    _require_awaiting(ticket)
    for action in ticket.actions:
        if action.status in (ActionStatus.PENDING, ActionStatus.FAILED):
            action.status, action.decided_by, action.decided_at = (
                ActionStatus.REJECTED, user, utcnow())
    ticket.status, ticket.handled_by = TicketStatus.MANUAL, user
    ticket.reject_reason = reason.strip() or None
    log(db, ticket, user, "rejected", "Brouillon refusé — repris par un conseiller",
        {"reason": ticket.reject_reason})
