import socket
from pathlib import Path

import typer
import uvicorn

from . import __version__
from .config import get_settings
from .db import init_db, migrate_database, sqlite_database_path

app = typer.Typer(no_args_is_help=True, help="arXiv Updater 本地管理命令")


@app.command("init-db")
def initialize_database() -> None:
    """Create or upgrade the local SQLite library."""

    init_db()
    typer.echo("本地数据库已就绪。")


@app.command("migrate-db")
def migrate_db() -> None:
    """Apply local Alembic migrations with an automatic SQLite backup if needed."""

    migrate_database()
    typer.echo("数据库迁移完成。")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
) -> None:
    """Run the private loopback web app and its embedded update scheduler."""

    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise typer.BadParameter("本地个人版只能绑定 127.0.0.1、::1 或 localhost")
    init_db()
    from .web import create_app

    uvicorn.run(create_app(with_scheduler=True), host=host, port=port, reload=False)


@app.command()
def doctor() -> None:
    """Show local paths and external service configuration status without exposing keys."""

    settings = get_settings()
    database_path = sqlite_database_path()
    checks = {
        "version": __version__,
        "python_host": socket.gethostname(),
        "database": str(database_path) if database_path else "SQLite in memory",
        "database_parent": str(Path("data").resolve()),
        "serpapi": "configured" if settings.serpapi_api_key else "not configured",
        "semantic_scholar": "configured" if settings.semantic_scholar_api_key else "not configured",
        "deepseek": "configured" if settings.deepseek_api_key else "not configured",
    }
    for key, value in checks.items():
        typer.echo(f"{key}: {value}")


@app.command()
def sync(source: str = typer.Option("all", help="arxiv, scholar, scirate, journals, all")) -> None:
    """Run one local source update immediately."""

    from .db import SessionLocal
    from .services.sync import sync_sources

    init_db()
    with SessionLocal() as db:
        results = sync_sources(db, source)
    for result in results:
        typer.echo(f"{result.source}: {result.status.value} ({result.items_created} new)")
