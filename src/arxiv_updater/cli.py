import secrets
import socket
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from sqlalchemy import create_engine, func, inspect, select

from . import __version__
from .auth import create_invite as create_invite_record
from .auth import create_user
from .config import get_settings
from .db import Base, SessionLocal, database_counts, init_db, migrate_database
from .models import User, UserRole

app = typer.Typer(no_args_is_help=True, help="arXiv 智能文章更新器管理命令")


@app.command("init-db")
def initialize_database() -> None:
    """Create database tables and a safe local development administrator."""
    settings = get_settings()
    init_db()
    if settings.is_development and settings.local_dev_auto_login:
        with SessionLocal() as db:
            count = db.scalar(select(func.count()).select_from(User)) or 0
            if count == 0:
                create_user(
                    db,
                    "local@localhost",
                    secrets.token_urlsafe(24),
                    "本地管理员",
                    UserRole.ADMIN,
                )
                typer.echo("已创建本地开发管理员（仅自动登录，不显示随机密码）。")
    typer.echo("数据库初始化完成。")


@app.command("migrate-db")
def migrate_db() -> None:
    """Apply Alembic migrations, safely baselining a pre-Alembic local database."""
    migrate_database()
    typer.echo("数据库迁移完成。")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    with_scheduler: bool = typer.Option(False, "--with-scheduler"),
) -> None:
    """Run the web application; optionally start the lightweight scheduler."""
    settings = get_settings()
    if (
        settings.is_development
        and settings.local_dev_auto_login
        and not settings.allows_dev_auto_login_for(host)
    ):
        raise typer.BadParameter(
            "启用 LOCAL_DEV_AUTO_LOGIN 时只能绑定 localhost/127.0.0.1/::1"
        )
    initialize_database()
    if with_scheduler:
        from .scheduler import start_scheduler

        start_scheduler()
    uvicorn.run("arxiv_updater.web:app", host=host, port=port, reload=False)


@app.command("create-admin")
def create_admin(email: str, display_name: str = "管理员") -> None:
    init_db()
    password = typer.prompt("密码", hide_input=True, confirmation_prompt=True)
    with SessionLocal() as db:
        create_user(db, email, password, display_name, UserRole.ADMIN)
    typer.echo("管理员创建完成。")


@app.command("create-invite")
def create_invite(days: int = 7) -> None:
    init_db()
    with SessionLocal() as db:
        token = create_invite_record(db, days=days)
    typer.echo(f"{get_settings().base_url}/register?invite={token}")


@app.command()
def doctor() -> None:
    settings = get_settings()
    checks = {
        "version": __version__,
        "python_host": socket.gethostname(),
        "database": "sqlite" if settings.database_url.startswith("sqlite") else "postgresql",
        "database_parent": str(Path("data").resolve()),
        "serpapi": "configured" if settings.serpapi_api_key else "not configured",
        "deepseek": "configured" if settings.deepseek_api_key else "not configured",
    }
    for key, value in checks.items():
        typer.echo(f"{key}: {value}")


@app.command("migrate-sqlite-to-postgres")
def migrate_sqlite_to_postgres(
    target_url: Annotated[str, typer.Option(envvar="TARGET_DATABASE_URL")],
    sqlite_path: Annotated[
        Path, typer.Option(exists=True, dir_okay=False)
    ] = Path("data/arxiv_updater.db"),
) -> None:
    """Copy a SQLite library into an empty PostgreSQL database and verify row counts."""
    if not target_url.startswith(("postgresql://", "postgresql+psycopg://")):
        raise typer.BadParameter("TARGET_DATABASE_URL 必须是 PostgreSQL URL")
    source = create_engine(f"sqlite:///{sqlite_path.resolve().as_posix()}")
    target = create_engine(target_url, pool_pre_ping=True)
    Base.metadata.create_all(bind=source)
    Base.metadata.create_all(bind=target)
    target_tables = set(inspect(target).get_table_names())
    if not set(Base.metadata.tables).issubset(target_tables):
        raise typer.BadParameter("目标数据库结构不完整，请先运行 migrate-db")

    with source.connect() as source_connection, target.begin() as target_connection:
        for table in Base.metadata.sorted_tables:
            existing = target_connection.scalar(select(func.count()).select_from(table)) or 0
            if existing:
                raise typer.BadParameter(f"目标表 {table.name} 不是空表，已停止以避免重复数据")
            rows = [dict(row) for row in source_connection.execute(select(table)).mappings()]
            if rows:
                target_connection.execute(table.insert(), rows)

    from sqlalchemy.orm import Session

    with Session(source) as source_session, Session(target) as target_session:
        source_counts = database_counts(source_session)
        target_counts = database_counts(target_session)
    if source_counts != target_counts:
        raise RuntimeError("迁移后行数校验失败")
    typer.echo(f"迁移完成并校验 {sum(source_counts.values())} 行数据。")


@app.command()
def sync(source: str = typer.Option("all", help="arxiv, scholar, scirate, journals, all")) -> None:
    from .services.sync import sync_sources

    init_db()
    with SessionLocal() as db:
        results = sync_sources(db, source)
    for result in results:
        typer.echo(f"{result.source}: {result.status.value} ({result.items_created} new)")


@app.command()
def worker() -> None:
    from .scheduler import run_worker

    initialize_database()
    run_worker()
