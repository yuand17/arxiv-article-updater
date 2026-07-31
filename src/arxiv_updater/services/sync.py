from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..db import SessionLocal
from ..models import SyncRun, SyncStatus


@dataclass(slots=True)
class SyncResult:
    source: str
    status: SyncStatus
    items_created: int = 0


def sync_sources(db: Session, source: str = "all") -> list[SyncRun]:
    # Adapters are added in the next vertical slices. Keeping the command usable now
    # makes configuration and authentication independently testable.
    sources = ["arxiv", "scholar", "scirate", "journals"] if source == "all" else [source]
    runs: list[SyncRun] = []
    for name in sources:
        run = SyncRun(source=name, status=SyncStatus.SKIPPED, error="adapter not installed yet")
        db.add(run)
        runs.append(run)
    db.commit()
    return runs


def scheduled_sync(source: str = "all") -> None:
    with SessionLocal() as db:
        sync_sources(db, source)

