"""A stand-in for the client's shop/ERP system, exposed as a small REST API.

It exists so the integration is a real HTTP integration: token auth, 404s,
validation errors and idempotent writes, as an actual ERP would behave.
"""

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import ErpCustomer, ErpEscalation, ErpOrder, ErpRefund


def _check_token(x_erp_token: str = Header(default="")) -> None:
    if x_erp_token != get_settings().erp_token:
        raise HTTPException(401, "Jeton ERP invalide")


router = APIRouter(prefix="/mock-erp", tags=["mock-erp"], dependencies=[Depends(_check_token)])


def refunded_cents(db: Session, order_ref: str) -> int:
    total = db.scalar(
        select(func.coalesce(func.sum(ErpRefund.amount_cents), 0)).where(
            ErpRefund.order_ref == order_ref
        )
    )
    return int(total or 0)


def _order_json(db: Session, order: ErpOrder) -> dict:
    return {
        "ref": order.ref,
        "customer_email": order.customer.email,
        "status": order.status,
        "total_cents": order.total_cents,
        "refunded_cents": refunded_cents(db, order.ref),
        "currency": "EUR",
        "items": order.items,
        "carrier": order.carrier,
        "tracking": order.tracking,
        "ordered_at": order.ordered_at.date().isoformat(),
        "delivered_at": order.delivered_at.date().isoformat() if order.delivered_at else None,
    }


@router.get("/customers")
def find_customer(email: str, db: Session = Depends(get_db)) -> dict:
    customer = db.scalar(select(ErpCustomer).where(func.lower(ErpCustomer.email) == email.lower()))
    if customer is None:
        raise HTTPException(404, "Client inconnu")
    orders = db.scalars(
        select(ErpOrder)
        .where(ErpOrder.customer_id == customer.id)
        .order_by(ErpOrder.ordered_at.desc())
    ).all()
    return {
        "email": customer.email,
        "name": customer.name,
        "tier": customer.tier,
        "recent_orders": [{"ref": o.ref, "status": o.status} for o in orders[:5]],
    }


@router.get("/orders/{ref}")
def get_order(ref: str, db: Session = Depends(get_db)) -> dict:
    order = db.get(ErpOrder, ref.upper())
    if order is None:
        raise HTTPException(404, "Commande inconnue")
    return _order_json(db, order)


class RefundIn(BaseModel):
    order_ref: str
    amount_cents: int = Field(gt=0)
    reason: str = ""


@router.post("/refunds", status_code=201)
def create_refund(
    body: RefundIn,
    idempotency_key: str = Header(min_length=8),
    db: Session = Depends(get_db),
) -> dict:
    existing = db.scalar(select(ErpRefund).where(ErpRefund.idempotency_key == idempotency_key))
    if existing is not None:  # same request replayed: return the first result
        return {"refund_id": existing.id, "amount_cents": existing.amount_cents, "replayed": True}
    order = db.get(ErpOrder, body.order_ref.upper())
    if order is None:
        raise HTTPException(404, "Commande inconnue")
    remaining = order.total_cents - refunded_cents(db, order.ref)
    if body.amount_cents > remaining:
        raise HTTPException(422, f"Montant supérieur au reste remboursable ({remaining} centimes)")
    refund = ErpRefund(
        order_ref=order.ref,
        amount_cents=body.amount_cents,
        reason=body.reason,
        idempotency_key=idempotency_key,
    )
    db.add(refund)
    db.commit()
    return {"refund_id": refund.id, "amount_cents": refund.amount_cents, "replayed": False}


class EscalationIn(BaseModel):
    team: str
    reason: str
    customer_email: str


@router.post("/escalations", status_code=201)
def create_escalation(
    body: EscalationIn,
    idempotency_key: str = Header(min_length=8),
    db: Session = Depends(get_db),
) -> dict:
    existing = db.scalar(
        select(ErpEscalation).where(ErpEscalation.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return {"escalation_id": existing.id, "replayed": True}
    row = ErpEscalation(**body.model_dump(), idempotency_key=idempotency_key)
    db.add(row)
    db.commit()
    return {"escalation_id": row.id, "replayed": False}
