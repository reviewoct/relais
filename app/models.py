"""Tables. Money is stored in integer cents, never as a float."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TicketStatus(StrEnum):
    NEW = "new"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    RESOLVED = "resolved"
    MANUAL = "manual"  # handed to a person: rejected draft, refusal, or step limit
    FAILED = "failed"


class ActionStatus(StrEnum):
    PENDING = "pending"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), unique=True)
    from_email: Mapped[str] = mapped_column(String(255), index=True)
    from_name: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default=TicketStatus.NEW, index=True)

    category: Mapped[str | None] = mapped_column(String(32))
    urgency: Mapped[str | None] = mapped_column(String(16))
    language: Mapped[str | None] = mapped_column(String(16))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    summary: Mapped[str | None] = mapped_column(Text)

    draft_subject: Mapped[str | None] = mapped_column(String(500))
    draft_body: Mapped[str | None] = mapped_column(Text)
    internal_summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[int | None] = mapped_column(Integer)  # percent
    handled_by: Mapped[str | None] = mapped_column(String(255))
    reject_reason: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    events: Mapped[list["Event"]] = relationship(order_by="Event.id", cascade="all, delete-orphan")
    actions: Mapped[list["ProposedAction"]] = relationship(
        order_by="ProposedAction.id", cascade="all, delete-orphan"
    )


class Event(Base):
    """Append-only timeline: every agent step and every human decision."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class ProposedAction(Base):
    """Something the agent wants done. Nothing here runs until a person approves it."""

    __tablename__ = "proposed_actions"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # refund | escalation
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default=ActionStatus.PENDING)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    decided_by: Mapped[str | None] = mapped_column(String(255))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxMessage(Base):
    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), unique=True)
    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="simulated")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AiRequest(Base):
    """One row per model call, so cost reporting is a query over real records."""

    __tablename__ = "ai_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    purpose: Mapped[str] = mapped_column(String(32))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_micro_usd: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    ok: Mapped[bool] = mapped_column(default=True)
    error: Mapped[str | None] = mapped_column(Text)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class KbArticle(Base):
    __tablename__ = "kb_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    title: Mapped[str] = mapped_column(String(255))
    body: Mapped[str] = mapped_column(Text)


# --- Mock ERP -----------------------------------------------------------------
# These tables stand in for an external shop/ERP system. The agent never reads
# them directly: it goes through the HTTP API in mock_erp.py, like a real integration.


class ErpCustomer(Base):
    __tablename__ = "erp_customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    tier: Mapped[str] = mapped_column(String(32), default="standard")


class ErpOrder(Base):
    __tablename__ = "erp_orders"

    ref: Mapped[str] = mapped_column(String(32), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("erp_customers.id"))
    status: Mapped[str] = mapped_column(String(32))
    total_cents: Mapped[int] = mapped_column(Integer)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    carrier: Mapped[str | None] = mapped_column(String(64))
    tracking: Mapped[str | None] = mapped_column(String(64))
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[ErpCustomer] = relationship()


class ErpRefund(Base):
    __tablename__ = "erp_refunds"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_ref: Mapped[str] = mapped_column(ForeignKey("erp_orders.ref"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ErpEscalation(Base):
    __tablename__ = "erp_escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    team: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    customer_email: Mapped[str] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
