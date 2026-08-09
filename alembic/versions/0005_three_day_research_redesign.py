"""Add three-day recommendation, journal discovery, and retention schema.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _create_discovered_journal_table() -> None:
    op.create_table(
        "journal_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=False, unique=True),
        sa.Column("canonical_domain", sa.String(255), nullable=False),
        sa.Column("issn_online", sa.String(32), nullable=False, server_default=""),
        sa.Column("issn_print", sa.String(32), nullable=False, server_default=""),
        sa.Column("scope_kind", sa.String(20), nullable=False, server_default="general"),
        sa.Column("discovery_status", sa.String(30), nullable=False, server_default="pending"),
        sa.Column("discovery_version", sa.String(40), nullable=False, server_default=""),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("last_items_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_items_imported", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_nonresearch_filtered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_nonphysics_filtered", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_journal_subscriptions_canonical_domain",
        "journal_subscriptions",
        ["canonical_domain"],
    )
    op.create_index(
        "ix_journal_subscriptions_is_active", "journal_subscriptions", ["is_active"]
    )


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("The local redesign supports SQLite only")

    # Old rows were implicit/default feed subscriptions. Papers remain, but subscriptions reset.
    op.drop_table("journal_subscriptions")
    _create_discovered_journal_table()
    op.create_table(
        "journal_endpoints",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("journal_subscription_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("config_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("last_validated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(
            ["journal_subscription_id"], ["journal_subscriptions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "journal_subscription_id", "url", name="uq_journal_endpoint_url"
        ),
    )
    op.create_index(
        "ix_journal_endpoints_journal_subscription_id",
        "journal_endpoints",
        ["journal_subscription_id"],
    )

    op.drop_index("ix_papers_semantic_scholar_id", table_name="papers")
    op.drop_column("papers", "semantic_scholar_id")
    op.add_column(
        "papers", sa.Column("document_type", sa.String(80), nullable=False, server_default="")
    )
    op.add_column("papers", sa.Column("is_original_research", sa.Boolean(), nullable=True))
    op.add_column("papers", sa.Column("is_physics", sa.Boolean(), nullable=True))
    op.add_column("papers", sa.Column("physics_confidence", sa.Float(), nullable=True))
    op.add_column(
        "papers", sa.Column("classification_reason", sa.Text(), nullable=False, server_default="")
    )
    op.add_column(
        "papers",
        sa.Column("classification_source", sa.String(80), nullable=False, server_default=""),
    )
    op.add_column(
        "papers",
        sa.Column("classification_version", sa.String(40), nullable=False, server_default=""),
    )
    op.add_column("papers", sa.Column("classified_at", sa.DateTime(timezone=True), nullable=True))

    op.add_column(
        "app_preferences",
        sa.Column("featured_paper_count", sa.Integer(), nullable=False, server_default="66"),
    )
    for column in (
        sa.Column("requested_count", sa.Integer(), nullable=False, server_default="66"),
        sa.Column("candidate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("shortlist_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rerank_success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rerank_fallback_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("selected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("filtered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_stats_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("stale_sources_json", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("ranking_version", sa.String(40), nullable=False, server_default=""),
    ):
        op.add_column("recommendation_batches", column)

    op.create_table(
        "seen_source_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source", sa.String(50), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("doi", sa.String(255), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("classification_version", sa.String(40), nullable=False, server_default=""),
        sa.Column("paper_id", sa.String(36), nullable=True),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("source", "external_id", name="uq_seen_source_item"),
    )
    for column in ("source", "doi", "outcome", "paper_id"):
        op.create_index(f"ix_seen_source_items_{column}", "seen_source_items", [column])

    op.create_table(
        "cleanup_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="running"),
        sa.Column("scanned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("protected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("papers_before", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("papers_after", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_cleanup_runs_status", "cleanup_runs", ["status"])

    bind.execute(sa.text("DELETE FROM api_usage WHERE lower(service) LIKE '%semantic%scholar%'"))
    bind.execute(sa.text("UPDATE source_schedules SET interval_days = 1 WHERE source = 'journals'"))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("The local redesign supports SQLite only")

    op.drop_table("cleanup_runs")
    op.drop_table("seen_source_items")
    for column in (
        "ranking_version",
        "stale_sources_json",
        "source_stats_json",
        "filtered_count",
        "selected_count",
        "rerank_fallback_count",
        "rerank_success_count",
        "shortlist_count",
        "candidate_count",
        "requested_count",
    ):
        op.drop_column("recommendation_batches", column)
    op.drop_column("app_preferences", "featured_paper_count")

    op.add_column("papers", sa.Column("semantic_scholar_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_papers_semantic_scholar_id", "papers", ["semantic_scholar_id"], unique=True
    )
    for column in (
        "classified_at",
        "classification_version",
        "classification_source",
        "classification_reason",
        "physics_confidence",
        "is_physics",
        "is_original_research",
        "document_type",
    ):
        op.drop_column("papers", column)

    op.drop_table("journal_endpoints")
    op.drop_table("journal_subscriptions")
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
    bind.execute(sa.text("UPDATE source_schedules SET interval_days = 7 WHERE source = 'journals'"))
