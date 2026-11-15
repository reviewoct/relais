"""FastAPI application: JSON API, dashboard pages, and the mock ERP router."""

import json
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import actions, mock_erp, seed
from app.agent import log
from app.config import get_settings
from app.db import SessionLocal, engine, get_db
from app.erp_client import ERPClient
from app.jobs import Worker, enqueue
from app.llm import build_llm
from app.models import AiRequest, Base, Job, OutboxMessage, Ticket, TicketStatus
from app.schemas import InboundEmail

HERE = Path(__file__).parent
PARIS = ZoneInfo("Europe/Paris")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    Base.metadata.create_all(engine)
    if not hasattr(app.state, "llm"):
        app.state.llm = build_llm(settings)
    if not hasattr(app.state, "erp"):
        app.state.erp = ERPClient(settings.erp_base_url, settings.erp_token)
    with SessionLocal() as db:
        if settings.seed_demo_data:
            seed.seed_reference_data(db)
            if settings.seed_demo_tickets:
                seed.seed_tickets(db)
    worker = None
    if settings.worker_inline:
        worker = Worker(SessionLocal, lambda: app.state.llm, lambda: app.state.erp, settings)
        worker.start()
    yield
    if worker:
        worker.stop()


app = FastAPI(
    title="Relais",
    description="Bureau de support assisté par IA : un agent prépare, un humain valide.",
    version="0.1.0",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(mock_erp.router)
templates = Jinja2Templates(directory=HERE / "templates")

STATUS_LABELS = {
    "new": "En file",
    "processing": "En cours",
    "awaiting_approval": "À valider",
    "resolved": "Résolu",
    "manual": "Repris par un humain",
    "failed": "Échec",
}
ACTION_LABELS = {
    "pending": "En attente",
    "rejected": "Refusée",
    "executed": "Exécutée",
    "failed": "Échec",
}


def _paris(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:  # SQLite returns naive datetimes; they are stored in UTC
        value = value.replace(tzinfo=UTC)
    return value.astimezone(PARIS).strftime(fmt)


def _euros(cents: int | None) -> str:
    if cents is None:
        return "—"
    return f"{cents / 100:,.2f} €".replace(",", " ").replace(".", ",")


def _usd(micro: int | None) -> str:
    return "—" if micro is None else f"{micro / 1_000_000:.4f} $"


templates.env.filters.update(
    paris=_paris,
    euros=_euros,
    usd=_usd,
    pretty=lambda v: json.dumps(v, ensure_ascii=False, indent=2),
)
templates.env.globals.update(STATUS_LABELS=STATUS_LABELS, ACTION_LABELS=ACTION_LABELS)

# --- Access control ---------------------------------------------------------------

_basic = HTTPBasic(auto_error=False)


def current_user(creds: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """Dashboard user. With no password configured, the dashboard is open (local demo)."""
    s = get_settings()
    if not s.dashboard_password:
        return "conseiller démo"
    ok = creds is not None and secrets.compare_digest(
        f"{creds.username}:{creds.password}".encode(),
        f"{s.dashboard_user}:{s.dashboard_password}".encode(),
    )
    if not ok:
        raise HTTPException(401, "Authentification requise", {"WWW-Authenticate": "Basic"})
    return creds.username


def same_origin(request: Request) -> None:
    """Reject form posts sent from another site."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Origine refusée")


def _ticket_or_404(db: Session, ticket_id: int) -> Ticket:
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "Ticket introuvable")
    return ticket


def _ingest(db: Session, mail: InboundEmail) -> tuple[Ticket, bool]:
    """Create the ticket and queue it. The same message_id is accepted only once."""
    existing = db.scalar(select(Ticket).where(Ticket.message_id == mail.message_id))
    if existing is not None:
        return existing, False
    ticket = Ticket(**mail.model_dump())
    db.add(ticket)
    db.flush()
    enqueue(db, ticket)
    log(db, ticket, "système", "received", "E-mail reçu et mis en file")
    return ticket, True


def _page(request: Request, name: str, **context) -> HTMLResponse:
    context["ai_mode"] = get_settings().resolved_ai_mode
    return templates.TemplateResponse(request, name, context)


# --- JSON API -----------------------------------------------------------------------


@app.get("/health", tags=["api"])
def health(db: Session = Depends(get_db)) -> dict:
    queued = db.scalar(select(func.count()).select_from(Job).where(Job.status == "queued"))
    return {"status": "ok", "ai_mode": get_settings().resolved_ai_mode, "jobs_queued": queued}


@app.post("/api/inbound", status_code=202, tags=["api"])
def inbound(
    mail: InboundEmail,
    x_api_key: str = Header(default=""),
    db: Session = Depends(get_db),
) -> dict:
    """Webhook for the mail provider. Safe to call twice with the same message."""
    expected = get_settings().inbound_api_key
    if expected and not secrets.compare_digest(x_api_key.encode(), expected.encode()):
        raise HTTPException(401, "Clé API invalide")
    ticket, created = _ingest(db, mail)
    return {"ticket_id": ticket.id, "created": created, "status": ticket.status}


def _ticket_json(t: Ticket) -> dict:
    return {
        "id": t.id, "status": t.status, "from_email": t.from_email, "subject": t.subject,
        "category": t.category, "urgency": t.urgency, "summary": t.summary,
        "draft_body": t.draft_body, "confidence": t.confidence,
        "actions": [{"id": a.id, "kind": a.kind, "status": a.status, "payload": a.payload}
                    for a in t.actions],
    }


@app.get("/api/tickets", tags=["api"])
def api_tickets(db: Session = Depends(get_db), _: str = Depends(current_user)) -> list[dict]:
    rows = db.scalars(select(Ticket).order_by(Ticket.id.desc()).limit(100)).all()
    return [_ticket_json(t) for t in rows]


@app.get("/api/tickets/{ticket_id}", tags=["api"])
def api_ticket(
    ticket_id: int, db: Session = Depends(get_db), _: str = Depends(current_user)
) -> dict:
    t = _ticket_or_404(db, ticket_id)
    return {**_ticket_json(t),
            "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                        "title": e.title, "detail": e.detail} for e in t.events]}


# --- Dashboard ------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def inbox(request: Request, db: Session = Depends(get_db), user: str = Depends(current_user)):
    tickets = db.scalars(select(Ticket).order_by(Ticket.id.desc()).limit(100)).all()
    counts = dict(db.execute(select(Ticket.status, func.count()).group_by(Ticket.status)).all())
    spend = db.scalar(select(func.coalesce(func.sum(AiRequest.cost_micro_usd), 0)))
    busy = any(t.status in (TicketStatus.NEW, TicketStatus.PROCESSING) for t in tickets)
    return _page(request, "inbox.html", tickets=tickets, counts=counts, spend=spend, busy=busy,
                 user=user, samples=seed.SAMPLE_EMAILS)


@app.post("/simulate", include_in_schema=False, dependencies=[Depends(same_origin)])
def simulate(
    from_name: str = Form(""),
    from_email: str = Form(...),
    subject: str = Form(...),
    body: str = Form(...),
    db: Session = Depends(get_db),
    _: str = Depends(current_user),
):
    mail = InboundEmail(message_id=f"sim-{uuid.uuid4()}@relais.local", from_email=from_email,
                        from_name=from_name, subject=subject, body=body)
    ticket, _created = _ingest(db, mail)
    return RedirectResponse(f"/tickets/{ticket.id}", status_code=303)


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse, include_in_schema=False)
def ticket_page(
    ticket_id: int, request: Request, db: Session = Depends(get_db),
    user: str = Depends(current_user),
):
    ticket = _ticket_or_404(db, ticket_id)
    outbox = db.scalar(select(OutboxMessage).where(OutboxMessage.ticket_id == ticket.id))
    cost = db.scalar(select(func.coalesce(func.sum(AiRequest.cost_micro_usd), 0))
                     .where(AiRequest.ticket_id == ticket.id))
    calls = db.scalar(select(func.count()).select_from(AiRequest)
                      .where(AiRequest.ticket_id == ticket.id))
    busy = ticket.status in (TicketStatus.NEW, TicketStatus.PROCESSING)
    return _page(request, "ticket.html", t=ticket, outbox=outbox, cost=cost, calls=calls,
                 busy=busy, user=user, error=request.query_params.get("erreur"))


@app.post("/tickets/{ticket_id}/approve", include_in_schema=False,
          dependencies=[Depends(same_origin)])
def approve_ticket(
    ticket_id: int,
    request: Request,
    body: str = Form(...),
    action_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
):
    ticket = _ticket_or_404(db, ticket_id)
    try:
        ok = actions.approve(db, ticket, user, body, set(action_ids), request.app.state.erp)
    except actions.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    suffix = "" if ok else "?erreur=action"
    return RedirectResponse(f"/tickets/{ticket.id}{suffix}", status_code=303)


@app.post("/tickets/{ticket_id}/reject", include_in_schema=False,
          dependencies=[Depends(same_origin)])
def reject_ticket(
    ticket_id: int,
    reason: str = Form(""),
    db: Session = Depends(get_db),
    user: str = Depends(current_user),
):
    ticket = _ticket_or_404(db, ticket_id)
    try:
        actions.reject(db, ticket, user, reason)
    except actions.Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    db.commit()
    return RedirectResponse(f"/tickets/{ticket.id}", status_code=303)


@app.post("/tickets/{ticket_id}/retry", include_in_schema=False,
          dependencies=[Depends(same_origin)])
def retry_ticket(
    ticket_id: int, db: Session = Depends(get_db), user: str = Depends(current_user)
):
    ticket = _ticket_or_404(db, ticket_id)
    if ticket.status not in (TicketStatus.FAILED, TicketStatus.MANUAL):
        raise HTTPException(409, "Ce ticket ne peut pas être relancé.")
    ticket.status = TicketStatus.NEW
    enqueue(db, ticket)
    log(db, ticket, user, "requeued", "Relancé manuellement")
    return RedirectResponse(f"/tickets/{ticket.id}", status_code=303)


@app.get("/couts", response_class=HTMLResponse, include_in_schema=False)
def costs(request: Request, db: Session = Depends(get_db), user: str = Depends(current_user)):
    rows = db.scalars(select(AiRequest).order_by(AiRequest.id.desc()).limit(200)).all()
    totals = db.execute(
        select(func.count(), func.coalesce(func.sum(AiRequest.input_tokens), 0),
               func.coalesce(func.sum(AiRequest.output_tokens), 0),
               func.coalesce(func.sum(AiRequest.cost_micro_usd), 0))
    ).one()
    tickets_done = db.scalar(
        select(func.count(func.distinct(AiRequest.ticket_id))).where(AiRequest.ok.is_(True)))
    return _page(request, "costs.html", rows=rows, totals=totals, tickets_done=tickets_done,
                 user=user)
