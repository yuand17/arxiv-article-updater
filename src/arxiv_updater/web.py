from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .auth import authenticate, consume_invite, create_user
from .config import get_settings
from .db import get_db, init_db
from .models import User

PACKAGE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
DbSession = Annotated[Session, Depends(get_db)]


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
    def home(request: Request, db: DbSession) -> Response:
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
        return templates.TemplateResponse(request, "home.html", {"user": user})

    return app


app = create_app()
