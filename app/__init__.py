"""DeerFlow API Service — FastAPI wrapper around DeerFlow harness."""
import sys
from pathlib import Path

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
    # Initialize client manager
    manager = get_client_manager()
    await manager.startup()
    yield
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
