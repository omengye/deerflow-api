"""DeerFlow API Service — FastAPI wrapper around DeerFlow harness."""
import sys
import asyncio
from pathlib import Path

# Python 3.14 on Windows: ProactorEventLoop transport.close() raises
# RuntimeError('Event loop is closed') during async stream cleanup (anyio/httpx).
# WindowsSelectorEventLoopPolicy avoids this entirely.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Load .env from project root before any config/settings are imported.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:
    pass

# Prefer the embedded deerflow package in this API project. Fall back to the
# original harness checkout only when the embedded package is absent.
_project_root = Path(__file__).resolve().parent.parent
_embedded_harness_package = _project_root / "deerflow"
_harness_path = _project_root.parent / "deer-flow" / "backend" / "packages" / "harness"
if not _embedded_harness_package.exists() and str(_harness_path) not in sys.path:
    sys.path.insert(0, str(_harness_path))

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.dependencies import get_client_manager
from app.middleware import ApiKeyAuthMiddleware, RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle."""
    manager = get_client_manager()
    await manager.startup()

    feishu_channel = None
    if settings.feishu and settings.feishu.enabled and settings.feishu.app_id:
        try:
            from app.channels.feishu import FeishuChannel
            feishu_channel = FeishuChannel(
                app_id=settings.feishu.app_id,
                app_secret=settings.feishu.app_secret,
                verification_token=settings.feishu.verification_token,
            )
            await feishu_channel.start(asyncio.get_running_loop())
        except ImportError:
            import logging
            logging.getLogger(__name__).warning(
                "lark-oapi not installed; Feishu channel disabled. "
                'Install with: uv pip install "deerflow-api[feishu]"'
            )
        except Exception:
            import logging
            logging.getLogger(__name__).exception("Failed to start Feishu channel")

    yield

    if feishu_channel is not None:
        await feishu_channel.astop()
    await manager.shutdown()


app = FastAPI(
    title="DeerFlow API",
    description="API service built on DeerFlow agent harness",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(ApiKeyAuthMiddleware)
app.add_middleware(RequestContextMiddleware)

# Import routers
from app.routers import chat, threads, models, skills, mcp, uploads, runs  # noqa: E402

app.include_router(chat.router, prefix="/api")
app.include_router(threads.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")
app.include_router(runs.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/health/ready")
def readiness():
    manager = get_client_manager()
    payload = manager.readiness_check()
    if payload.get("status") != "ok":
        raise HTTPException(status_code=503, detail=payload)
    return payload
