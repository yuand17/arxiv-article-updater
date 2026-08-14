"""Fixed journal catalog for the simple single-user release."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import JournalEndpoint, JournalSubscription, utcnow

CATALOG_VERSION = "builtin-journals-v2"


@dataclass(frozen=True, slots=True)
class BuiltinJournalEndpoint:
    kind: str
    url: str
    priority: int


@dataclass(frozen=True, slots=True)
class BuiltinJournal:
    key: str
    subscription_id: str
    name: str
    homepage_url: str
    canonical_domain: str
    issn_online: str
    issn_print: str
    scope_kind: str
    endpoints: tuple[BuiltinJournalEndpoint, ...]


BUILTIN_JOURNALS: tuple[BuiltinJournal, ...] = (
    BuiltinJournal(
        key="nature",
        subscription_id="2c6612ff-2324-5db8-a468-c7af51d75f17",
        name="Nature",
        homepage_url="https://www.nature.com",
        canonical_domain="www.nature.com",
        issn_online="1476-4687",
        issn_print="0028-0836",
        scope_kind="general",
        endpoints=(
            BuiltinJournalEndpoint("rss", "https://www.nature.com/nature.rss", 10),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/1476-4687/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="nature-physics",
        subscription_id="8b70ec09-7b22-594c-b58e-321681aa7064",
        name="Nature Physics",
        homepage_url="https://www.nature.com/nphys",
        canonical_domain="www.nature.com",
        issn_online="1745-2481",
        issn_print="1745-2473",
        scope_kind="physics",
        endpoints=(
            BuiltinJournalEndpoint("rss", "https://www.nature.com/nphys.rss", 10),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/1745-2481/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="nature-communications",
        subscription_id="b38c0d9b-08fc-55d9-9624-0126d0faac29",
        name="Nature Communications",
        homepage_url="https://www.nature.com/ncomms",
        canonical_domain="www.nature.com",
        issn_online="2041-1723",
        issn_print="",
        scope_kind="general",
        endpoints=(
            BuiltinJournalEndpoint("rss", "https://www.nature.com/ncomms.rss", 10),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/2041-1723/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="science",
        subscription_id="92673ded-806e-51d5-9530-fc2d8546b83e",
        name="Science",
        homepage_url="https://www.science.org/journal/science",
        canonical_domain="www.science.org",
        issn_online="1095-9203",
        issn_print="0036-8075",
        scope_kind="general",
        endpoints=(
            BuiltinJournalEndpoint(
                "rss",
                "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=science",
                10,
            ),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/1095-9203/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="science-advances",
        subscription_id="c9138da6-8573-5e79-a401-5524c92db097",
        name="Science Advances",
        homepage_url="https://www.science.org/journal/sciadv",
        canonical_domain="www.science.org",
        issn_online="2375-2548",
        issn_print="",
        scope_kind="general",
        endpoints=(
            BuiltinJournalEndpoint(
                "rss",
                "https://www.science.org/action/showFeed?type=etoc&feed=rss&jc=sciadv",
                10,
            ),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/2375-2548/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="physical-review-letters",
        subscription_id="c8e3820a-1a79-526a-b332-b798308ca3ae",
        name="Physical Review Letters",
        homepage_url="https://journals.aps.org/prl/",
        canonical_domain="journals.aps.org",
        issn_online="1079-7114",
        issn_print="0031-9007",
        scope_kind="physics",
        endpoints=(
            BuiltinJournalEndpoint("rss", "https://feeds.aps.org/rss/recent/prl.xml", 10),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/1079-7114/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="physical-review-x",
        subscription_id="bf53e198-7ef8-53c8-a8fb-b5ea62a3dad4",
        name="Physical Review X",
        homepage_url="https://journals.aps.org/prx/",
        canonical_domain="journals.aps.org",
        issn_online="2160-3308",
        issn_print="",
        scope_kind="physics",
        endpoints=(
            BuiltinJournalEndpoint("rss", "https://feeds.aps.org/rss/recent/prx.xml", 10),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/2160-3308/works",
                20,
            ),
        ),
    ),
    BuiltinJournal(
        key="prx-quantum",
        subscription_id="bf386e2b-22e9-54c9-988d-4d76f777c673",
        name="PRX Quantum",
        homepage_url="https://journals.aps.org/prxquantum/",
        canonical_domain="journals.aps.org",
        issn_online="2691-3399",
        issn_print="",
        scope_kind="physics",
        endpoints=(
            BuiltinJournalEndpoint(
                "rss", "https://feeds.aps.org/rss/recent/prxquantum.xml", 10
            ),
            BuiltinJournalEndpoint(
                "crossref",
                "https://api.crossref.org/journals/2691-3399/works",
                20,
            ),
        ),
    ),
)


def ensure_builtin_journals(db: Session) -> list[JournalSubscription]:
    """Upsert the fixed catalog while preserving each existing on/off state and stats."""

    changed = False
    ordered_ids: list[str] = []
    for journal in BUILTIN_JOURNALS:
        subscription = db.scalar(
            select(JournalSubscription)
            .options(selectinload(JournalSubscription.endpoints))
            .where(func.lower(JournalSubscription.name) == journal.name.lower())
            .limit(1)
        )
        if subscription is None:
            subscription = db.scalar(
                select(JournalSubscription)
                .options(selectinload(JournalSubscription.endpoints))
                .where(JournalSubscription.homepage_url == journal.homepage_url)
                .limit(1)
            )
        if subscription is None:
            subscription = JournalSubscription(
                id=journal.subscription_id,
                name=journal.name,
                homepage_url=journal.homepage_url,
                canonical_domain=journal.canonical_domain,
                issn_online=journal.issn_online,
                issn_print=journal.issn_print,
                scope_kind=journal.scope_kind,
                discovery_status="builtin",
                discovery_version=CATALOG_VERSION,
                last_discovered_at=utcnow(),
                is_active=True,
            )
            db.add(subscription)
            db.flush()
            changed = True

        for attribute, value in (
            ("name", journal.name),
            ("homepage_url", journal.homepage_url),
            ("canonical_domain", journal.canonical_domain),
            ("issn_online", journal.issn_online),
            ("issn_print", journal.issn_print),
            ("scope_kind", journal.scope_kind),
            ("discovery_status", "builtin"),
            ("discovery_version", CATALOG_VERSION),
        ):
            if getattr(subscription, attribute) != value:
                setattr(subscription, attribute, value)
                changed = True

        endpoint_by_url = {endpoint.url: endpoint for endpoint in subscription.endpoints}
        expected_urls = {endpoint.url for endpoint in journal.endpoints}
        for endpoint in list(subscription.endpoints):
            if endpoint.url not in expected_urls:
                db.delete(endpoint)
                changed = True
        for expected in journal.endpoints:
            existing_endpoint = endpoint_by_url.get(expected.url)
            if existing_endpoint is None:
                db.add(
                    JournalEndpoint(
                        journal_subscription_id=subscription.id,
                        kind=expected.kind,
                        url=expected.url,
                        priority=expected.priority,
                        last_validated_at=utcnow(),
                    )
                )
                changed = True
                continue
            if existing_endpoint.kind != expected.kind:
                existing_endpoint.kind = expected.kind
                changed = True
            if existing_endpoint.priority != expected.priority:
                existing_endpoint.priority = expected.priority
                changed = True
        ordered_ids.append(subscription.id)

    if changed:
        db.commit()

    subscriptions = db.scalars(
        select(JournalSubscription)
        .options(selectinload(JournalSubscription.endpoints))
        .where(JournalSubscription.id.in_(ordered_ids))
        .execution_options(populate_existing=True)
    ).all()
    by_id = {subscription.id: subscription for subscription in subscriptions}
    return [by_id[subscription_id] for subscription_id in ordered_ids]
