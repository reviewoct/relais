"""Keyword search over the policy articles.

Small on purpose: a dozen policy pages do not need embeddings. The function is
the seam where a vector index would plug in for a larger knowledge base.
"""

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import KbArticle

_STOP = set(
    "a au aux avec ce ces dans de des du elle en est et il je la le les ma mais me mes mon ne "
    "nous on ou par pas pour que qui sa se ses son sur un une vos votre vous the of to and "
    "is my for".split()
)


def tokens(text: str) -> list[str]:
    flat = unicodedata.normalize("NFKD", text.lower())
    flat = "".join(c for c in flat if not unicodedata.combining(c))
    return [t for t in re.findall(r"[a-z0-9]{3,}", flat) if t not in _STOP]


def search(db: Session, query: str, limit: int = 2) -> list[dict]:
    q = set(tokens(query))
    scored = []
    for art in db.scalars(select(KbArticle)).all():
        title, body = tokens(art.title), tokens(art.body)
        # Prefix match so "remboursement" finds "rembourser"; title hits weigh more.
        score = sum(
            3 * any(t[:6] == w[:6] for t in title) + sum(1 for b in body if b[:6] == w[:6])
            for w in q
        )
        if score:
            scored.append((score, art))
    scored.sort(key=lambda pair: -pair[0])
    return [{"slug": a.slug, "title": a.title, "text": a.body} for _, a in scored[:limit]]
