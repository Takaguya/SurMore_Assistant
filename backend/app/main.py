import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.datastructures import MutableHeaders

from . import db
from .auth import User, current_user
from .config import get_settings
from .routers import chat, documents, knowledge_bases
from .schemas import Health

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


settings = get_settings()
PRODUCTION = settings.environment == "production"


def check_production_settings() -> None:
    """Refuse to serve the public with settings that would expose everyone's data."""
    if not PRODUCTION:
        return
    missing = []
    if settings.auth_mode == "none":
        missing.append("AUTH_MODE=supabase (with sign-in off, every visitor shares and can delete all data)")
    elif not (settings.supabase_url and settings.supabase_anon_key):
        missing.append("SUPABASE_URL and SUPABASE_ANON_KEY")
    if missing:
        raise RuntimeError("ENVIRONMENT=production but these settings are missing: " + "; ".join(missing))


@asynccontextmanager
async def lifespan(_: FastAPI):
    check_production_settings()
    db.init_db()
    yield
    db.close_db()


app = FastAPI(
    title="SurMore Assistant API",
    lifespan=lifespan,
    # The interactive API docs are handy locally but just add surface area on a public site.
    docs_url=None if PRODUCTION else "/docs",
    redoc_url=None,
    openapi_url=None if PRODUCTION else "/openapi.json",
)


class SecurityHeaders:
    """Plain ASGI middleware (it doesn't buffer, so streamed chat answers pass straight through)."""

    def __init__(self, app):
        self.app = app
        connect = " ".join(filter(None, ["'self'", settings.supabase_url.rstrip("/")]))
        self.csp = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; font-src 'self' data:; "
            f"connect-src {connect}; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("X-Content-Type-Options", "nosniff")
                headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
                headers.setdefault("X-Frame-Options", "DENY")
                if PRODUCTION:
                    headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                    if headers.get("content-type", "").startswith("text/html"):
                        headers.setdefault("Content-Security-Policy", self.csp)
            await send(message)

        await self.app(scope, receive, send_with_headers)


app.add_middleware(SecurityHeaders)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origin_list,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Every data endpoint needs a signed-in user (or the "local" user when AUTH_MODE=none).
for router in (knowledge_bases.router, documents.router, chat.router):
    app.include_router(router, dependencies=[Depends(current_user)])


class PublicConfig(BaseModel):
    auth_mode: str
    supabase_url: str
    supabase_anon_key: str
    auth_providers: list[str]


class Me(BaseModel):
    id: str
    email: str | None


@app.get("/api/public-config", response_model=PublicConfig)
def public_config():
    """What the browser needs before sign-in. The anon key is designed to be public."""
    return PublicConfig(
        auth_mode=settings.auth_mode,
        supabase_url=settings.supabase_url.rstrip("/"),
        supabase_anon_key=settings.supabase_anon_key,
        auth_providers=settings.auth_provider_list,
    )


@app.get("/api/me", response_model=Me)
def me(user: User = Depends(current_user)):
    return Me(id=user.id, email=user.email)


@app.get("/api/health", response_model=Health)
def health():
    s = get_settings()
    return Health(
        api_key_configured=bool(s.nvidia_api_key),
        llm_model=s.nvidia_llm_model,
        embedding_model=s.nvidia_embedding_model,
        ocr_model=s.nvidia_ocr_model,
        ocr_mode=s.ocr_mode,
        max_upload_mb=s.max_upload_mb,
    )


# After `npm run build`, the backend also serves the frontend, so one process runs the whole app.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
