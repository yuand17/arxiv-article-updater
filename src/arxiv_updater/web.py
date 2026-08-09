import ipaddress
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from .arxiv_schedule import next_arxiv_update_at
from .config import get_settings
from .datetime_utils import format_local_datetime
from .db import get_db, init_db
from .models import (
    ApiUsage,
    Interaction,
    InteractionKind,
    JournalSubscription,
    Paper,
    SourceSchedule,
    SyncRun,
    TrackedAuthor,
    utcnow,
)
from .scheduler import DEFAULT_SOURCE_INTERVALS, ensure_source_schedules
from .services.abstracts import enrich_paper_abstract_in_background
from .services.interactions import record_interaction, remove_interaction
from .services.preferences import (
    get_preferences,
    mark_preferences_dirty,
    rebuild_preference_profile_in_background,
)
from .services.ranking import available_categories, rank_papers
from .sources.scholar import parse_scholar_author_id

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
DbSession = Annotated[Session, Depends(get_db)]


def source_label(source: str, metadata: dict) -> str:
    if source == "journal":
        return str(metadata.get("journal") or "期刊")
    return {"arxiv": "arXiv", "scholar": "重点作者", "scirate": "SciRate"}.get(source, source)


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


def local_datetime(value: datetime | None, pattern: str = "%Y-%m-%d %H:%M") -> str:
    return format_local_datetime(value, get_settings().timezone, pattern)


templates.env.filters["local_datetime"] = local_datetime


def _is_public_https(value: str) -> bool:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
        return False
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        return True
    return address.is_global


def settings_context(
    db: Session,
    *,
    saved: bool = False,
    journal_error: str = "",
    sync_started: str = "",
) -> dict[str, object]:
    ensure_source_schedules(db)
    preferences = get_preferences(db)
    settings = get_settings()
    return {
        "preferences": preferences,
        "authors": db.scalars(
            select(TrackedAuthor).order_by(
                TrackedAuthor.citation_count.desc(),
                func.lower(TrackedAuthor.name),
                TrackedAuthor.created_at,
            )
        ).all(),
        "journal_feeds": db.scalars(
            select(JournalSubscription).order_by(JournalSubscription.name)
        ).all(),
        "schedules": db.scalars(select(SourceSchedule).order_by(SourceSchedule.source)).all(),
        "runs": db.scalars(select(SyncRun).order_by(SyncRun.started_at.desc()).limit(20)).all(),
        "usage": db.execute(
            select(
                ApiUsage.service,
                func.sum(ApiUsage.request_count),
                func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
            ).group_by(ApiUsage.service)
        ).all(),
        "service_configured": {
            "SerpAPI": bool(settings.serpapi_api_key),
            "Semantic Scholar": bool(settings.semantic_scholar_api_key),
            "DeepSeek": bool(settings.deepseek_api_key),
        },
        "saved": saved,
        "journal_error": journal_error,
        "sync_started": sync_started,
    }


def create_app(*, with_scheduler: bool = False) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        init_db()
        scheduler = None
        if with_scheduler:
            from .scheduler import start_scheduler

            scheduler = start_scheduler()
        try:
            yield
        finally:
            if scheduler and scheduler.running:
                scheduler.shutdown(wait=False)

    app = FastAPI(title="arXiv Updater", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        db: DbSession,
        view: str = Query("weekly"),
        q: str = Query(""),
        category: str = Query(""),
        offset: int = Query(0, ge=0),
    ) -> Response:
        allowed_views = {"weekly", "all", "authors", "scirate", "arxiv", "journals", "saved"}
        view = view if view in allowed_views else "weekly"
        page_size = 100 if view == "all" else 50
        items = rank_papers(
            db,
            view=view,
            query=q,
            category=category,
            limit=page_size + 1,
            offset=offset,
        )
        has_more = len(items) > page_size
        ranked = items[:page_size]
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "ranked": ranked,
                "view": view,
                "q": q,
                "category": category,
                "categories": available_categories(db),
                "has_more": has_more,
                "next_offset": offset + page_size,
                "offset": offset,
                "weekly_shortfall": view == "weekly"
                and not q
                and not category
                and len(ranked) < 50,
            },
        )

    @app.get("/papers", response_class=HTMLResponse)
    def load_more_papers(
        request: Request,
        db: DbSession,
        view: str = Query("all"),
        q: str = Query(""),
        category: str = Query(""),
        offset: int = Query(0, ge=0),
    ) -> Response:
        if view != "all":
            return HTMLResponse("只支持继续加载全部更新", status_code=400)
        page_size = 100
        items = rank_papers(
            db,
            view=view,
            query=q,
            category=category,
            limit=page_size + 1,
            offset=offset,
        )
        has_more = len(items) > page_size
        return templates.TemplateResponse(
            request,
            "partials/paper_page.html",
            {
                "ranked": items[:page_size],
                "view": view,
                "q": q,
                "category": category,
                "has_more": has_more,
                "next_offset": offset + page_size,
                "offset": offset,
            },
        )

    @app.post("/papers/{paper_id}/abstract", response_class=HTMLResponse)
    def view_abstract(
        request: Request,
        paper_id: str,
        background_tasks: BackgroundTasks,
        db: DbSession,
    ) -> Response:
        paper = db.scalar(
            select(Paper).options(selectinload(Paper.sources)).where(Paper.id == paper_id)
        )
        if paper is None:
            return HTMLResponse("论文不存在", status_code=404)
        record_interaction(db, paper.id, InteractionKind.ABSTRACT_VIEWED)
        if not paper.abstract.strip():
            background_tasks.add_task(enrich_paper_abstract_in_background, paper.id)
        return templates.TemplateResponse(request, "partials/abstract_panel.html", {"paper": paper})

    @app.post("/papers/{paper_id}/save", response_class=HTMLResponse)
    def toggle_save(request: Request, paper_id: str, db: DbSession) -> Response:
        existing = db.scalar(
            select(Interaction).where(
                Interaction.paper_id == paper_id,
                Interaction.kind == InteractionKind.SAVED,
            )
        )
        if existing:
            remove_interaction(db, paper_id, InteractionKind.SAVED)
            saved = False
        else:
            record_interaction(db, paper_id, InteractionKind.SAVED)
            saved = True
        return templates.TemplateResponse(
            request, "partials/save_button.html", {"paper_id": paper_id, "saved": saved}
        )

    @app.post("/papers/{paper_id}/dismiss")
    def dismiss(paper_id: str, db: DbSession) -> Response:
        if not db.get(Paper, paper_id):
            return HTMLResponse("论文不存在", status_code=404)
        record_interaction(db, paper_id, InteractionKind.DISMISSED)
        return HTMLResponse("")

    @app.get("/papers/{paper_id}/fulltext")
    def fulltext(paper_id: str, db: DbSession) -> Response:
        paper = db.get(Paper, paper_id)
        if paper is None:
            return HTMLResponse("论文不存在", status_code=404)
        target = paper.pdf_url or paper.canonical_url
        if not target:
            return HTMLResponse("这篇论文没有可用的外部链接", status_code=404)
        record_interaction(db, paper_id, InteractionKind.FULLTEXT)
        return RedirectResponse(target, status_code=303)

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        db: DbSession,
        journal_error: str = Query(""),
        sync_started: str = Query(""),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(
                db,
                journal_error=journal_error,
                sync_started=(
                    sync_started if sync_started in DEFAULT_SOURCE_INTERVALS else ""
                ),
            ),
        )

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(request: Request, db: DbSession, interests: str = Form("")) -> Response:
        preferences = get_preferences(db)
        preferences.manual_interests = interests.strip()
        mark_preferences_dirty(db)
        db.commit()
        return templates.TemplateResponse(
            request, "settings.html", settings_context(db, saved=True)
        )

    @app.post("/settings/preferences/rebuild")
    def rebuild_preferences(background_tasks: BackgroundTasks) -> Response:
        background_tasks.add_task(rebuild_preference_profile_in_background, force=True)
        return RedirectResponse("/settings?profile_rebuild=1", status_code=303)

    @app.post("/settings/authors")
    def add_author(
        background_tasks: BackgroundTasks,
        db: DbSession,
        profile_url: str = Form(),
    ) -> Response:
        try:
            author_id = parse_scholar_author_id(profile_url)
        except ValueError:
            return RedirectResponse("/settings?author_error=1", status_code=303)
        author = db.scalar(
            select(TrackedAuthor).where(TrackedAuthor.scholar_author_id == author_id)
        )
        if author is None:
            db.add(
                TrackedAuthor(
                    scholar_author_id=author_id,
                    profile_url=profile_url.strip(),
                    name=f"Scholar {author_id}",
                )
            )
            db.commit()
            if get_settings().serpapi_api_key:
                from .scheduler import run_source_update_in_background

                background_tasks.add_task(run_source_update_in_background, "scholar")
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/authors/{author_id}/delete")
    def delete_author(author_id: str, db: DbSession) -> Response:
        author = db.get(TrackedAuthor, author_id)
        if author:
            db.delete(author)
            db.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/journals")
    def add_journal(
        db: DbSession,
        name: str = Form(),
        feed_url: str = Form(),
        issn: str = Form(""),
    ) -> Response:
        if not _is_public_https(feed_url):
            return RedirectResponse("/settings?journal_error=https", status_code=303)
        exists = db.scalar(
            select(JournalSubscription).where(JournalSubscription.feed_url == feed_url.strip())
        )
        if not exists:
            db.add(
                JournalSubscription(
                    name=name.strip() or urlparse(feed_url).hostname or "期刊",
                    feed_url=feed_url.strip(),
                    issn=issn.strip(),
                )
            )
            db.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/journals/{feed_id}/delete")
    def delete_journal(feed_id: str, db: DbSession) -> Response:
        feed = db.get(JournalSubscription, feed_id)
        if feed:
            db.delete(feed)
            db.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/schedules/{source}")
    def save_source_schedule(
        source: str,
        db: DbSession,
        interval_days: int = Form(),
        enabled: str | None = Form(None),
    ) -> Response:
        if source not in DEFAULT_SOURCE_INTERVALS or not 1 <= interval_days <= 30:
            return RedirectResponse("/settings?schedule_error=1", status_code=303)
        ensure_source_schedules(db)
        schedule = db.get(SourceSchedule, source)
        if schedule:
            schedule.interval_days = interval_days
            schedule.enabled = enabled == "on"
            schedule.next_due_at = (
                next_arxiv_update_at(utcnow())
                if source == "arxiv"
                else utcnow() + timedelta(days=interval_days)
            )
            schedule.updated_at = utcnow()
            db.commit()
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/sync/{source}")
    def run_source_now(source: str, background_tasks: BackgroundTasks) -> Response:
        if source not in DEFAULT_SOURCE_INTERVALS:
            return HTMLResponse("未知来源", status_code=404)
        from .scheduler import run_source_update_in_background

        background_tasks.add_task(
            run_source_update_in_background,
            source,
            source == "scirate",
        )
        return RedirectResponse(f"/settings?sync_started={source}", status_code=303)

    return app


app = create_app()
