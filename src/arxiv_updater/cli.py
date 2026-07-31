import secrets
import socket
from pathlib import Path

import typer
import uvicorn
from sqlalchemy import func, select

from . import __version__
from .auth import create_invite as create_invite_record
from .auth import create_user
from .config import get_settings
from .db import SessionLocal, init_db
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


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8000),
    with_scheduler: bool = typer.Option(False, "--with-scheduler"),
) -> None:
    """Run the web application; optionally start the lightweight scheduler."""
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

