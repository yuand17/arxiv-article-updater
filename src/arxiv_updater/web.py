import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlparse

from fastapi import BackgroundTasks, Depends, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from .arxiv_schedule import next_arxiv_update_at
from .config import get_external_service_states, get_settings
from .datetime_utils import format_local_datetime
from .db import get_db, init_db
from .external_services import (
    SUPPORTED_SERVICES,
    CredentialStoreError,
    CredentialValidationError,
    ServiceName,
    clear_external_service,
    public_service_view,
    save_external_service,
)
from .models import (
    ApiUsage,
    Interaction,
    InteractionKind,
    Paper,
    SourceSchedule,
    SyncRun,
    TrackedAuthor,
    utcnow,
)
from .scheduler import DEFAULT_SOURCE_INTERVALS, ensure_source_schedules
from .services.abstracts import enrich_paper_abstract_in_background
from .services.interactions import record_interaction, remove_interaction
from .services.journal_catalog import ensure_builtin_journals
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
    "journal_subscription_saved": (
        "success",
        "期刊订阅已更新",
        "开关已经生效；既有论文会继续保留。",
    ),
    "schedule_saved": ("success", "更新计划已保存", "下次运行时间已经重算。"),
    "sync_started": ("info", "更新已启动", "可在活动记录中查看进度。"),
    "service_saved": ("success", "外部服务设置已保存", "新的开关状态已立即生效。"),
    "service_cleared": ("success", "API key 已清除", "服务已关闭，安全存储中的密钥已删除。"),
}
ACTIVITY_WINDOW_DAYS = 7
ACTIVITY_ROW_LIMIT = 100
LOCAL_WEB_HOSTS = {"127.0.0.1", "localhost", "::1"}


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


def _local_form_request_is_trusted(request: Request) -> bool:
    """Reject browser cross-site posts to credential-changing endpoints."""

    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return False
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    try:
        return urlparse(source).hostname in LOCAL_WEB_HOSTS
    except ValueError:
        return False


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


def settings_context(
    db: Session,
    *,
    saved: bool = False,
    service_error: str = "",
    sync_started: str = "",
) -> dict[str, object]:
    ensure_source_schedules(db)
    journal_feeds = ensure_builtin_journals(db)
    preferences = get_preferences(db)
    activity_cutoff = utcnow() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    accurate_api_usage = or_(
        ApiUsage.service != "serpapi",
        ApiUsage.operation != "author_sync",
    )
    runs = db.scalars(
        select(SyncRun)
        .where(SyncRun.started_at >= activity_cutoff)
        .order_by(SyncRun.started_at.desc(), SyncRun.id.desc())
        .limit(ACTIVITY_ROW_LIMIT)
    ).all()
    usage_details = db.scalars(
        select(ApiUsage)
        .where(ApiUsage.created_at >= activity_cutoff, accurate_api_usage)
        .order_by(ApiUsage.created_at.desc(), ApiUsage.id.desc())
        .limit(ACTIVITY_ROW_LIMIT)
    ).all()
    service_states = get_external_service_states()
    return {
        "preferences": preferences,
        "authors": db.scalars(
            select(TrackedAuthor).order_by(
                TrackedAuthor.citation_count.desc(),
                func.lower(TrackedAuthor.name),
                TrackedAuthor.created_at,
            )
        ).all(),
        "journal_feeds": journal_feeds,
        "schedules": db.scalars(select(SourceSchedule).order_by(SourceSchedule.source)).all(),
        "runs": runs,
        "usage_details": usage_details,
        "usage": db.execute(
            select(
                ApiUsage.service,
                func.sum(ApiUsage.request_count),
                func.sum(ApiUsage.input_tokens + ApiUsage.output_tokens),
            )
            .where(ApiUsage.created_at >= activity_cutoff, accurate_api_usage)
            .group_by(ApiUsage.service)
        ).all(),
        "external_services": {
            name: public_service_view(state) for name, state in service_states.items()
        },
        "activity_window_days": ACTIVITY_WINDOW_DAYS,
        "activity_row_limit": ACTIVITY_ROW_LIMIT,
        "saved": saved,
        "service_error": service_error,
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
        sync_started: str = Query(""),
    ) -> Response:
        return templates.TemplateResponse(
            request,
            "settings.html",
            settings_context(
                db,
                sync_started=(
                    sync_started if sync_started in DEFAULT_SOURCE_INTERVALS else ""
                ),
            ),
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
    def rebuild_preferences(
        request: Request,
        background_tasks: BackgroundTasks,
        db: DbSession,
    ) -> Response:
        if not get_settings().deepseek_api_key:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error="请先开启 DeepSeek 并保存 API key。"),
                status_code=422,
            )
        background_tasks.add_task(rebuild_preference_profile_in_background, force=True)
        return _action_response(request, "profile_started")

    @app.post("/settings/services/{service}")
    def save_optional_service(
        request: Request,
        service: str,
        db: DbSession,
        enabled: str | None = Form(None),
        api_key: str = Form(""),
    ) -> Response:
        if not _local_form_request_is_trusted(request):
            return HTMLResponse("已阻止跨站设置请求", status_code=403)
        if service not in SUPPORTED_SERVICES:
            return HTMLResponse("未知外部服务", status_code=404)
        service_name = cast(ServiceName, service)
        try:
            state = save_external_service(
                service_name,
                enabled=enabled == "on",
                new_api_key=api_key,
            )
        except (CredentialStoreError, CredentialValidationError) as exc:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error=str(exc)),
                status_code=422,
            )
        get_settings.cache_clear()
        if service_name == "serpapi":
            ensure_source_schedules(db)
            schedule = db.get(SourceSchedule, "scholar")
            if schedule:
                schedule.enabled = state.enabled
                schedule.next_due_at = utcnow() if state.enabled else None
                schedule.last_error = ""
                schedule.updated_at = utcnow()
                db.commit()
        return _action_response(request, "service_saved")

    @app.post("/settings/services/{service}/clear")
    def clear_optional_service(request: Request, service: str, db: DbSession) -> Response:
        if not _local_form_request_is_trusted(request):
            return HTMLResponse("已阻止跨站设置请求", status_code=403)
        if service not in SUPPORTED_SERVICES:
            return HTMLResponse("未知外部服务", status_code=404)
        service_name = cast(ServiceName, service)
        try:
            clear_external_service(service_name)
        except CredentialStoreError as exc:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error=str(exc)),
                status_code=422,
            )
        get_settings.cache_clear()
        if service_name == "serpapi":
            ensure_source_schedules(db)
        return _action_response(request, "service_cleared")

    @app.post("/settings/authors")
    def add_author(
        request: Request,
        background_tasks: BackgroundTasks,
        db: DbSession,
        profile_url: str = Form(),
    ) -> Response:
        if not get_settings().serpapi_api_key:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error="请先开启 SerpAPI 并保存 API key。"),
                status_code=422,
            )
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

    @app.post("/settings/journals/{feed_id}/toggle")
    def toggle_journal(
        request: Request,
        feed_id: str,
        db: DbSession,
        enabled: str | None = Form(None),
    ) -> Response:
        builtin_feeds = ensure_builtin_journals(db)
        feed = next((item for item in builtin_feeds if item.id == feed_id), None)
        if feed is None:
            return HTMLResponse("未知内置期刊", status_code=404)
        feed.is_active = enabled == "on"
        if feed.is_active:
            feed.last_error = ""
        db.commit()
        return _settings_fragment(request, db, "journal_subscription_saved")

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
        if source == "journals":
            interval_days = 1
            enabled = "on"
        if source == "scholar" and not get_settings().serpapi_api_key:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error="SerpAPI 未启用，不能开启 Scholar 更新。"),
                status_code=422,
            )
        ensure_source_schedules(db)
        schedule = db.get(SourceSchedule, source)
        if schedule:
            schedule.interval_days = interval_days
            schedule.enabled = True if source in {"scholar", "journals"} else enabled == "on"
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
        request: Request,
        source: str,
        background_tasks: BackgroundTasks,
        db: DbSession,
    ) -> Response:
        if source not in DEFAULT_SOURCE_INTERVALS and source != "all":
            return HTMLResponse("未知来源", status_code=404)
        if source == "scholar" and not get_settings().serpapi_api_key:
            return templates.TemplateResponse(
                request,
                "settings.html",
                settings_context(db, service_error="SerpAPI 未启用，已阻止 Scholar 更新。"),
                status_code=422,
            )
        from .scheduler import (
            run_all_source_updates_in_background,
            run_source_update_in_background,
        )

        if source == "all":
            background_tasks.add_task(run_all_source_updates_in_background)
        else:
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
        if source not in DEFAULT_SOURCE_INTERVALS and source != "all":
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
