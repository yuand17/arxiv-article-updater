"""Deterministic abstract enrichment from already-identified first-party sources."""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote, urlparse

import httpx
from bs4 import BeautifulSoup
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..journal_network import get_journal_network
from ..models import Paper, utcnow
from ..sources.arxiv import ARXIV_QUERY_URL, parse_arxiv_feed
from ..sources.journals import clean_crossref_abstract
from .papers import normalize_doi


@dataclass(frozen=True, slots=True)
class AbstractMatch:
    abstract: str
    source: str
    confidence: float


_APS_FEED_FOOTER = re.compile(r"\[[^\]]+\]\s+Published\s+.+$", re.I)
_PUBLISHER_FEED_PREFIX = re.compile(r"^.{2,80},\s+Published online:", re.I)


def abstract_needs_enrichment(paper: Paper) -> bool:
    """Identify missing abstracts and known publisher-feed teasers."""

    abstract = paper.abstract.strip()
    if not abstract:
        return True
    if paper.abstract_source != "journal":
        return False
    return bool(
        abstract.startswith("Author(s):")
        or _PUBLISHER_FEED_PREFIX.match(abstract)
        or _APS_FEED_FOOTER.search(abstract)
        or abstract.endswith("…")
    )


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
        if abstract_needs_enrichment(candidate):
            continue
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


def _crossref_abstract(doi: str, client: httpx.Client) -> str:
    normalized_doi = normalize_doi(doi)
    if not normalized_doi:
        return ""
    response = client.get(
        f"https://api.crossref.org/works/{quote(normalized_doi, safe='/')}",
        headers={"User-Agent": "arxiv-updater/0.2 (mailto:local@localhost)"},
    )
    if response.status_code == 404:
        return ""
    response.raise_for_status()
    payload = response.json()
    abstract = str((payload.get("message") or {}).get("abstract") or "")
    return clean_crossref_abstract(abstract)


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
    for meta_name in ("citation_abstract", "dc.description", "dc:description"):
        node = soup.find(
            "meta", attrs={"name": re.compile(f"^{re.escape(meta_name)}$", re.I)}
        )
        abstract = str(node.get("content") or "").strip() if node else ""
        if abstract:
            return abstract
    return ""


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
    if not abstract_needs_enrichment(paper):
        paper.abstract_status = "available"
        paper.abstract_checked_at = now
        db.commit()
        return paper

    original_abstract = paper.abstract
    original_source = paper.abstract_source
    paper.abstract_status = "pending"
    db.commit()
    try:
        local = _local_match(db, paper)
        if local:
            _apply_match(paper, local, now)
            db.commit()
            return paper

        client = http_client or get_journal_network().client
        if paper.arxiv_id:
            abstract = _arxiv_abstract(paper.arxiv_id, client)
            if abstract:
                _apply_match(paper, AbstractMatch(abstract, "arxiv", 1.0), now)
                db.commit()
                return paper
        if paper.doi:
            abstract = _crossref_abstract(paper.doi, client)
            if abstract:
                _apply_match(paper, AbstractMatch(abstract, "crossref", 1.0), now)
                db.commit()
                return paper
        if paper.canonical_url:
            abstract = _citation_meta_abstract(paper.canonical_url, client)
            if abstract:
                _apply_match(paper, AbstractMatch(abstract, "citation-meta", 0.95), now)
                db.commit()
                return paper
        paper.abstract = original_abstract
        paper.abstract_source = original_source
        paper.abstract_status = "available" if original_abstract.strip() else "missing"
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
