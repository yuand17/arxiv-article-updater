"""Deterministic abstract enrichment from already-identified first-party sources."""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import Paper, utcnow
from ..sources.arxiv import ARXIV_QUERY_URL, parse_arxiv_feed
from .papers import normalize_doi


@dataclass(frozen=True, slots=True)
class AbstractMatch:
    abstract: str
    source: str
    confidence: float


def _local_match(db: Session, paper: Paper) -> AbstractMatch | None:
    """Reuse only exact arXiv-ID or DOI matches; title search is intentionally excluded."""

    conditions = []
    if paper.arxiv_id:
        conditions.append(Paper.arxiv_id == paper.arxiv_id)
    if paper.doi:
        conditions.append(Paper.doi == normalize_doi(paper.doi))
    if not conditions:
        return None
    candidates = db.scalars(
        select(Paper).where(Paper.id != paper.id, Paper.abstract != "", or_(*conditions))
    ).all()
    for candidate in candidates:
        if paper.arxiv_id and candidate.arxiv_id == paper.arxiv_id:
            return AbstractMatch(candidate.abstract, "local-arxiv", 1.0)
        if paper.doi and normalize_doi(candidate.doi) == normalize_doi(paper.doi):
            return AbstractMatch(candidate.abstract, "local-doi", 1.0)
    return None


def _arxiv_abstract(arxiv_id: str, client: httpx.Client) -> str:
    response = client.get(
        ARXIV_QUERY_URL,
        params={"id_list": arxiv_id},
        headers={"User-Agent": "arxiv-updater/0.2 (personal research library)"},
    )
    response.raise_for_status()
    papers = parse_arxiv_feed(response.text)
    return papers[0].abstract.strip() if papers else ""


def _citation_meta_abstract(url: str, client: httpx.Client) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    response = client.get(
        url,
        headers={"User-Agent": "arxiv-updater/0.2 (personal research library)"},
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    node = soup.find("meta", attrs={"name": re.compile(r"^citation_abstract$", re.I)})
    return str(node.get("content") or "").strip() if node else ""


def _apply_match(paper: Paper, match: AbstractMatch, now: datetime) -> None:
    paper.abstract = match.abstract.strip()
    paper.abstract_source = match.source
    paper.abstract_match_confidence = match.confidence
    paper.abstract_status = "available"
    paper.abstract_checked_at = now


def enrich_paper_abstract(
    db: Session,
    paper_id: str,
    *,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> Paper | None:
    """Fill one missing abstract without fuzzy identity search or a hidden metadata API."""

    paper = db.get(Paper, paper_id)
    if paper is None:
        return None
    now = now or utcnow()
    if paper.abstract.strip():
        paper.abstract_status = "available"
        paper.abstract_checked_at = now
        db.commit()
        return paper

    paper.abstract_status = "pending"
    db.commit()
    try:
        local = _local_match(db, paper)
        if local:
            _apply_match(paper, local, now)
            db.commit()
            return paper

        client = http_client or httpx.Client(timeout=20, follow_redirects=True)
        if paper.arxiv_id:
            abstract = _arxiv_abstract(paper.arxiv_id, client)
            if abstract:
                _apply_match(paper, AbstractMatch(abstract, "arxiv", 1.0), now)
                db.commit()
                return paper
        if paper.canonical_url:
            abstract = _citation_meta_abstract(paper.canonical_url, client)
            if abstract:
                _apply_match(paper, AbstractMatch(abstract, "citation-meta", 0.95), now)
                db.commit()
                return paper
        paper.abstract_status = "missing"
        paper.abstract_checked_at = now
        db.commit()
    except httpx.HTTPStatusError as exc:
        paper.abstract_status = "pending" if exc.response.status_code == 429 else "failed"
        paper.abstract_checked_at = now
        db.commit()
    except Exception:
        paper.abstract_status = "failed"
        paper.abstract_checked_at = now
        db.commit()
    return paper


def enrich_paper_abstract_in_background(paper_id: str) -> None:
    with SessionLocal() as db:
        enrich_paper_abstract(db, paper_id)
