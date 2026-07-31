from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@event.listens_for(Engine, "connect")
def _sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    from . import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def migrate_database() -> None:
    """Upgrade a fresh database, or safely baseline a pre-Alembic local database."""
    from alembic.config import Config

    from alembic import command

    from . import models  # noqa: F401

    candidates = [Path.cwd() / "alembic.ini", Path(__file__).resolve().parents[2] / "alembic.ini"]
    config_path = next((path for path in candidates if path.exists()), candidates[0])
    if not config_path.exists():
        raise RuntimeError(f"Alembic configuration not found: {config_path}")
    config = Config(config_path)
    tables = set(inspect(engine).get_table_names())
    if tables and "alembic_version" not in tables:
        Base.metadata.create_all(bind=engine)
        command.stamp(config, "head")
    else:
        command.upgrade(config, "head")


def database_counts(session: Session) -> dict[str, int]:
    """Return row counts used by migration verification and diagnostics."""
    from . import models  # noqa: F401

    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = int(session.scalar(select(func.count()).select_from(table)) or 0)
    return counts
