import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def new_id() -> str:
    return str(uuid.uuid4())


def utcnow() -> datetime:
    return datetime.now(UTC)


class InteractionKind(StrEnum):
    """Explicit single-user reading signals used by the preference profile."""

    ABSTRACT_VIEWED = "abstract_viewed"
    SAVED = "saved"
    FULLTEXT = "fulltext"
    DISMISSED = "dismissed"


class SyncStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text)
    normalized_title: Mapped[str] = mapped_column(Text, index=True)
    abstract: Mapped[str] = mapped_column(Text, default="")
    abstract_source: Mapped[str] = mapped_column(String(50), default="")
    abstract_match_confidence: Mapped[float | None] = mapped_column(Float)
    abstract_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    abstract_status: Mapped[str] = mapped_column(String(20), default="missing", index=True)
    authors_text: Mapped[str] = mapped_column(Text, default="")
    first_author: Mapped[str] = mapped_column(String(255), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )
    arxiv_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    doi: Mapped[str | None] = mapped_column(String(255), unique=True)
    scholar_citation_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    categories: Mapped[list[str]] = mapped_column(JSON, default=list)
    scites_count: Mapped[int] = mapped_column(Integer, default=0)
    is_scirate_hot: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    document_type: Mapped[str] = mapped_column(String(80), default="")
    is_original_research: Mapped[bool | None] = mapped_column(Boolean)
    is_physics: Mapped[bool | None] = mapped_column(Boolean)
    physics_confidence: Mapped[float | None] = mapped_column(Float)
    classification_reason: Mapped[str] = mapped_column(Text, default="")
    classification_source: Mapped[str] = mapped_column(String(80), default="")
    classification_version: Mapped[str] = mapped_column(String(40), default="")
    classified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    sources: Mapped[list["PaperSource"]] = relationship(back_populates="paper")
    interactions: Mapped[list["Interaction"]] = relationship(back_populates="paper")

    __table_args__ = (Index("idx_papers_published_discovered", "published_at", "discovered_at"),)


class PaperSource(Base):
    __tablename__ = "paper_sources"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(50), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    paper: Mapped[Paper] = relationship(back_populates="sources")
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_source_external_id"),)


class TrackedAuthor(Base):
    __tablename__ = "tracked_authors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scholar_author_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), default="Unknown author")
    profile_url: Mapped[str] = mapped_column(Text)
    citation_count: Mapped[int] = mapped_column(Integer, default=0)
    citation_count_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class JournalSubscription(Base):
    __tablename__ = "journal_subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(255))
    homepage_url: Mapped[str] = mapped_column(Text, unique=True)
    canonical_domain: Mapped[str] = mapped_column(String(255), index=True)
    issn_online: Mapped[str] = mapped_column(String(32), default="")
    issn_print: Mapped[str] = mapped_column(String(32), default="")
    scope_kind: Mapped[str] = mapped_column(String(20), default="general")
    discovery_status: Mapped[str] = mapped_column(String(30), default="pending")
    discovery_version: Mapped[str] = mapped_column(String(40), default="")
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_items_seen: Mapped[int] = mapped_column(Integer, default=0)
    last_items_imported: Mapped[int] = mapped_column(Integer, default=0)
    last_nonresearch_filtered: Mapped[int] = mapped_column(Integer, default=0)
    last_nonphysics_filtered: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    endpoints: Mapped[list["JournalEndpoint"]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan"
    )


class JournalEndpoint(Base):
    __tablename__ = "journal_endpoints"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    journal_subscription_id: Mapped[str] = mapped_column(
        ForeignKey("journal_subscriptions.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    url: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=100)
    config_json: Mapped[dict] = mapped_column(JSON, default=dict)
    last_validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")

    subscription: Mapped[JournalSubscription] = relationship(back_populates="endpoints")
    __table_args__ = (
        UniqueConstraint("journal_subscription_id", "url", name="uq_journal_endpoint_url"),
    )


class Interaction(Base):
    __tablename__ = "interactions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    kind: Mapped[InteractionKind] = mapped_column(Enum(InteractionKind), index=True)
    weight: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )

    paper: Mapped[Paper] = relationship(back_populates="interactions")
    __table_args__ = (UniqueConstraint("paper_id", "kind", name="uq_interaction_paper_kind"),)


class AppPreferences(Base):
    """The one and only local reader's manually entered and inferred preferences."""

    __tablename__ = "app_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    manual_interests: Mapped[str] = mapped_column(Text, default="")
    profile_summary: Mapped[str] = mapped_column(Text, default="")
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    profile_model: Mapped[str] = mapped_column(String(120), default="")
    profile_prompt_version: Mapped[str] = mapped_column(String(30), default="")
    profile_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_interaction_count: Mapped[int] = mapped_column(Integer, default=0)
    profile_dirty_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    featured_paper_count: Mapped[int] = mapped_column(Integer, default=66)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class SourceSchedule(Base):
    __tablename__ = "source_schedules"

    source: Mapped[str] = mapped_column(String(50), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_days: Mapped[int] = mapped_column(Integer)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RecommendationBatch(Base):
    __tablename__ = "recommendation_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    profile_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    model: Mapped[str] = mapped_column(String(120), default="")
    prompt_version: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(30), default="success", index=True)
    fallback_used: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str] = mapped_column(Text, default="")
    requested_count: Mapped[int] = mapped_column(Integer, default=66)
    candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    shortlist_count: Mapped[int] = mapped_column(Integer, default=0)
    rerank_success_count: Mapped[int] = mapped_column(Integer, default=0)
    rerank_fallback_count: Mapped[int] = mapped_column(Integer, default=0)
    selected_count: Mapped[int] = mapped_column(Integer, default=0)
    filtered_count: Mapped[int] = mapped_column(Integer, default=0)
    source_stats_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stale_sources_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    ranking_version: Mapped[str] = mapped_column(String(40), default="")

    items: Mapped[list["RecommendationItem"]] = relationship(back_populates="batch")


class RecommendationItem(Base):
    __tablename__ = "recommendation_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str] = mapped_column(
        ForeignKey("recommendation_batches.id", ondelete="CASCADE"), index=True
    )
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    llm_score: Mapped[float] = mapped_column(Float, default=0)
    final_score: Mapped[float] = mapped_column(Float, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")

    batch: Mapped[RecommendationBatch] = relationship(back_populates="items")
    paper: Mapped[Paper] = relationship()
    __table_args__ = (UniqueConstraint("batch_id", "paper_id", name="uq_recommendation_item"),)


class SyncRun(Base):
    __tablename__ = "sync_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[SyncStatus] = mapped_column(Enum(SyncStatus), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    items_seen: Mapped[int] = mapped_column(Integer, default=0)
    items_created: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class ApiUsage(Base):
    __tablename__ = "api_usage"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    service: Mapped[str] = mapped_column(String(50), index=True)
    operation: Mapped[str] = mapped_column(String(80))
    request_count: Mapped[int] = mapped_column(Integer, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class SeenSourceItem(Base):
    __tablename__ = "seen_source_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    source: Mapped[str] = mapped_column(String(50), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    doi: Mapped[str | None] = mapped_column(String(255), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    outcome: Mapped[str] = mapped_column(String(40), index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    classification_version: Mapped[str] = mapped_column(String(40), default="")
    paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), index=True
    )

    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_seen_source_item"),)


class CleanupRun(Base):
    __tablename__ = "cleanup_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="running", index=True)
    scanned_count: Mapped[int] = mapped_column(Integer, default=0)
    deleted_count: Mapped[int] = mapped_column(Integer, default=0)
    protected_count: Mapped[int] = mapped_column(Integer, default=0)
    papers_before: Mapped[int] = mapped_column(Integer, default=0)
    papers_after: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
