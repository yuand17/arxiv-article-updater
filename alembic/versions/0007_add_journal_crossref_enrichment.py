"""Add Crossref enrichment to the remaining built-in journal feeds.

Revision ID: 0007
Revises: 0006
"""

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION = "builtin-journals-v2"
PREVIOUS_CATALOG_VERSION = "builtin-journals-v1"
ENDPOINTS = (
    (
        "Nature Physics",
        "https://api.crossref.org/journals/1745-2481/works",
    ),
    (
        "Physical Review Letters",
        "https://api.crossref.org/journals/1079-7114/works",
    ),
    (
        "Physical Review X",
        "https://api.crossref.org/journals/2160-3308/works",
    ),
    (
        "PRX Quantum",
        "https://api.crossref.org/journals/2691-3399/works",
    ),
)
BUILTIN_JOURNAL_NAMES = (
    "Nature",
    "Nature Physics",
    "Nature Communications",
    "Science",
    "Science Advances",
    "Physical Review Letters",
    "Physical Review X",
    "PRX Quantum",
)


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    subscriptions = sa.table(
        "journal_subscriptions",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("discovery_version", sa.String),
    )
    endpoints = sa.table(
        "journal_endpoints",
        sa.column("id", sa.String),
        sa.column("journal_subscription_id", sa.String),
        sa.column("kind", sa.String),
        sa.column("url", sa.Text),
        sa.column("priority", sa.Integer),
        sa.column("last_validated_at", sa.DateTime(timezone=True)),
    )
    return subscriptions, endpoints


def upgrade() -> None:
    bind = op.get_bind()
    subscriptions, endpoints = _tables()
    now = datetime.now(UTC)
    bind.execute(
        subscriptions.update()
        .where(subscriptions.c.name.in_(BUILTIN_JOURNAL_NAMES))
        .values(discovery_version=CATALOG_VERSION)
    )
    for journal_name, url in ENDPOINTS:
        subscription_id = bind.execute(
            sa.select(subscriptions.c.id).where(subscriptions.c.name == journal_name)
        ).scalar_one()
        exists = bind.execute(
            sa.select(endpoints.c.id).where(
                endpoints.c.journal_subscription_id == subscription_id,
                endpoints.c.url == url,
            )
        ).first()
        if exists is not None:
            continue
        bind.execute(
            endpoints.insert().values(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{subscription_id}|{url}")),
                journal_subscription_id=subscription_id,
                kind="crossref",
                url=url,
                priority=20,
                last_validated_at=now,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    subscriptions, endpoints = _tables()
    urls = [url for _journal_name, url in ENDPOINTS]
    bind.execute(endpoints.delete().where(endpoints.c.url.in_(urls)))
    bind.execute(
        subscriptions.update()
        .where(subscriptions.c.name.in_(BUILTIN_JOURNAL_NAMES))
        .values(discovery_version=PREVIOUS_CATALOG_VERSION)
    )
