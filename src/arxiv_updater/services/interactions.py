from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Interaction, InteractionKind

WEIGHTS = {
    InteractionKind.INTERESTED: 3.0,
    InteractionKind.SAVED: 5.0,
    InteractionKind.FULLTEXT: 1.0,
    InteractionKind.DISMISSED: -5.0,
}


def record_interaction(
    db: Session, user_id: str, paper_id: str, kind: InteractionKind
) -> Interaction:
    interaction = db.scalar(
        select(Interaction).where(
            Interaction.user_id == user_id,
            Interaction.paper_id == paper_id,
            Interaction.kind == kind,
        )
    )
    if interaction is None:
        interaction = Interaction(
            user_id=user_id,
            paper_id=paper_id,
            kind=kind,
            weight=WEIGHTS[kind],
        )
        db.add(interaction)
        db.commit()
        db.refresh(interaction)
    return interaction


def remove_interaction(db: Session, user_id: str, paper_id: str, kind: InteractionKind) -> None:
    interaction = db.scalar(
        select(Interaction).where(
            Interaction.user_id == user_id,
            Interaction.paper_id == paper_id,
            Interaction.kind == kind,
        )
    )
    if interaction:
        db.delete(interaction)
        db.commit()

