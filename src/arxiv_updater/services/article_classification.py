"""Deterministic, auditable journal article classification."""

import re
from dataclasses import dataclass, field
from datetime import datetime

from ..models import utcnow
from ..sources.base import PaperCandidate

CLASSIFICATION_VERSION = "journal-rules-v1"

EXCLUDED_TYPE_TERMS = {
    "book review",
    "career",
    "comment",
    "commentary",
    "correspondence",
    "editorial",
    "erratum",
    "expression of concern",
    "interview",
    "magazine",
    "news",
    "news & views",
    "opinion",
    "perspective",
    "podcast",
    "publisher correction",
    "author correction",
    "correction",
    "research highlight",
    "retraction",
    "retraction note",
    "review",
    "review article",
    "systematic review",
}
ORIGINAL_TYPE_TERMS = {
    "article",
    "letter",
    "original article",
    "original research",
    "rapid communication",
    "research article",
    "research-article",
    "research letter",
}
PHYSICS_JOURNAL_TERMS = {
    "astrophys",
    "condensed matter",
    "computational physics",
    "optics",
    "physical review",
    "physics",
    "quantum",
}
PHYSICS_TERMS = {
    "ads/cft",
    "astrophys",
    "atomic physics",
    "biophysics",
    "black hole",
    "boson",
    "condensed matter",
    "cosmolog",
    "density functional",
    "electromagnet",
    "entanglement",
    "fermion",
    "field theory",
    "fluid dynamics",
    "gravit",
    "hadron",
    "hamiltonian",
    "high energy physics",
    "laser",
    "many-body",
    "material physics",
    "materials physics",
    "mathematical physics",
    "molecular physics",
    "nuclear physics",
    "optical",
    "particle physics",
    "phase transition",
    "photon",
    "plasma",
    "quantum",
    "relativ",
    "soft matter",
    "spin",
    "statistical mechanics",
    "superconduct",
    "topological",
}


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    document_type: str
    is_original_research: bool
    is_physics: bool
    physics_confidence: float
    reason: str
    source: str
    version: str = CLASSIFICATION_VERSION
    classified_at: datetime = field(default_factory=utcnow)

    @property
    def accepted(self) -> bool:
        return self.is_original_research and self.is_physics

    @property
    def outcome(self) -> str:
        if not self.is_original_research:
            return "nonresearch"
        if not self.is_physics:
            return "nonphysics"
        return "imported"


def _normalized(value: object) -> str:
    return " ".join(str(value or "").lower().split())


def _type_evidence(candidate: PaperCandidate, journal_name: str) -> tuple[str, bool, str]:
    raw_type = _normalized(
        candidate.metadata.get("document_type")
        or candidate.metadata.get("content_type")
        or candidate.metadata.get("type")
    )
    title = _normalized(candidate.title)
    url = _normalized(candidate.canonical_url)

    for excluded in sorted(EXCLUDED_TYPE_TERMS, key=len, reverse=True):
        if excluded == raw_type or excluded in raw_type:
            return raw_type or excluded, False, "publisher_type"
        if re.search(rf"\b{re.escape(excluded)}\b", title):
            return raw_type or excluded, False, "title_exclusion"

    journal = _normalized(journal_name)
    if "physical review letters" in journal or journal == "prl":
        if any(
            term in raw_type or re.search(rf"\b{re.escape(term)}\b", title)
            for term in EXCLUDED_TYPE_TERMS
        ):
            return raw_type or "excluded", False, "prl_exclusion"
        return raw_type or "letter", True, "prl_scope"

    if raw_type in ORIGINAL_TYPE_TERMS or any(
        raw_type.startswith(f"{kind} ") for kind in ORIGINAL_TYPE_TERMS
    ):
        return raw_type, True, "publisher_type"
    if "nature.com/articles/d" in url:
        return raw_type or "editorial_content", False, "nature_editorial_path"
    if re.search(r"nature\.com/articles/s\d", url):
        return raw_type or "article", True, "nature_research_path"
    if "/articles/" in url or "/article/" in url:
        return raw_type or "article", True, "publisher_article_path"
    return raw_type or "unknown", False, "insufficient_type_evidence"


def _physics_evidence(
    candidate: PaperCandidate, journal_name: str, scope_kind: str
) -> tuple[bool, float, str]:
    journal = _normalized(journal_name)
    if scope_kind == "physics" or any(term in journal for term in PHYSICS_JOURNAL_TERMS):
        return True, 0.99, "specialist_physics_journal"

    metadata_text = " ".join(
        _normalized(value)
        for value in (
            candidate.categories,
            candidate.metadata.get("subjects"),
            candidate.metadata.get("section"),
            candidate.metadata.get("disciplines"),
        )
    )
    text = f"{metadata_text} {_normalized(candidate.title)} {_normalized(candidate.abstract)}"
    matches = sorted(term for term in PHYSICS_TERMS if term in text)
    if any(term in metadata_text for term in PHYSICS_TERMS):
        return True, 0.96, "publisher_subject:" + ",".join(matches[:3])
    if len(matches) >= 2:
        return True, min(0.95, 0.75 + len(matches) * 0.05), "physics_terms:" + ",".join(
            matches[:4]
        )
    return False, 0.0, "insufficient_physics_evidence"


def infer_journal_scope(name: str) -> str:
    journal = _normalized(name)
    return "physics" if any(term in journal for term in PHYSICS_JOURNAL_TERMS) else "general"


def classify_journal_candidate(
    candidate: PaperCandidate,
    *,
    journal_name: str,
    scope_kind: str,
) -> ClassificationResult:
    document_type, original, type_source = _type_evidence(candidate, journal_name)
    physics, confidence, physics_reason = _physics_evidence(
        candidate, journal_name, scope_kind
    )
    reason = f"type={type_source}; physics={physics_reason}"
    return ClassificationResult(
        document_type=document_type,
        is_original_research=original,
        is_physics=physics,
        physics_confidence=confidence,
        reason=reason,
        source="deterministic_journal_rules",
    )
