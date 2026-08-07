"""Convert the shared deployment schema into the local single-user schema.

Revision ID: 0003
Revises: 0002
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def upgrade() -> None:
    """Migrate the existing one-user library without retaining login-only data."""

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("The single-user refactor supports SQLite only")

    interests = (
        bind.execute(
            sa.text("SELECT interests FROM users ORDER BY created_at, id LIMIT 1")
        ).scalar()
        or ""
    )
    now = _now()

    op.add_column(
        "papers",
        sa.Column("abstract_source", sa.String(length=50), nullable=False, server_default=""),
    )
    op.add_column("papers", sa.Column("abstract_match_confidence", sa.Float(), nullable=True))
    op.add_column(
        "papers", sa.Column("abstract_checked_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "papers",
        sa.Column(
            "abstract_status", sa.String(length=20), nullable=False, server_default="missing"
        ),
    )
    op.add_column("papers", sa.Column("semantic_scholar_id", sa.String(length=64), nullable=True))
    op.create_index("ix_papers_abstract_status", "papers", ["abstract_status"])
    op.create_index("ix_papers_semantic_scholar_id", "papers", ["semantic_scholar_id"], unique=True)
    bind.execute(
        sa.text(
            "UPDATE papers "
            "SET abstract_status = CASE WHEN trim(abstract) <> '' "
            "THEN 'available' ELSE 'missing' END, "
            "abstract_source = CASE WHEN trim(abstract) <> '' THEN 'existing' ELSE '' END"
        )
    )

    op.create_table(
        "app_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("manual_interests", sa.Text(), nullable=False),
        sa.Column("profile_summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("profile_json", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("profile_model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column(
            "profile_prompt_version", sa.String(length=30), nullable=False, server_default=""
        ),
        sa.Column("profile_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile_interaction_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("profile_dirty_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind.execute(
        sa.text(
            "INSERT INTO app_preferences "
            "(id, manual_interests, profile_summary, profile_json, profile_model, "
            "profile_prompt_version, profile_interaction_count, created_at, updated_at) "
            "VALUES (:id, :manual_interests, '', '{}', '', '', 0, :now, :now)"
        ),
        {"id": 1, "manual_interests": interests, "now": now},
    )

    op.create_table(
        "source_schedules",
        sa.Column("source", sa.String(length=50), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_source_schedules_next_due_at", "source_schedules", ["next_due_at"])
    bind.execute(
        sa.text(
            "INSERT INTO source_schedules "
            "(source, enabled, interval_days, next_due_at, last_error, updated_at) VALUES "
            "('arxiv', 1, 1, :now, '', :now), "
            "('scirate', 1, 3, :now, '', :now), "
            "('scholar', 1, 7, :now, '', :now), "
            "('journals', 1, 7, :now, '', :now)"
        ),
        {"now": now},
    )

    op.create_table(
        "recommendation_batches",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("profile_generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("model", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=30), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="success"),
        sa.Column("fallback_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
    )
    op.create_index("ix_recommendation_batches_status", "recommendation_batches", ["status"])
    op.create_table(
        "recommendation_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("batch_id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("llm_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("final_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["batch_id"], ["recommendation_batches.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("batch_id", "paper_id", name="uq_recommendation_item"),
    )
    op.create_index("ix_recommendation_items_batch_id", "recommendation_items", ["batch_id"])
    op.create_index("ix_recommendation_items_paper_id", "recommendation_items", ["paper_id"])

    # Rebuild SQLite tables rather than silently retaining user identifiers and foreign keys.
    op.create_table(
        "interactions_single_user",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("paper_id", "kind", name="uq_interaction_paper_kind"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO interactions_single_user (id, paper_id, kind, weight, created_at) "
            "SELECT id, paper_id, kind, weight, created_at FROM interactions "
            "WHERE kind IN ('SAVED', 'FULLTEXT', 'DISMISSED')"
        )
    )
    op.drop_table("interactions")
    op.rename_table("interactions_single_user", "interactions")
    op.create_index("ix_interactions_paper_id", "interactions", ["paper_id"])
    op.create_index("ix_interactions_kind", "interactions", ["kind"])
    op.create_index("ix_interactions_created_at", "interactions", ["created_at"])

    op.create_table(
        "api_usage_single_user",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    bind.execute(
        sa.text(
            "INSERT INTO api_usage_single_user "
            "(id, service, operation, request_count, input_tokens, output_tokens, created_at) "
            "SELECT id, service, operation, request_count, input_tokens, output_tokens, created_at "
            "FROM api_usage"
        )
    )
    op.drop_table("api_usage")
    op.rename_table("api_usage_single_user", "api_usage")
    op.create_index("ix_api_usage_service", "api_usage", ["service"])
    op.create_index("ix_api_usage_created_at", "api_usage", ["created_at"])

    # These tables only implement shared accounts or the retired per-paper summary feature.
    op.drop_table("author_follows")
    op.drop_table("invites")
    op.drop_table("paper_summaries")
    op.drop_table("users")


def downgrade() -> None:
    """Provide a structurally valid, deliberately lossy downgrade for development only."""

    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        raise RuntimeError("The single-user refactor supports SQLite only")
    now = _now()
    legacy_user_id = "00000000-0000-0000-0000-000000000001"
    interests = (
        bind.execute(sa.text("SELECT manual_interests FROM app_preferences WHERE id = 1")).scalar()
        or ""
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("interests", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("email"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO users "
            "(id, email, password_hash, display_name, role, interests, is_active, created_at) "
            "VALUES (:id, 'local@localhost', 'disabled-after-downgrade', '本地用户', 'ADMIN', "
            ":interests, 1, :now)"
        ),
        {"id": legacy_user_id, "interests": interests, "now": now},
    )
    op.create_table(
        "author_follows",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("author_id", sa.String(length=36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["author_id"], ["tracked_authors.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("user_id", "author_id", name="uq_user_author_follow"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO author_follows (id, user_id, author_id, created_at) "
            "SELECT lower(hex(randomblob(16))), :user_id, id, :now FROM tracked_authors"
        ),
        {"user_id": legacy_user_id, "now": now},
    )
    op.create_index("ix_author_follows_user_id", "author_follows", ["user_id"])
    op.create_index("ix_author_follows_author_id", "author_follows", ["author_id"])
    op.create_table(
        "invites",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("created_by_id", sa.String(length=36), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_invites_expires_at", "invites", ["expires_at"])
    op.create_table(
        "paper_summaries",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("paper_id", sa.String(length=36), nullable=False, unique=True),
        sa.Column("tldr", sa.Text(), nullable=False),
        sa.Column("contributions", sa.JSON(), nullable=False),
        sa.Column("methods", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=False),
        sa.Column("prompt_version", sa.String(length=30), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_paper_summaries_paper_id", "paper_summaries", ["paper_id"], unique=True)

    op.create_table(
        "interactions_legacy",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("paper_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["paper_id"], ["papers.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO interactions_legacy (id, user_id, paper_id, kind, weight, created_at) "
            "SELECT id, :user_id, paper_id, "
            "CASE WHEN kind = 'ABSTRACT_VIEWED' THEN 'INTERESTED' ELSE kind END, "
            "weight, created_at FROM interactions"
        ),
        {"user_id": legacy_user_id},
    )
    op.drop_table("interactions")
    op.rename_table("interactions_legacy", "interactions")
    op.create_index("ix_interactions_user_id", "interactions", ["user_id"])
    op.create_index("ix_interactions_paper_id", "interactions", ["paper_id"])
    op.create_index("ix_interactions_kind", "interactions", ["kind"])
    op.create_index("ix_interactions_created_at", "interactions", ["created_at"])
    op.create_index(
        "idx_interactions_user_paper_kind", "interactions", ["user_id", "paper_id", "kind"]
    )

    op.create_table(
        "api_usage_legacy",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("service", sa.String(length=50), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=True),
        sa.Column("operation", sa.String(length=80), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
    )
    bind.execute(
        sa.text(
            "INSERT INTO api_usage_legacy "
            "(id, service, operation, request_count, input_tokens, output_tokens, created_at) "
            "SELECT id, service, operation, request_count, input_tokens, output_tokens, created_at "
            "FROM api_usage"
        )
    )
    op.drop_table("api_usage")
    op.rename_table("api_usage_legacy", "api_usage")
    op.create_index("ix_api_usage_service", "api_usage", ["service"])
    op.create_index("ix_api_usage_created_at", "api_usage", ["created_at"])

    op.drop_table("recommendation_items")
    op.drop_table("recommendation_batches")
    op.drop_table("source_schedules")
    op.drop_table("app_preferences")
    with op.batch_alter_table("papers") as batch:
        batch.drop_index("ix_papers_abstract_status")
        batch.drop_index("ix_papers_semantic_scholar_id")
        batch.drop_column("semantic_scholar_id")
        batch.drop_column("abstract_status")
        batch.drop_column("abstract_checked_at")
        batch.drop_column("abstract_match_confidence")
        batch.drop_column("abstract_source")
