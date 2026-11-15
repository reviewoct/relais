"""Standalone worker process: `python -m app.worker`. Used by the Docker setup."""

import logging
import signal

from app.config import get_settings
from app.db import SessionLocal, engine
from app.erp_client import ERPClient
from app.jobs import Worker
from app.llm import build_llm
from app.models import Base


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    settings = get_settings()
    Base.metadata.create_all(engine)
    llm = build_llm(settings)
    erp = ERPClient(settings.erp_base_url, settings.erp_token)
    worker = Worker(SessionLocal, lambda: llm, lambda: erp, settings)
    signal.signal(signal.SIGTERM, lambda *_: worker.stop())
    logging.getLogger("relais.worker").info("worker started (ai_mode=%s)",
                                            settings.resolved_ai_mode)
    worker.start()
    try:
        while worker.is_alive():
            worker.join(timeout=1.0)
    except KeyboardInterrupt:
        worker.stop()


if __name__ == "__main__":
    main()
