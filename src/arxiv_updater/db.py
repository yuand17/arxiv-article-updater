import sqlite3
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, func, inspect, select
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


settings = get_settings()
engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
)
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


def sqlite_database_path(database_url: str | None = None) -> Path | None:
    """Return the on-disk SQLite path, never a broad or inferred directory."""

    url = make_url(database_url or settings.database_url)
    if not url.drivername.startswith("sqlite") or not url.database or url.database == ":memory:":
        return None
    return Path(url.database).expanduser().resolve()


def backup_sqlite_database(*, label: str = "pre-migration") -> Path | None:
    """Create and validate a timestamped SQLite backup with the SQLite backup API."""

    source = sqlite_database_path()
    if source is None or not source.is_file():
        return None
    backup_dir = source.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"{source.stem}.{label}-{timestamp}{source.suffix}.bak"
    with sqlite3.connect(source) as source_connection, sqlite3.connect(target) as target_connection:
        source_connection.backup(target_connection)
    with sqlite3.connect(target) as check_connection:
        integrity = check_connection.execute("PRAGMA integrity_check").fetchone()
    if not integrity or integrity[0] != "ok":
        raise RuntimeError(f"SQLite backup integrity check failed: {target}")
    return target


def init_db() -> None:
    """Ensure the local database exists at the current Alembic revision."""

    migrate_database()


def migrate_database() -> None:
    """Upgrade the local database, making a restorable copy before a real upgrade."""
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
        # This path supports test databases built from current ORM metadata.  Existing user data
        # is already Alembic-managed in normal application use and follows the branch below.
        Base.metadata.create_all(bind=engine)
        command.stamp(config, "head")
        return

    script = ScriptDirectory.from_config(config)
    head_revision = script.get_current_head()
    with engine.connect() as connection:
        current_revision = MigrationContext.configure(connection).get_current_revision()
    backup_path = None
    if current_revision != head_revision:
        backup_path = backup_sqlite_database()
    try:
        command.upgrade(config, "head")
    except Exception as exc:
        if backup_path:
            raise RuntimeError(f"数据库迁移失败，未继续写入。可恢复备份：{backup_path}") from exc
        raise


def database_counts(session: Session) -> dict[str, int]:
    """Return row counts used by migration verification and diagnostics."""
    from . import models  # noqa: F401

    counts: dict[str, int] = {}
    for table in Base.metadata.sorted_tables:
        counts[table.name] = int(session.scalar(select(func.count()).select_from(table)) or 0)
    return counts
