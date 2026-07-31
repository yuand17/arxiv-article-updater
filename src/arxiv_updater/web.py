from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from .auth import (
    authenticate,
    consume_invite,
    create_invite,
    create_user,
    get_current_user,
    require_admin,
)
from .config import get_settings
from .db import get_db, init_db
from .models import (
    ApiUsage,
    AuthorFollow,
    InteractionKind,
    Paper,
    SyncRun,
    TrackedAuthor,
    User,
)
from .services.interactions import record_interaction, remove_interaction
from .services.llm import SummaryUnavailableError, generate_summary
from .services.ranking import rank_papers
from .sources.scholar import parse_scholar_author_id

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
DbSession = Annotated[Session, Depends(get_db)]


def source_label(source: str, metadata: dict) -> str:
    if source == "journal":
        return str(metadata.get("journal") or "期刊")
    return {"arxiv": "arXiv", "scholar": "重点作者", "scirate": "SciRate"}.get(
        source, source
    )


templates.env.globals["source_label"] = source_label


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="arXiv 智能文章更新器")
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.on_event("startup")
    def startup() -> None:
        init_db()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(request, "login.html", {"error": None})

    @app.post("/login")
    def login(
        request: Request,
        db: DbSession,
        email: str = Form(),
        password: str = Form(),
    ):
        user = authenticate(db, email, password)
        if not user:
            return templates.TemplateResponse(
                request, "login.html", {"error": "邮箱或密码不正确"}, status_code=400
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(request: Request) -> RedirectResponse:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/register", response_class=HTMLResponse)
    def register_page(request: Request, invite: str = "") -> HTMLResponse:
        return templates.TemplateResponse(
            request, "register.html", {"invite": invite, "error": None}
        )

    @app.post("/register")
    def register(
        request: Request,
        db: DbSession,
        invite: str = Form(),
        email: str = Form(),
        display_name: str = Form(),
        password: str = Form(),
    ):
        try:
            consume_invite(db, invite)
            user = create_user(db, email, password, display_name)
        except ValueError as exc:
            return templates.TemplateResponse(
                request,
                "register.html",
                {"invite": invite, "error": str(exc)},
                status_code=400,
            )
        request.session["user_id"] = user.id
        return RedirectResponse("/", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        db: DbSession,
        view: str = Query("weekly"),
        q: str = Query(""),
        category: str = Query(""),
    ) -> Response:
        settings = get_settings()
        user_id = request.session.get("user_id")
        if not user_id and settings.is_development and settings.local_dev_auto_login:
            user = db.scalar(select(User).where(User.email == "local@localhost"))
            if user:
                request.session["user_id"] = user.id
                user_id = user.id
        if not user_id:
            return RedirectResponse("/login", status_code=303)
        user = db.get(User, user_id)
        if not user:
            request.session.clear()
            return RedirectResponse("/login", status_code=303)
        allowed_views = {"weekly", "all", "authors", "scirate", "arxiv", "journals", "saved"}
        view = view if view in allowed_views else "weekly"
        limit = 20 if view == "weekly" else 100
        ranked = rank_papers(db, user, view=view, query=q, category=category, limit=limit)
        categories = sorted(
            {
                paper_category
                for item in ranked
                for paper_category in (item.paper.categories or [])
            }
        )
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "user": user,
                "ranked": ranked,
                "view": view,
                "q": q,
                "category": category,
                "categories": categories,
            },
        )

    @app.post("/papers/{paper_id}/interested", response_class=HTMLResponse)
    def interested(request: Request, paper_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        paper = db.scalar(
            select(Paper).options(selectinload(Paper.sources)).where(Paper.id == paper_id)
        )
        if not paper:
            return HTMLResponse("论文不存在", status_code=404)
        record_interaction(db, user.id, paper.id, InteractionKind.INTERESTED)
        summary_error = None
        try:
            summary = generate_summary(db, user, paper)
        except SummaryUnavailableError as exc:
            summary = None
            summary_error = str(exc)
        return templates.TemplateResponse(
            request,
            "partials/paper_detail.html",
            {"paper": paper, "summary": summary, "summary_error": summary_error},
        )

    @app.post("/papers/{paper_id}/save", response_class=HTMLResponse)
    def toggle_save(request: Request, paper_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        from .models import Interaction

        existing = db.scalar(
            select(Interaction).where(
                Interaction.user_id == user.id,
                Interaction.paper_id == paper_id,
                Interaction.kind == InteractionKind.SAVED,
            )
        )
        if existing:
            remove_interaction(db, user.id, paper_id, InteractionKind.SAVED)
            saved = False
        else:
            record_interaction(db, user.id, paper_id, InteractionKind.SAVED)
            saved = True
        return templates.TemplateResponse(
            request, "partials/save_button.html", {"paper_id": paper_id, "saved": saved}
        )

    @app.post("/papers/{paper_id}/dismiss")
    def dismiss(request: Request, paper_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        if not db.get(Paper, paper_id):
            return HTMLResponse("论文不存在", status_code=404)
        record_interaction(db, user.id, paper_id, InteractionKind.DISMISSED)
        return HTMLResponse("")

    @app.get("/papers/{paper_id}/fulltext")
    def fulltext(request: Request, paper_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        paper = db.get(Paper, paper_id)
        if not paper:
            return HTMLResponse("论文不存在", status_code=404)
        target = paper.pdf_url or paper.canonical_url
        if not target:
            return HTMLResponse("这篇论文没有可用的外部链接", status_code=404)
        record_interaction(db, user.id, paper_id, InteractionKind.FULLTEXT)
        return RedirectResponse(target, status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, db: DbSession) -> Response:
        user = get_current_user(request, db)
        follows = db.scalars(
            select(AuthorFollow)
            .options(selectinload(AuthorFollow.author))
            .where(AuthorFollow.user_id == user.id)
            .order_by(AuthorFollow.created_at.desc())
        ).all()
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"user": user, "follows": follows, "error": None, "saved": False},
        )

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(request: Request, db: DbSession, interests: str = Form("")) -> Response:
        user = get_current_user(request, db)
        user.interests = interests.strip()
        db.commit()
        follows = db.scalars(
            select(AuthorFollow)
            .options(selectinload(AuthorFollow.author))
            .where(AuthorFollow.user_id == user.id)
        ).all()
        return templates.TemplateResponse(
            request,
            "settings.html",
            {"user": user, "follows": follows, "error": None, "saved": True},
        )

    @app.post("/settings/authors")
    def add_author(
        request: Request,
        background_tasks: BackgroundTasks,
        db: DbSession,
        profile_url: str = Form(),
    ) -> Response:
        user = get_current_user(request, db)
        try:
            author_id = parse_scholar_author_id(profile_url)
        except ValueError:
            return RedirectResponse("/settings?author_error=1", status_code=303)
        author = db.scalar(
            select(TrackedAuthor).where(TrackedAuthor.scholar_author_id == author_id)
        )
        if author is None:
            author = TrackedAuthor(
                scholar_author_id=author_id,
                profile_url=profile_url.strip(),
                name=f"Scholar {author_id}",
            )
            db.add(author)
            db.flush()
        exists = db.scalar(
            select(AuthorFollow).where(
                AuthorFollow.user_id == user.id, AuthorFollow.author_id == author.id
            )
        )
        if not exists:
            db.add(AuthorFollow(user_id=user.id, author_id=author.id))
            db.commit()
            if get_settings().serpapi_api_key:
                from .services.sync import scheduled_sync

                background_tasks.add_task(scheduled_sync, "scholar")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/authors/{follow_id}/delete")
    def delete_author(request: Request, follow_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        follow = db.scalar(
            select(AuthorFollow).where(
                AuthorFollow.id == follow_id, AuthorFollow.user_id == user.id
            )
        )
        if follow:
            db.delete(follow)
            db.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request, db: DbSession) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        latest_runs = db.scalars(
            select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)
        ).all()
        usage = db.execute(
            select(
                ApiUsage.service,
                func.sum(ApiUsage.request_count),
                func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
            ).group_by(ApiUsage.service)
        ).all()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {"user": user, "runs": latest_runs, "usage": usage, "invite_url": None},
        )

    @app.post("/admin/invites", response_class=HTMLResponse)
    def admin_invite(request: Request, db: DbSession) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        token = create_invite(db, user.id)
        runs = db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)).all()
        usage = db.execute(
            select(
                ApiUsage.service,
                func.sum(ApiUsage.request_count),
                func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
            ).group_by(ApiUsage.service)
        ).all()
        return templates.TemplateResponse(
            request,
            "admin.html",
            {
                "user": user,
                "runs": runs,
                "usage": usage,
                "invite_url": f"{get_settings().base_url}/register?invite={token}",
            },
        )

    return app


app = create_app()
