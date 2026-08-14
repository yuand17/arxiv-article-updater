"""Seed the fixed eight-journal catalog.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_VERSION = "builtin-journals-v1"
JOURNALS = (
    {
        "id": "2c6612ff-2324-5db8-a468-c7af51d75f17",
        "name": "Nature",
        "homepage_url": "https://www.nature.com",
        "canonical_domain": "www.nature.com",
        "issn_online": "1476-4687",
        "issn_print": "0028-0836",
        "scope_kind": "general",
        "endpoints": (
            ("rss", "https://www.nature.com/nature.rss", 10),
            ("crossref", "https://api.crossref.org/journals/1476-4687/works", 20),
        ),
    },
    {
        "id": "8b70ec09-7b22-594c-b58e-321681aa7064",
        "name": "Nature Physics",
        "homepage_url": "https://www.nature.com/nphys",
        "canonical_domain": "www.nature.com",
        "issn_online": "1745-2481",
        "issn_print": "1745-2473",
        "scope_kind": "physics",
        "endpoints": (("rss", "https://www.nature.com/nphys.rss", 10),),
    },
    {
        "id": "b38c0d9b-08fc-55d9-9624-0126d0faac29",
        "name": "Nature Communications",
        "homepage_url": "https://www.nature.com/ncomms",
        "canonical_domain": "www.nature.com",
        "issn_online": "2041-1723",
        "issn_print": "",
        "scope_kind": "general",
        "endpoints": (
            ("rss", "https://www.nature.com/ncomms.rss", 10),
            ("crossref", "https://api.crossref.org/journals/2041-1723/works", 20),
        ),
    },
    {
        "id": "92673ded-806e-51d5-9530-fc2d8546b83e",
        "name": "Science",
        "homepage_url": "https://www.science.org/journal/science",
        "canonical_domain": "www.science.org",
        "issn_online": "1095-9203",
        "issn_print": "0036-8075",
        "scope_kind": "general",
        "endpoints": (
            (
                "rss",
                "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
                10,
            ),
            ("crossref", "https://api.crossref.org/journals/1095-9203/works", 20),
        ),
    },
    {
        "id": "c9138da6-8573-5e79-a401-5524c92db097",
        "name": "Science Advances",
        "homepage_url": "https://www.science.org/journal/sciadv",
        "canonical_domain": "www.science.org",
        "issn_online": "2375-2548",
        "issn_print": "",
        "scope_kind": "general",
        "endpoints": (
            (
                "rss",
                "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv",
                10,
            ),
            ("crossref", "https://api.crossref.org/journals/2375-2548/works", 20),
        ),
    },
    {
        "id": "c8e3820a-1a79-526a-b332-b798308ca3ae",
        "name": "Physical Review Letters",
        "homepage_url": "https://journals.aps.org/prl/",
        "canonical_domain": "journals.aps.org",
        "issn_online": "1079-7114",
        "issn_print": "0031-9007",
        "scope_kind": "physics",
        "endpoints": (("rss", "https://feeds.aps.org/rss/recent/prl.xml", 10),),
    },
    {
        "id": "bf53e198-7ef8-53c8-a8fb-b5ea62a3dad4",
        "name": "Physical Review X",
        "homepage_url": "https://journals.aps.org/prx/",
        "canonical_domain": "journals.aps.org",
        "issn_online": "2160-3308",
        "issn_print": "",
        "scope_kind": "physics",
        "endpoints": (("rss", "https://feeds.aps.org/rss/recent/prx.xml", 10),),
    },
    {
        "id": "bf386e2b-22e9-54c9-988d-4d76f777c673",
        "name": "PRX Quantum",
        "homepage_url": "https://journals.aps.org/prxquantum/",
        "canonical_domain": "journals.aps.org",
        "issn_online": "2691-3399",
        "issn_print": "",
        "scope_kind": "physics",
        "endpoints": (
            ("rss", "https://feeds.aps.org/rss/recent/prxquantum.xml", 10),
        ),
    },
)


def _tables() -> tuple[sa.TableClause, sa.TableClause]:
    subscriptions = sa.table(
        "journal_subscriptions",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("homepage_url", sa.Text),
        sa.column("canonical_domain", sa.String),
        sa.column("issn_online", sa.String),
        sa.column("issn_print", sa.String),
        sa.column("scope_kind", sa.String),
        sa.column("discovery_status", sa.String),
        sa.column("discovery_version", sa.String),
        sa.column("last_discovered_at", sa.DateTime(timezone=True)),
        sa.column("is_active", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
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
    existing = {
        str(row.name).casefold(): row
        for row in bind.execute(
            sa.select(
                subscriptions.c.id,
                subscriptions.c.name,
                subscriptions.c.homepage_url,
            )
        ).mappings()
    }

    for journal in JOURNALS:
        row = existing.get(str(journal["name"]).casefold())
        subscription_id = str(row.id) if row is not None else str(journal["id"])
        values = {
            "name": journal["name"],
            "homepage_url": journal["homepage_url"],
            "canonical_domain": journal["canonical_domain"],
            "issn_online": journal["issn_online"],
            "issn_print": journal["issn_print"],
            "scope_kind": journal["scope_kind"],
            "discovery_status": "builtin",
            "discovery_version": CATALOG_VERSION,
        }
        if row is None:
            bind.execute(
                subscriptions.insert().values(
                    id=subscription_id,
                    **values,
                    last_discovered_at=now,
                    is_active=True,
                    created_at=now,
                )
            )
        else:
            bind.execute(
                subscriptions.update()
                .where(subscriptions.c.id == subscription_id)
                .values(**values)
            )

        expected_urls = [str(endpoint[1]) for endpoint in journal["endpoints"]]
        bind.execute(
            endpoints.delete().where(
                endpoints.c.journal_subscription_id == subscription_id,
                endpoints.c.url.not_in(expected_urls),
            )
        )
        endpoint_rows = {
            str(endpoint.url): endpoint
            for endpoint in bind.execute(
                sa.select(endpoints).where(
                    endpoints.c.journal_subscription_id == subscription_id
                )
            ).mappings()
        }
        for kind, url, priority in journal["endpoints"]:
            endpoint_row = endpoint_rows.get(str(url))
            if endpoint_row is None:
                bind.execute(
                    endpoints.insert().values(
                        id=f"{subscription_id[:23]}-{len(endpoint_rows):012d}",
                        journal_subscription_id=subscription_id,
                        kind=kind,
                        url=url,
                        priority=priority,
                        last_validated_at=now,
                    )
                )
                endpoint_rows[str(url)] = {"url": url}
            else:
                bind.execute(
                    endpoints.update()
                    .where(endpoints.c.id == endpoint_row.id)
                    .values(kind=kind, priority=priority)
                )

    bind.execute(
        sa.text(
            "UPDATE source_schedules SET enabled = 1, interval_days = 1, "
            "next_due_at = COALESCE(next_due_at, :now) WHERE source = 'journals'"
        ),
        {"now": now},
    )


def downgrade() -> None:
    bind = op.get_bind()
    subscriptions, endpoints = _tables()
    crossref_urls = [
        str(endpoint[1])
        for journal in JOURNALS
        for endpoint in journal["endpoints"]
        if endpoint[0] == "crossref"
    ]
    bind.execute(endpoints.delete().where(endpoints.c.url.in_(crossref_urls)))
    bind.execute(
        subscriptions.delete().where(
            subscriptions.c.id.in_([str(journal["id"]) for journal in JOURNALS]),
            subscriptions.c.discovery_version == CATALOG_VERSION,
        )
    )
