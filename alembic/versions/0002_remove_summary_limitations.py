"""Remove limitations from paper summaries.

Revision ID: 0002
Revises: 0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("paper_summaries", "limitations")


def downgrade() -> None:
    op.add_column(
        "paper_summaries",
        sa.Column("limitations", sa.Text(), nullable=False, server_default=""),
    )
