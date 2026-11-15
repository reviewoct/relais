"""A job queue in a database table: claim, retry with backoff, dead-letter.

No broker to run. The claim is a conditional UPDATE, so two workers cannot take
the same job, on SQLite and PostgreSQL alike.
"""

import logging
import threading
from collections.abc import Callable
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app.agent import log, process_ticket
from app.config import Settings
from app.erp_client import ERPClient
from app.llm import LLM, PermanentLLMError
from app.models import Job, Ticket, TicketStatus, utcnow

logger = logging.getLogger("relais.jobs")
STALE_AFTER = timedelta(minutes=10)


def enqueue(db: Session, ticket: Ticket) -> Job:
    job = Job(ticket_id=ticket.id)
    db.add(job)
    return job


def claim(db: Session) -> Job | None:
    now = utcnow()
    # A worker that died mid-job leaves it "running": hand it back after a while.
    db.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < now - STALE_AFTER)
        .values(status="queued")
    )
    job_id = db.scalar(
        select(Job.id)
        .where(Job.status == "queued", Job.run_after <= now)
        .order_by(Job.id)
        .limit(1)
    )
    if job_id is None:
        db.commit()
        return None
    claimed = db.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", locked_at=now, attempts=Job.attempts + 1)
    )
    db.commit()
    return db.get(Job, job_id) if claimed.rowcount == 1 else None


def run_once(factory: sessionmaker, llm: LLM, erp: ERPClient, settings: Settings) -> bool:
    """Process at most one job. Returns False when the queue is empty."""
    with factory() as db:
        job = claim(db)
        if job is None:
            return False
        ticket = db.get(Ticket, job.ticket_id)
        try:
            process_ticket(db, ticket, llm, erp, settings)
            job.status = "done"
        except Exception as exc:  # noqa: BLE001 - every failure must land in the job row
            db.rollback()
            job, ticket = db.get(Job, job.id), db.get(Ticket, job.ticket_id)
            job.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            final = isinstance(exc, PermanentLLMError) or job.attempts >= settings.job_max_attempts
            if final:
                job.status = "dead"
                ticket.status = TicketStatus.FAILED
                log(db, ticket, "système", "error", "Traitement abandonné",
                    {"error": job.last_error})
            else:
                job.status = "queued"
                job.run_after = utcnow() + timedelta(seconds=5 * 2**job.attempts)
                ticket.status = TicketStatus.NEW
                log(db, ticket, "système", "retry",
                    f"Échec temporaire — nouvelle tentative ({job.attempts}/"
                    f"{settings.job_max_attempts})", {"error": job.last_error})
            logger.warning("job %s failed (final=%s): %s", job.id, final, job.last_error)
        db.commit()
        return True


class Worker(threading.Thread):
    def __init__(
        self,
        factory: sessionmaker,
        get_llm: Callable[[], LLM],
        get_erp: Callable[[], ERPClient],
        settings: Settings,
    ):
        super().__init__(daemon=True, name="relais-worker")
        self.factory, self.settings = factory, settings
        self.get_llm, self.get_erp = get_llm, get_erp
        self._halt = threading.Event()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                worked = run_once(self.factory, self.get_llm(), self.get_erp(), self.settings)
            except Exception:  # noqa: BLE001 - the loop must survive a bad iteration
                logger.exception("worker iteration failed")
                worked = False
            if not worked:
                self._halt.wait(self.settings.worker_poll_seconds)

    def stop(self) -> None:
        self._halt.set()
