"""Test setup: a throwaway SQLite file, no background worker, no real model.

The database, the HTTP layer, the mock ERP and the tool guardrails are all real.
Only the model is replaced, by a script whose every move the test controls.
"""

import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="relais-test-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_TMP}/test.db",
    AI_MODE="demo",
    ANTHROPIC_API_KEY="",
    WORKER_INLINE="false",
    SEED_DEMO_TICKETS="false",
    INBOUND_API_KEY="",
    DASHBOARD_PASSWORD="",
    ERP_TOKEN="test-erp-token",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.erp_client import ERPClient  # noqa: E402
from app.jobs import run_once  # noqa: E402
from app.llm import ToolCall, Turn, Usage  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.schemas import Classification  # noqa: E402

CLAIRE = {
    "message_id": "m-1@test",
    "from_email": "claire.moreau@example.fr",
    "from_name": "Claire Moreau",
    "subject": "Lampe reçue cassée — commande CMD-10428",
    "body": "Bonjour, ma lampe CMD-10428 est arrivée cassée. Je souhaite être remboursée.",
}


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    with TestClient(app) as c:  # lifespan creates the tables and seeds the reference data
        yield c


@pytest.fixture
def erp(client):
    # The ERP client talks HTTP to the mock ERP mounted in the same app.
    erp = ERPClient("http://testserver/mock-erp", "test-erp-token", client=client,
                    sleep=lambda _s: None)
    app.state.erp = erp
    return erp


@pytest.fixture
def db(client):
    with SessionLocal() as session:
        yield session


def turn(*calls: tuple[str, dict], text: str = "") -> Turn:
    """Build one scripted model turn that requests the given tool calls."""
    tool_calls = [ToolCall(f"call_{name}_{i}", name, args) for i, (name, args) in enumerate(calls)]
    blocks = [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}
              for c in tool_calls]
    return Turn(text, tool_calls, "tool_use" if calls else "end_turn", blocks, "scripted",
                Usage(100, 20))


REPLY = ("submit_reply", {
    "subject": "Re: votre demande",
    "body": "Bonjour, nous avons bien pris en compte votre demande et revenons vers vous.",
    "internal_summary": "Dossier vérifié.",
    "confidence": 0.8,
})


class ScriptedLLM:
    provider = "scripted"

    def __init__(self, *turns: Turn, category: str = "remboursement"):
        self.turns = list(turns)
        self.category = category
        self.seen: list[list[dict]] = []

    def classify(self, subject, body):
        cls = Classification(category=self.category, urgency="normale", language="fr",
                             sentiment="neutre", summary="résumé", order_ref=None)
        return cls, Usage(50, 10), "scripted"

    def step(self, system, tools, messages):
        self.seen.append([dict(m) for m in messages])
        if not self.turns:
            return turn()
        nxt = self.turns.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def run(erp):
    def _run(llm) -> bool:
        return run_once(SessionLocal, llm, erp, get_settings())

    return _run


@pytest.fixture
def send(client):
    def _send(**overrides) -> int:
        resp = client.post("/api/inbound", json={**CLAIRE, **overrides})
        assert resp.status_code == 202, resp.text
        return resp.json()["ticket_id"]

    return _send
