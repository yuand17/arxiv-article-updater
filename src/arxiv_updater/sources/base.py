from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(slots=True)
class PaperCandidate:
    source: str
    external_id: str
    title: str
    authors: list[str]
    abstract: str = ""
    published_at: datetime | None = None
    updated_at: datetime | None = None
    arxiv_id: str | None = None
    doi: str | None = None
    scholar_citation_id: str | None = None
    categories: list[str] = field(default_factory=list)
    canonical_url: str | None = None
    pdf_url: str | None = None
    metadata: dict = field(default_factory=dict)


class SourceAdapter(ABC):
    name: str

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> list[PaperCandidate]:
        """Fetch source records without writing application state."""

