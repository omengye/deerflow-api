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
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.dependencies import get_client_manager


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

# Import routers
from app.routers import chat, threads, models, skills, mcp, uploads  # noqa: E402

app.include_router(chat.router, prefix="/api")
app.include_router(threads.router, prefix="/api")
app.include_router(models.router, prefix="/api")
app.include_router(skills.router, prefix="/api")
app.include_router(mcp.router, prefix="/api")
app.include_router(uploads.router, prefix="/api")


@app.get("/health")
def health():
    return {"status": "ok"}
