"""The agent's tools: a closed list, each with a schema, a validator and a handler.

The model chooses which tool to call. The code decides whether the call is
allowed. Every rule that protects money or another customer's data is enforced
here, in Python, and does not depend on the prompt being obeyed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import kb
from app.erp_client import ERPClient, ERPError, ERPUnavailable
from app.models import ActionStatus, ProposedAction, Ticket
from app.schemas import (
    GetOrderIn,
    NoInput,
    ProposeEscalationIn,
    ProposeRefundIn,
    SearchPolicyIn,
    SubmitReplyIn,
)


class ToolError(Exception):
    """Returned to the model as an error result, so it can correct course."""


@dataclass
class ToolContext:
    db: Session
    ticket: Ticket
    erp: ERPClient


@dataclass
class ToolDef:
    name: str
    description: str
    properties: dict[str, Any]
    input_model: type[BaseModel]
    handler: Callable[[ToolContext, Any], dict]

    def api_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": self.properties,
                "required": list(self.properties),
                "additionalProperties": False,
            },
        }


def _own_order(ctx: ToolContext, order_ref: str) -> dict:
    """Fetch an order, but only if it belongs to the person who wrote the ticket."""
    order = ctx.erp.get_order(order_ref.strip().upper())
    if order is None or order["customer_email"].lower() != ctx.ticket.from_email.lower():
        # Same message in both cases: do not reveal that someone else's order exists.
        raise ToolError(f"Aucune commande {order_ref} trouvée pour ce client.")
    return order


def _lookup_customer(ctx: ToolContext, _: NoInput) -> dict:
    customer = ctx.erp.find_customer(ctx.ticket.from_email)
    if customer is None:
        return {"found": False, "note": "Aucun compte client pour cette adresse e-mail."}
    return {"found": True, **customer}


def _get_order(ctx: ToolContext, args: GetOrderIn) -> dict:
    return _own_order(ctx, args.order_ref)


def _search_policy(ctx: ToolContext, args: SearchPolicyIn) -> dict:
    hits = kb.search(ctx.db, args.query)
    return {"articles": hits} if hits else {"articles": [], "note": "Aucun article trouvé."}


def _propose_refund(ctx: ToolContext, args: ProposeRefundIn) -> dict:
    order = _own_order(ctx, args.order_ref)
    amount_cents = round(args.amount_eur * 100)
    remaining = order["total_cents"] - order["refunded_cents"]
    if amount_cents > remaining:
        raise ToolError(
            f"Montant refusé : {amount_cents / 100:.2f} € dépasse le reste remboursable "
            f"de la commande ({remaining / 100:.2f} €)."
        )
    duplicate = ctx.db.scalar(
        select(ProposedAction).where(
            ProposedAction.ticket_id == ctx.ticket.id,
            ProposedAction.kind == "refund",
            ProposedAction.status == ActionStatus.PENDING,
        )
    )
    if duplicate is not None:
        raise ToolError("Un remboursement est déjà en attente de validation pour ce ticket.")
    action = ProposedAction(
        ticket_id=ctx.ticket.id,
        kind="refund",
        payload={"order_ref": order["ref"], "amount_cents": amount_cents, "reason": args.reason},
    )
    ctx.db.add(action)
    ctx.db.flush()
    return {
        "status": "en_attente_de_validation_humaine",
        "action_id": action.id,
        "note": "Rien n'a été remboursé. Un responsable doit valider.",
    }


def _propose_escalation(ctx: ToolContext, args: ProposeEscalationIn) -> dict:
    action = ProposedAction(
        ticket_id=ctx.ticket.id,
        kind="escalation",
        payload={"team": args.team, "reason": args.reason},
    )
    ctx.db.add(action)
    ctx.db.flush()
    return {"status": "en_attente_de_validation_humaine", "action_id": action.id}


def _submit_reply(ctx: ToolContext, args: SubmitReplyIn) -> dict:
    t = ctx.ticket
    t.draft_subject, t.draft_body = args.subject, args.body
    t.internal_summary = args.internal_summary
    t.confidence = round(args.confidence * 100)
    return {"status": "brouillon_enregistre"}


SUBMIT_TOOL = "submit_reply"
_S = {"type": "string"}

TOOLS: dict[str, ToolDef] = {
    t.name: t
    for t in [
        ToolDef(
            "lookup_customer",
            "Retrouve la fiche client de l'expéditeur du ticket dans l'ERP : nom, niveau de "
            "fidélité et références de ses commandes récentes. À appeler en premier.",
            {},
            NoInput,
            _lookup_customer,
        ),
        ToolDef(
            "get_order",
            "Lit une commande dans l'ERP : statut, montant, articles, transporteur, suivi, dates. "
            "Ne fonctionne que pour les commandes de l'expéditeur du ticket.",
            {"order_ref": {**_S, "description": "Référence de commande, ex. CMD-10428"}},
            GetOrderIn,
            _get_order,
        ),
        ToolDef(
            "search_policy",
            "Cherche dans les règles internes (livraison, retours, remboursements, facturation, "
            "garantie, compte). À utiliser avant d'affirmer une règle ou un délai au client.",
            {"query": {**_S, "description": "Mots-clés, ex. « délai remboursement colis abîmé »"}},
            SearchPolicyIn,
            _search_policy,
        ),
        ToolDef(
            "propose_refund",
            "Propose un remboursement à la validation d'un responsable. N'exécute rien : le "
            "remboursement n'a lieu que si un humain l'approuve.",
            {
                "order_ref": _S,
                "amount_eur": {"type": "number", "description": "Montant en euros, ex. 49.9"},
                "reason": {**_S, "description": "Justification, avec la règle appliquée"},
            },
            ProposeRefundIn,
            _propose_refund,
        ),
        ToolDef(
            "propose_escalation",
            "Propose de transmettre le dossier à une équipe interne quand il dépasse ce que le "
            "support peut régler. Soumis à validation humaine.",
            {
                "team": {**_S, "enum": ["logistique", "comptabilite", "technique", "direction"]},
                "reason": _S,
            },
            ProposeEscalationIn,
            _propose_escalation,
        ),
        ToolDef(
            SUBMIT_TOOL,
            "Enregistre le brouillon de réponse au client et termine le traitement. À appeler une "
            "seule fois, en dernier. Le brouillon sera relu par un humain avant envoi.",
            {
                "subject": _S,
                "body": {**_S, "description": "Réponse complète, dans la langue du client"},
                "internal_summary": {
                    **_S,
                    "description": "Pour le relecteur : ce qui a été vérifié et pourquoi",
                },
                "confidence": {"type": "number", "description": "Confiance entre 0 et 1"},
            },
            SubmitReplyIn,
            _submit_reply,
        ),
    ]
}


def api_tools() -> list[dict]:
    return [t.api_schema() for t in TOOLS.values()]


def execute(ctx: ToolContext, name: str, raw_input: Any) -> tuple[dict, bool]:
    """Run one tool call. Returns (result, is_error). Never raises for model mistakes."""
    tool = TOOLS.get(name)
    if tool is None:
        return {"error": f"Outil inconnu : {name}"}, True
    try:
        args = tool.input_model.model_validate(raw_input)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        return {"error": f"Arguments invalides — {problems}"}, True
    try:
        return tool.handler(ctx, args), False
    except (ToolError, ERPError) as exc:
        return {"error": str(exc)}, True
    except ERPUnavailable:
        # Not the model's fault and not fixable by it: let the job retry later.
        raise
