"""Thread (session) management endpoints."""
import logging
import re
import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from pydantic import BaseModel

from app.dependencies import get_client_manager
from app.schemas import ThreadResponse, ThreadDetail

logger = logging.getLogger(__name__)

router = APIRouter(tags=["threads"])

# Conservative thread_id format: UUIDs and bare alphanumerics with `-`/`_`,
# at most 128 chars. Prevents path-traversal / injection through path params.
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_MAX_LIST_LIMIT = 1000
_CLEANUP_PAGE_SIZE = 500


def _validate_thread_id(thread_id: str) -> None:
    if not thread_id or not _THREAD_ID_RE.match(thread_id):
        raise HTTPException(status_code=400, detail="Invalid thread_id format")


@router.post("/threads", response_model=ThreadResponse)
async def create_thread():
    """Create a new session (returns a fresh UUID)."""
    thread_id = str(uuid.uuid4())
    return ThreadResponse(thread_id=thread_id)


@router.get("/threads", response_model=list[ThreadResponse])
async def list_threads(limit: int = Query(default=10, ge=1, le=_MAX_LIST_LIMIT)):
    """List recent sessions."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        result = client.list_threads(limit=limit)
        threads = []
        thread_list = result.get("thread_list", result.get("threads", []))
        for t in thread_list:
            threads.append(ThreadResponse(
                thread_id=t.get("thread_id", ""),
                title=t.get("title"),
                created_at=t.get("created_at"),
            ))
        return threads
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/threads/{thread_id}", response_model=ThreadDetail)
async def get_thread(thread_id: str):
    """Get full session state including message history."""
    _validate_thread_id(thread_id)
    manager = get_client_manager()
    client = manager.get_client()

    try:
        thread = client.get_thread(thread_id)
        if not thread:
            raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")

        # Support both checkpoint-based and values-based thread formats
        if "checkpoints" in thread:
            # Get the latest checkpoint values
            checkpoints = thread["checkpoints"]
            if checkpoints:
                latest = checkpoints[-1]
                values = latest.get("values", {})
                messages = values.get("messages", [])
                artifacts = values.get("artifacts", [])
                title = values.get("title")
            else:
                messages = []
                artifacts = []
                title = None
        else:
            values = thread.get("values", {})
            messages = values.get("messages", [])
            artifacts = values.get("artifacts", [])
            title = values.get("title")

        serialized = []
        for msg in messages:
            if hasattr(msg, "model_dump"):
                serialized.append(msg.model_dump())
            elif hasattr(msg, "dict"):
                serialized.append(msg.dict())
            elif isinstance(msg, dict):
                serialized.append(msg)
            else:
                serialized.append({"content": str(msg)})

        # Check if currently running
        status = "running" if manager.is_thread_running(thread_id) else "idle"

        return ThreadDetail(
            thread_id=thread_id,
            messages=serialized,
            artifacts=artifacts if isinstance(artifacts, list) else [],
            title=title,
            status=status,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


class ThreadUpdateRequest(BaseModel):
    title: str | None = None
    metadata: dict[str, Any] | None = None


@router.put("/threads/{thread_id}")
async def update_thread(thread_id: str, req: ThreadUpdateRequest = Body()):
    """Update session metadata (title, custom tags, etc.)."""
    _validate_thread_id(thread_id)
    manager = get_client_manager()

    metadata = {}
    if req.title:
        metadata["title"] = req.title
    if req.metadata:
        metadata.update(req.metadata)

    if not metadata:
        raise HTTPException(status_code=400, detail="At least one field (title/metadata) is required")

    result = manager.update_thread_metadata(thread_id, metadata)
    if not result.get("success"):
        raise HTTPException(status_code=500, detail=result.get("detail", "Update failed"))

    return result


@router.delete("/threads/{thread_id}")
async def delete_thread(thread_id: str):
    """Delete a session completely (checkpointer data + file system)."""
    _validate_thread_id(thread_id)
    manager = get_client_manager()

    result = manager.delete_thread_completely(thread_id)
    if not result.get("success"):
        if result.get("running"):
            raise HTTPException(status_code=409, detail=result.get("detail", "Thread is running"))
        raise HTTPException(status_code=500, detail=result.get("detail", "Delete failed"))
    return result


class ThreadStatusResponse(BaseModel):
    thread_id: str
    status: str  # idle / running
    title: str | None = None
    metadata: dict[str, Any] | None = None


@router.get("/threads/{thread_id}/status", response_model=ThreadStatusResponse)
async def get_thread_status(thread_id: str):
    """Get session running status."""
    _validate_thread_id(thread_id)
    manager = get_client_manager()
    client = manager.get_client()

    # Check if currently executing
    if manager.is_thread_running(thread_id):
        return ThreadStatusResponse(thread_id=thread_id, status="running")

    # Try to get the latest checkpoint to confirm existence
    try:
        thread = client.get_thread(thread_id)
        title = None
        custom_metadata = None
        if thread.get("checkpoints"):
            latest = thread["checkpoints"][-1]
            title = latest.get("values", {}).get("title")
            # Extract custom metadata fields (non-reserved keys)
            raw_meta = latest.get("metadata", {})
            reserved = {"source", "step", "parents", "ls_integration", "thread_id"}
            custom_metadata = {k: v for k, v in raw_meta.items() if k not in reserved}
            if not custom_metadata:
                custom_metadata = None
        return ThreadStatusResponse(thread_id=thread_id, status="idle", title=title, metadata=custom_metadata)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found")


class CleanupRequest(BaseModel):
    older_than_days: int = 7
    status_filter: str | None = None  # None = all, "idle" = only idle
    max_pages: int = 20  # safety bound: scan up to max_pages * page_size threads


@router.post("/threads/cleanup")
async def cleanup_threads(req: CleanupRequest = Body()):
    """Batch delete sessions based on age and status.

    Iterates over threads in pages instead of pulling the full list into
    memory. Returns count of deleted sessions.
    """
    from datetime import datetime, timezone

    manager = get_client_manager()
    client = manager.get_client()

    now = datetime.now(timezone.utc)
    cutoff_ts = now.timestamp() - (req.older_than_days * 86400)
    deleted = 0
    pages_scanned = 0

    try:
        while pages_scanned < max(1, req.max_pages):
            pages_scanned += 1
            page_size = _CLEANUP_PAGE_SIZE
            try:
                page = client.list_threads(limit=page_size)
            except TypeError:
                # Older client signatures may not accept limit kwarg.
                page = client.list_threads()
            thread_list = page.get("thread_list", page.get("threads", []))
            if not thread_list:
                break

            page_deleted = 0
            for t in thread_list:
                created_at = t.get("created_at")
                if created_at:
                    try:
                        if "T" in str(created_at):
                            ts = datetime.fromisoformat(str(created_at)).timestamp()
                        else:
                            ts = float(created_at)
                        if ts > cutoff_ts:
                            continue
                    except (ValueError, TypeError):
                        continue

                thread_id = t.get("thread_id")
                if not thread_id or not _THREAD_ID_RE.match(str(thread_id)):
                    continue

                if req.status_filter == "idle" and manager.is_thread_running(thread_id):
                    continue

                result = manager.delete_thread_completely(thread_id)
                if result.get("success"):
                    deleted += 1
                    page_deleted += 1

            # Stop when a full page yields nothing eligible (avoids infinite
            # loop against backends that always return the same window).
            if page_deleted == 0 or len(thread_list) < page_size:
                break

        return {"success": True, "deleted_count": deleted, "pages_scanned": pages_scanned}
    except Exception:
        logger.exception("Unhandled error during cleanup")
        raise HTTPException(status_code=500, detail="Internal server error")
