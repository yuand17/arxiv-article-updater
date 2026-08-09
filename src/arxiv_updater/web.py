import base64
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from .arxiv_schedule import next_arxiv_update_at
from .config import get_settings
from .datetime_utils import format_local_datetime
from .db import get_db, init_db
from .models import (
    ApiUsage,
    Interaction,
    InteractionKind,
    JournalEndpoint,
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
from .services.journal_discovery import (
    JournalDiscoveryError,
    JournalDiscoveryPreview,
    discover_journal,
)
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
TOAST_MESSAGES = {
    "settings_saved": ("success", "设置已保存", "新的篇数从下一次三天精选生效。"),
    "profile_started": ("info", "画像重建已启动", "完成后会自动用于后续推荐。"),
    "author_added": ("success", "重点作者已添加", "已加入后续 Scholar 同步。"),
    "author_removed": ("success", "重点作者已移除", "既有论文不会被立即删除。"),
    "journal_found": ("success", "期刊来源已找到", "请核对预览后确认添加。"),
    "journal_added": ("success", "期刊已添加", "第一次同步已经启动。"),
    "journal_removed": ("success", "期刊已移除", "既有论文将按保留规则处理。"),
    "schedule_saved": ("success", "更新计划已保存", "下次运行时间已经重算。"),
    "sync_started": ("info", "更新已启动", "可在活动记录中查看进度。"),
}


def _add_toast(response: Response, key: str) -> Response:
    level, title, message = TOAST_MESSAGES[key]
    response.headers["HX-Trigger"] = json.dumps(
        {"app:toast": {"level": level, "title": title, "message": message}},
    )
    return response


def _action_response(request: Request, key: str, location: str = "/settings") -> Response:
    if request.headers.get("HX-Request") == "true":
        return _add_toast(Response(status_code=204), key)
    separator = "&" if "?" in location else "?"
    return RedirectResponse(f"{location}{separator}toast={key}", status_code=303)


def _settings_fragment(
    request: Request,
    db: Session,
    key: str,
) -> Response:
    if request.headers.get("HX-Request") == "true":
        response = templates.TemplateResponse(
            request, "settings.html", settings_context(db)
        )
        return _add_toast(response, key)
    return RedirectResponse(f"/settings?toast={key}", status_code=303)


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


def _encode_cursor(value: datetime, item_id: str) -> str:
    payload = json.dumps([value.isoformat(), item_id], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str) -> tuple[datetime, str] | None:
    if not value:
        return None
    try:
        padding = "=" * (-len(value) % 4)
        timestamp, item_id = json.loads(base64.urlsafe_b64decode(value + padding))
        return datetime.fromisoformat(timestamp), str(item_id)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _sync_run_page(
    db: Session, cursor: str = "", limit: int = 20
) -> tuple[list[SyncRun], str]:
    statement = select(SyncRun)
    decoded = _decode_cursor(cursor)
    if decoded:
        before, item_id = decoded
        statement = statement.where(
            or_(
                SyncRun.started_at < before,
                and_(SyncRun.started_at == before, SyncRun.id < item_id),
            )
        )
    rows = db.scalars(
        statement.order_by(SyncRun.started_at.desc(), SyncRun.id.desc()).limit(limit + 1)
    ).all()
    page = list(rows[:limit])
    next_cursor = (
        _encode_cursor(page[-1].started_at, page[-1].id) if len(rows) > limit and page else ""
    )
    return page, next_cursor


def _usage_page(
    db: Session, cursor: str = "", limit: int = 20
) -> tuple[list[ApiUsage], str]:
    statement = select(ApiUsage)
    decoded = _decode_cursor(cursor)
    if decoded:
        before, item_id = decoded
        statement = statement.where(
            or_(
                ApiUsage.created_at < before,
                and_(ApiUsage.created_at == before, ApiUsage.id < item_id),
            )
        )
    rows = db.scalars(
        statement.order_by(ApiUsage.created_at.desc(), ApiUsage.id.desc()).limit(limit + 1)
    ).all()
    page = list(rows[:limit])
    next_cursor = (
        _encode_cursor(page[-1].created_at, page[-1].id) if len(rows) > limit and page else ""
    )
    return page, next_cursor


def settings_context(
    db: Session,
    *,
    saved: bool = False,
    journal_error: str = "",
    journal_preview: JournalDiscoveryPreview | None = None,
    sync_started: str = "",
) -> dict[str, object]:
    ensure_source_schedules(db)
    preferences = get_preferences(db)
    settings = get_settings()
    runs, run_next_cursor = _sync_run_page(db)
    usage_details, usage_next_cursor = _usage_page(db)
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
        "runs": runs,
        "run_next_cursor": run_next_cursor,
        "usage_details": usage_details,
        "usage_next_cursor": usage_next_cursor,
        "usage": db.execute(
            select(
                ApiUsage.service,
                func.sum(ApiUsage.request_count),
                func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
            ).group_by(ApiUsage.service)
        ).all(),
        "service_configured": {
            "SerpAPI": bool(settings.serpapi_api_key),
            "DeepSeek": bool(settings.deepseek_api_key),
        },
        "saved": saved,
        "journal_error": journal_error,
        "journal_preview": journal_preview,
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
                scheduler.shutdown(wait=True)

    app = FastAPI(title="arXiv Updater", lifespan=lifespan)
    app.state.journal_previews = {}
    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def home(
        request: Request,
        db: DbSession,
        view: str = Query("featured"),
        q: str = Query(""),
        category: str = Query(""),
        offset: int = Query(0, ge=0),
    ) -> Response:
        if view == "weekly":
            return RedirectResponse("/?view=featured", status_code=303)
        allowed_views = {"featured", "all", "authors", "scirate", "arxiv", "journals", "saved"}
        view = view if view in allowed_views else "featured"
        page_size = get_preferences(db).featured_paper_count if view == "featured" else 100
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
                "featured_shortfall": view == "featured"
                and not q
                and not category
                and len(ranked) < page_size,
                "featured_target": page_size,
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
        pageable_views = {"all", "authors", "scirate", "arxiv", "journals", "saved"}
        if view not in pageable_views:
            return HTMLResponse("这个视图不支持继续加载", status_code=400)
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

    @app.get("/settings/activity/sync-runs", response_class=HTMLResponse)
    def more_sync_runs(request: Request, db: DbSession, cursor: str = Query("")) -> Response:
        runs, next_cursor = _sync_run_page(db, cursor)
        return templates.TemplateResponse(
            request,
            "partials/sync_run_rows.html",
            {"runs": runs, "run_next_cursor": next_cursor},
        )

    @app.get("/settings/activity/api-usage", response_class=HTMLResponse)
    def more_api_usage(request: Request, db: DbSession, cursor: str = Query("")) -> Response:
        usage_details, next_cursor = _usage_page(db, cursor)
        return templates.TemplateResponse(
            request,
            "partials/api_usage_rows.html",
            {"usage_details": usage_details, "usage_next_cursor": next_cursor},
        )

    @app.post("/settings", response_class=HTMLResponse)
    def save_settings(
        request: Request,
        db: DbSession,
        interests: str = Form(""),
        featured_paper_count: int = Form(66),
    ) -> Response:
        if not 1 <= featured_paper_count <= 200:
            return HTMLResponse("三天精选篇数必须在 1 到 200 之间", status_code=422)
        preferences = get_preferences(db)
        preferences.manual_interests = interests.strip()
        preferences.featured_paper_count = featured_paper_count
        mark_preferences_dirty(db)
        db.commit()
        return _action_response(request, "settings_saved")

    @app.post("/settings/preferences/rebuild")
    def rebuild_preferences(request: Request, background_tasks: BackgroundTasks) -> Response:
        background_tasks.add_task(rebuild_preference_profile_in_background, force=True)
        return _action_response(request, "profile_started")

    @app.post("/settings/authors")
    def add_author(
        request: Request,
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
        return _settings_fragment(request, db, "author_added")

    @app.post("/settings/authors/{author_id}/delete")
    def delete_author(request: Request, author_id: str, db: DbSession) -> Response:
        author = db.get(TrackedAuthor, author_id)
        if author:
            db.delete(author)
            db.commit()
        return _settings_fragment(request, db, "author_removed")

    @app.post("/settings/journals/discover", response_class=HTMLResponse)
    def preview_journal(
        request: Request,
        db: DbSession,
        name: str = Form(),
        homepage_url: str = Form(),
    ) -> Response:
        try:
            preview = discover_journal(name, homepage_url)
        except JournalDiscoveryError as exc:
            response = templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, journal_error=str(exc)),
                status_code=422,
            )
            response.headers["HX-Trigger"] = json.dumps(
                {
                    "app:toast": {
                        "level": "error",
                        "title": "期刊发现失败",
                        "message": str(exc),
                    }
                },
            )
            return response
        previews: dict[str, JournalDiscoveryPreview] = app.state.journal_previews
        previews[preview.token] = preview
        while len(previews) > 10:
            previews.pop(next(iter(previews)))
        response = templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(db, journal_preview=preview),
        )
        return _add_toast(response, "journal_found")

    @app.post("/settings/journals/confirm")
    def confirm_journal(
        request: Request,
        background_tasks: BackgroundTasks,
        db: DbSession,
        token: str = Form(),
    ) -> Response:
        previews: dict[str, JournalDiscoveryPreview] = app.state.journal_previews
        preview = previews.pop(token, None)
        if preview is None:
            return RedirectResponse("/settings?journal_error=expired", status_code=303)
        exists = db.scalar(
            select(JournalSubscription).where(
                JournalSubscription.homepage_url == preview.homepage_url
            )
        )
        if not exists:
            subscription = JournalSubscription(
                name=preview.name,
                homepage_url=preview.homepage_url,
                canonical_domain=preview.canonical_domain,
                issn_online=preview.issn_online,
                issn_print=preview.issn_print,
                scope_kind=preview.scope_kind,
                discovery_status="verified",
                discovery_version=preview.version,
                last_discovered_at=utcnow(),
            )
            db.add(subscription)
            db.flush()
            db.add_all(
                [
                    JournalEndpoint(
                        journal_subscription_id=subscription.id,
                        kind=endpoint.kind,
                        url=endpoint.url,
                        priority=endpoint.priority,
                        last_validated_at=utcnow(),
                    )
                    for endpoint in preview.endpoints
                ]
            )
            db.commit()
            from .scheduler import run_source_update_in_background

            background_tasks.add_task(run_source_update_in_background, "journals")
        return _settings_fragment(request, db, "journal_added")

    @app.post("/settings/journals/{feed_id}/delete")
    def delete_journal(request: Request, feed_id: str, db: DbSession) -> Response:
        feed = db.get(JournalSubscription, feed_id)
        if feed:
            db.delete(feed)
            db.commit()
        return _settings_fragment(request, db, "journal_removed")

    @app.post("/settings/schedules/{source}")
    def save_source_schedule(
        request: Request,
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
        return _settings_fragment(request, db, "schedule_saved")

    @app.post("/settings/sync/{source}")
    def run_source_now(
        request: Request, source: str, background_tasks: BackgroundTasks
    ) -> Response:
        if source not in DEFAULT_SOURCE_INTERVALS:
            return HTMLResponse("未知来源", status_code=404)
        from .scheduler import run_source_update_in_background

        background_tasks.add_task(
            run_source_update_in_background,
            source,
            source == "scirate",
        )
        response = _action_response(request, "sync_started", f"/settings?sync_started={source}")
        if request.headers.get("HX-Request") == "true":
            level, title, message = TOAST_MESSAGES["sync_started"]
            response.headers["HX-Trigger"] = json.dumps(
                {
                    "app:toast": {"level": level, "title": title, "message": message},
                    "app:sync-started": {
                        "source": source,
                        "after": utcnow().replace(tzinfo=None).isoformat(),
                    },
                }
            )
        return response

    @app.get("/settings/sync/{source}/status", response_class=JSONResponse)
    def source_sync_status(
        source: str,
        db: DbSession,
        after: Annotated[datetime, Query()],
    ) -> JSONResponse:
        if source not in DEFAULT_SOURCE_INTERVALS:
            return JSONResponse({"status": "unknown"}, status_code=404)
        run = db.scalar(
            select(SyncRun)
            .where(SyncRun.source == source, SyncRun.started_at >= after)
            .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        )
        if run is None or run.status == "running":
            return JSONResponse({"status": "pending"})
        return JSONResponse(
            {
                "status": run.status.value,
                "message": run.error or "",
                "items_seen": run.items_seen,
                "items_created": run.items_created,
            }
        )

    return app


app = create_app()
