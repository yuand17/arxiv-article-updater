"""Store Google Scholar author citation counts.

Revision ID: 0004
Revises: 0003
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tracked_authors",
        sa.Column("citation_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "tracked_authors",
        sa.Column("citation_count_updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.get_bind().execute(
        sa.text("UPDATE source_schedules SET next_due_at = :now WHERE source = 'scholar'"),
        {"now": datetime.now(UTC)},
    )


def downgrade() -> None:
    op.drop_column("tracked_authors", "citation_count_updated_at")
    op.drop_column("tracked_authors", "citation_count")
