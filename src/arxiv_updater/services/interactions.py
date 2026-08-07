from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Interaction, InteractionKind
from .preferences import mark_preferences_dirty

WEIGHTS = {
    InteractionKind.ABSTRACT_VIEWED: 3.0,
    InteractionKind.SAVED: 5.0,
    InteractionKind.FULLTEXT: 4.0,
    InteractionKind.DISMISSED: -5.0,
}


def record_interaction(db: Session, paper_id: str, kind: InteractionKind) -> Interaction:
    """Record a reading signal exactly once for the local reader."""

    interaction = db.scalar(
        select(Interaction).where(
            Interaction.paper_id == paper_id,
            Interaction.kind == kind,
        )
    )
    if interaction is None:
        interaction = Interaction(
            paper_id=paper_id,
            kind=kind,
            weight=WEIGHTS[kind],
        )
        db.add(interaction)
        mark_preferences_dirty(db)
        db.commit()
        db.refresh(interaction)
    return interaction


def remove_interaction(db: Session, paper_id: str, kind: InteractionKind) -> None:
    interaction = db.scalar(
        select(Interaction).where(
            Interaction.paper_id == paper_id,
            Interaction.kind == kind,
        )
    )
    if interaction:
        db.delete(interaction)
        mark_preferences_dirty(db)
        db.commit()
