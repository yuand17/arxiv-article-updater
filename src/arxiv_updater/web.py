import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

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
    JournalSubscription,
    Paper,
    SyncRun,
    TrackedAuthor,
    User,
    UserRole,
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


def tracked_author_count(paper: Paper) -> int:
    return len(
        {
            str(source.metadata_json.get("tracked_author_id") or source.external_id)
            for source in paper.sources
            if source.source == "scholar"
        }
    )


templates.env.globals["source_label"] = source_label
templates.env.globals["tracked_author_count"] = tracked_author_count


def settings_context(
    db: Session,
    user: User,
    *,
    saved: bool = False,
    invite_url: str | None = None,
    journal_error: str = "",
) -> dict[str, object]:
    context: dict[str, object] = {
        "user": user,
        "follows": db.scalars(
            select(AuthorFollow)
            .options(selectinload(AuthorFollow.author))
            .where(AuthorFollow.user_id == user.id)
            .order_by(AuthorFollow.created_at.desc())
        ).all(),
        "error": None,
        "saved": saved,
    }
    if user.role == UserRole.ADMIN:
        context.update(
            {
                "runs": db.scalars(
                    select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)
                ).all(),
                "usage": db.execute(
                    select(
                        ApiUsage.service,
                        func.sum(ApiUsage.request_count),
                        func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
                    ).group_by(ApiUsage.service)
                ).all(),
                "invite_url": invite_url,
                "journal_feeds": db.scalars(
                    select(JournalSubscription).order_by(JournalSubscription.name)
                ).all(),
                "journal_error": journal_error,
            }
        )
    return context


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="arXiv 智能文章更新器", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.app_secret_key,
        same_site="lax",
        https_only=settings.base_url.startswith("https://"),
    )
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

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
        if not user_id and settings.allows_dev_auto_login_for(request.url.hostname):
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
    def settings_page(
        request: Request, db: DbSession, journal_error: str = Query("")
    ) -> Response:
        user = get_current_user(request, db)
        return templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(db, user, journal_error=journal_error),
        )

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(request: Request, db: DbSession, interests: str = Form("")) -> Response:
        user = get_current_user(request, db)
        user.interests = interests.strip()
        db.commit()
        return templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(db, user, saved=True),
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

    @app.get("/admin")
    def admin_page(request: Request, db: DbSession) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        return RedirectResponse("/settings#system", status_code=303)

    @app.post("/admin/journals")
    def admin_add_journal(
        request: Request,
        db: DbSession,
        name: str = Form(),
        feed_url: str = Form(),
        issn: str = Form(""),
    ) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        parsed = urlparse(feed_url.strip())
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            return RedirectResponse("/settings?journal_error=https#system", status_code=303)
        if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
            return RedirectResponse("/settings?journal_error=host#system", status_code=303)
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address and not address.is_global:
            return RedirectResponse("/settings?journal_error=host#system", status_code=303)
        exists = db.scalar(
            select(JournalSubscription).where(JournalSubscription.feed_url == feed_url.strip())
        )
        if not exists:
            db.add(
                JournalSubscription(
                    name=name.strip() or parsed.hostname,
                    feed_url=feed_url.strip(),
                    issn=issn.strip(),
                )
            )
            db.commit()
        return RedirectResponse("/settings#system", status_code=303)

    @app.post("/admin/journals/{feed_id}/delete")
    def admin_delete_journal(request: Request, feed_id: str, db: DbSession) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        feed = db.get(JournalSubscription, feed_id)
        if feed:
            db.delete(feed)
            db.commit()
        return RedirectResponse("/settings#system", status_code=303)

    @app.post("/admin/invites", response_class=HTMLResponse)
    def admin_invite(request: Request, db: DbSession) -> Response:
        user = get_current_user(request, db)
        require_admin(user)
        token = create_invite(db, user.id)
        return templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(
                db,
                user,
                invite_url=f"{get_settings().base_url}/register?invite={token}",
            ),
        )

    return app


app = create_app()
