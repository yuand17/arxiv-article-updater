"""Initial application schema.

Revision ID: 0001
Revises: None
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "journal_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("feed_url", sa.Text(), nullable=False, unique=True),
        sa.Column("issn", sa.String(32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_journal_subscriptions_is_active", "journal_subscriptions", ["is_active"])
    op.create_table(
        "papers",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("normalized_title", sa.Text(), nullable=False),
        sa.Column("abstract", sa.Text(), nullable=False),
        sa.Column("authors_text", sa.Text(), nullable=False),
        sa.Column("first_author", sa.String(255), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("arxiv_id", sa.String(32), unique=True),
        sa.Column("doi", sa.String(255), unique=True),
        sa.Column("scholar_citation_id", sa.String(255), unique=True),
        sa.Column("canonical_url", sa.Text()),
        sa.Column("pdf_url", sa.Text()),
        sa.Column("categories", sa.JSON(), nullable=False),
        sa.Column("scites_count", sa.Integer(), nullable=False),
        sa.Column("is_scirate_hot", sa.Boolean(), nullable=False),
    )
    op.create_index("idx_papers_published_discovered", "papers", ["published_at", "discovered_at"])
    for column in ("discovered_at", "is_scirate_hot", "normalized_title", "published_at"):
        op.create_index(f"ix_papers_{column}", "papers", [column])
    op.create_table(
        "sync_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column(
            "status",
            sa.Enum("RUNNING", "SUCCESS", "FAILED", "SKIPPED", name="syncstatus"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("items_seen", sa.Integer(), nullable=False),
        sa.Column("items_created", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_sync_runs_source", "sync_runs", ["source"])
    op.create_index("ix_sync_runs_status", "sync_runs", ["status"])
    op.create_table(
        "tracked_authors",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("scholar_author_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("profile_url", sa.Text(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_tracked_authors_last_synced_at", "tracked_authors", ["last_synced_at"])
    op.create_index(
        "ix_tracked_authors_scholar_author_id",
        "tracked_authors",
        ["scholar_author_id"],
        unique=True,
    )
    op.create_table(
        "users",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("role", sa.Enum("MEMBER", "ADMIN", name="userrole"), nullable=False),
        sa.Column("interests", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)
    op.create_table(
        "api_usage",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("service", sa.String(50), nullable=False),
        sa.Column("user_id", sa.String(36)),
        sa.Column("operation", sa.String(80), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_api_usage_created_at", "api_usage", ["created_at"])
    op.create_index("ix_api_usage_service", "api_usage", ["service"])
    op.create_table(
        "author_follows",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("author_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["tracked_authors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "author_id", name="uq_user_author_follow"),
    )
    op.create_index("ix_author_follows_author_id", "author_follows", ["author_id"])
    op.create_index("ix_author_follows_user_id", "author_follows", ["user_id"])
    op.create_table(
        "interactions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("user_id", sa.String(36), nullable=False),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column(
            "kind",
            sa.Enum("INTERESTED", "SAVED", "FULLTEXT", "DISMISSED", name="interactionkind"),
            nullable=False,
        ),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "idx_interactions_user_paper_kind", "interactions", ["user_id", "paper_id", "kind"]
    )
    for column in ("created_at", "kind", "paper_id", "user_id"):
        op.create_index(f"ix_interactions_{column}", "interactions", [column])
    op.create_table(
        "invites",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_by_id", sa.String(36)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_invites_expires_at", "invites", ["expires_at"])
    op.create_table(
        "paper_sources",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("source", "external_id", name="uq_source_external_id"),
    )
    op.create_index("ix_paper_sources_paper_id", "paper_sources", ["paper_id"])
    op.create_index("ix_paper_sources_source", "paper_sources", ["source"])
    op.create_table(
        "paper_summaries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("paper_id", sa.String(36), nullable=False),
        sa.Column("tldr", sa.Text(), nullable=False),
        sa.Column("contributions", sa.JSON(), nullable=False),
        sa.Column("methods", sa.Text(), nullable=False),
        sa.Column("limitations", sa.Text(), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("prompt_version", sa.String(30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_paper_summaries_paper_id", "paper_summaries", ["paper_id"], unique=True)


def downgrade() -> None:
    for table in (
        "paper_summaries",
        "paper_sources",
        "invites",
        "interactions",
        "author_follows",
        "api_usage",
        "users",
        "tracked_authors",
        "sync_runs",
        "papers",
        "journal_subscriptions",
    ):
        op.drop_table(table)
    for enum_name in ("interactionkind", "userrole", "syncstatus"):
        sa.Enum(name=enum_name).drop(op.get_bind(), checkfirst=True)
