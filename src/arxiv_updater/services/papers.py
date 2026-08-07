import re
import unicodedata
from dataclasses import dataclass

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..models import Paper, PaperSource, utcnow
from ..sources import PaperCandidate


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).lower()
    value = "".join(character for character in value if not unicodedata.combining(character))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def normalize_doi(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower().removeprefix("https://doi.org/").removeprefix("doi:")


@dataclass(slots=True)
class UpsertResult:
    paper: Paper
    created: bool


def find_existing_paper(db: Session, candidate: PaperCandidate) -> Paper | None:
    if candidate.arxiv_id:
        paper = db.scalar(select(Paper).where(Paper.arxiv_id == candidate.arxiv_id))
        if paper:
            return paper
    doi = normalize_doi(candidate.doi)
    if doi:
        paper = db.scalar(select(Paper).where(Paper.doi == doi))
        if paper:
            return paper
    if candidate.scholar_citation_id:
        paper = db.scalar(
            select(Paper).where(Paper.scholar_citation_id == candidate.scholar_citation_id)
        )
        if paper:
            return paper
    normalized = normalize_title(candidate.title)
    first_author = candidate.authors[0].strip().lower() if candidate.authors else ""
    published_year = candidate.published_at.year if candidate.published_at else None
    conditions = [Paper.normalized_title == normalized, Paper.first_author == first_author]
    if published_year:
        conditions.append(Paper.published_at.is_not(None))
    candidates = db.scalars(select(Paper).where(and_(*conditions)).limit(5)).all()
    for paper in candidates:
        if not published_year or (paper.published_at and paper.published_at.year == published_year):
            return paper
    return None


def upsert_paper(db: Session, candidate: PaperCandidate) -> UpsertResult:
    paper = find_existing_paper(db, candidate)
    created = paper is None
    normalized_doi = normalize_doi(candidate.doi)
    if paper is None:
        authors_text = ", ".join(candidate.authors)
        abstract = candidate.abstract.strip()
        paper = Paper(
            title=candidate.title.strip(),
            normalized_title=normalize_title(candidate.title),
            abstract=abstract,
            abstract_source=candidate.source if abstract else "",
            abstract_status="available" if abstract else "missing",
            abstract_checked_at=utcnow() if abstract else None,
            authors_text=authors_text,
            first_author=candidate.authors[0].strip().lower() if candidate.authors else "",
            published_at=candidate.published_at,
            updated_at=candidate.updated_at,
            arxiv_id=candidate.arxiv_id,
            doi=normalized_doi,
            scholar_citation_id=candidate.scholar_citation_id,
            canonical_url=candidate.canonical_url,
            pdf_url=candidate.pdf_url,
            categories=candidate.categories,
        )
        db.add(paper)
        db.flush()
    else:
        if candidate.abstract and not paper.abstract:
            paper.abstract = candidate.abstract.strip()
            paper.abstract_source = candidate.source
            paper.abstract_status = "available"
            paper.abstract_checked_at = utcnow()
        if candidate.arxiv_id and not paper.arxiv_id:
            paper.arxiv_id = candidate.arxiv_id
        if normalized_doi and not paper.doi:
            paper.doi = normalized_doi
        if candidate.scholar_citation_id and not paper.scholar_citation_id:
            paper.scholar_citation_id = candidate.scholar_citation_id
        if candidate.pdf_url and not paper.pdf_url:
            paper.pdf_url = candidate.pdf_url
        if candidate.canonical_url and not paper.canonical_url:
            paper.canonical_url = candidate.canonical_url
        paper.categories = sorted(set(paper.categories or []) | set(candidate.categories))
        if candidate.updated_at and (
            not paper.updated_at or candidate.updated_at > paper.updated_at
        ):
            paper.updated_at = candidate.updated_at

    source = db.scalar(
        select(PaperSource).where(
            PaperSource.source == candidate.source,
            PaperSource.external_id == candidate.external_id,
        )
    )
    if source is None:
        db.add(
            PaperSource(
                paper_id=paper.id,
                source=candidate.source,
                external_id=candidate.external_id,
                url=candidate.canonical_url,
                metadata_json=candidate.metadata,
            )
        )
    else:
        source.last_seen_at = utcnow()
        source.url = candidate.canonical_url or source.url
        source.metadata_json = candidate.metadata or source.metadata_json
    return UpsertResult(paper=paper, created=created)
