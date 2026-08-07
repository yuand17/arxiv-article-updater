"""Trusted abstract enrichment for papers imported from Scholar without abstracts."""

import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from rapidfuzz.fuzz import ratio
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..db import SessionLocal
from ..models import Paper, PaperSource, utcnow
from ..sources.arxiv import parse_arxiv_feed
from .papers import normalize_doi, normalize_title

SEMANTIC_SCHOLAR_MATCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search/match"
ARXIV_QUERY_URL = "https://export.arxiv.org/api/query"


@dataclass(frozen=True, slots=True)
class AbstractMatch:
    abstract: str
    source: str
    confidence: float
    semantic_scholar_id: str | None = None


class SemanticScholarClient:
    def __init__(
        self, settings: Settings | None = None, client: httpx.Client | None = None
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client or httpx.Client(timeout=20, follow_redirects=True)

    def match_title(self, title: str) -> dict | None:
        headers = {"User-Agent": "arxiv-updater/0.2 (personal research library)"}
        if self.settings.semantic_scholar_api_key:
            headers["x-api-key"] = self.settings.semantic_scholar_api_key
        response = self.client.get(
            SEMANTIC_SCHOLAR_MATCH_URL,
            params={
                "query": title,
                "fields": "title,abstract,authors,year,externalIds",
            },
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"][0] if payload["data"] else None
        return payload if isinstance(payload, dict) and payload.get("title") else None


def _author_surnames(value: str | list[dict] | list[str] | None) -> set[str]:
    if isinstance(value, list):
        names = [
            str(item.get("name", "")) if isinstance(item, dict) else str(item) for item in value
        ]
    else:
        names = str(value or "").split(",")
    result: set[str] = set()
    for name in names:
        tokens = re.findall(r"[\w'-]+", name.lower())
        if tokens:
            result.add(tokens[-1])
    return result


def _paper_year(paper: Paper) -> int | None:
    date = paper.published_at or paper.discovered_at
    return date.year if date else None


def _external_id(payload: dict, *names: str) -> str | None:
    ids = payload.get("externalIds") or {}
    if not isinstance(ids, dict):
        return None
    for name in names:
        value = ids.get(name)
        if value:
            return str(value).strip()
    return None


def _candidate_confidence(paper: Paper, payload: dict) -> float:
    candidate_doi = normalize_doi(_external_id(payload, "DOI"))
    candidate_arxiv = _external_id(payload, "ArXiv", "arXiv")
    if paper.doi and candidate_doi and normalize_doi(paper.doi) == candidate_doi:
        return 1.0
    if (
        paper.arxiv_id
        and candidate_arxiv
        and paper.arxiv_id.removeprefix("arXiv:") == candidate_arxiv.removeprefix("arXiv:")
    ):
        return 1.0
    candidate_title = str(payload.get("title") or "")
    title_similarity = ratio(normalize_title(paper.title), normalize_title(candidate_title)) / 100
    paper_year = _paper_year(paper)
    try:
        candidate_year = int(str(payload.get("year") or ""))
        year_gap = abs(candidate_year - (paper_year or candidate_year))
    except (TypeError, ValueError):
        year_gap = 99
    authors_overlap = bool(
        _author_surnames(paper.authors_text) & _author_surnames(payload.get("authors"))
    )
    return (
        title_similarity if title_similarity >= 0.95 and authors_overlap and year_gap <= 1 else 0.0
    )


def _local_match(db: Session, paper: Paper) -> AbstractMatch | None:
    conditions = []
    if paper.arxiv_id:
        conditions.append(Paper.arxiv_id == paper.arxiv_id)
    if paper.doi:
        conditions.append(Paper.doi == normalize_doi(paper.doi))
    if not conditions:
        conditions.append(Paper.normalized_title == paper.normalized_title)
    candidates = db.scalars(
        select(Paper).where(
            Paper.id != paper.id,
            Paper.abstract != "",
            or_(*conditions),
        )
    ).all()
    for candidate in candidates:
        if paper.arxiv_id and candidate.arxiv_id == paper.arxiv_id:
            return AbstractMatch(candidate.abstract, "local-arxiv", 1.0)
        if paper.doi and normalize_doi(candidate.doi) == normalize_doi(paper.doi):
            return AbstractMatch(candidate.abstract, "local-doi", 1.0)
        if (
            candidate.normalized_title == paper.normalized_title
            and _author_surnames(candidate.authors_text) & _author_surnames(paper.authors_text)
            and abs((_paper_year(candidate) or 0) - (_paper_year(paper) or 0)) <= 1
        ):
            return AbstractMatch(candidate.abstract, "local-title", 0.97)
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
    if match.semantic_scholar_id:
        paper.semantic_scholar_id = match.semantic_scholar_id


def enrich_paper_abstract(
    db: Session,
    paper_id: str,
    *,
    semantic_client: SemanticScholarClient | None = None,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> Paper | None:
    """Fill one missing abstract only after a conservative identity match."""

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
        matcher = semantic_client or SemanticScholarClient(client=client)
        payload = matcher.match_title(paper.title)
        if payload:
            confidence = _candidate_confidence(paper, payload)
            semantic_id = str(payload.get("paperId") or "").strip() or None
            abstract = str(payload.get("abstract") or "").strip()
            if confidence and abstract:
                _apply_match(
                    paper,
                    AbstractMatch(abstract, "semantic-scholar", confidence, semantic_id),
                    now,
                )
                db.commit()
                return paper
            candidate_arxiv = _external_id(payload, "ArXiv", "arXiv")
            if confidence and candidate_arxiv:
                abstract = _arxiv_abstract(candidate_arxiv, client)
                if abstract:
                    _apply_match(
                        paper,
                        AbstractMatch(
                            abstract, "arxiv-via-semantic-scholar", confidence, semantic_id
                        ),
                        now,
                    )
                    db.commit()
                    return paper

        if paper.canonical_url:
            meta_abstract = _citation_meta_abstract(paper.canonical_url, client)
            if meta_abstract:
                _apply_match(paper, AbstractMatch(meta_abstract, "citation-meta", 0.95), now)
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


def enrich_missing_scholar_abstracts(db: Session, *, limit: int = 10) -> int:
    """Try a bounded number after a Scholar sync; failures never abort that sync."""

    paper_ids = db.scalars(
        select(Paper.id)
        .join(PaperSource)
        .where(
            PaperSource.source == "scholar",
            Paper.abstract == "",
            Paper.abstract_status.in_(("missing", "pending", "failed")),
        )
        .order_by(Paper.discovered_at.desc())
        .limit(limit)
    ).all()
    for paper_id in paper_ids:
        enrich_paper_abstract(db, paper_id)
    return len(paper_ids)
