"""Run lifecycle endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Annotated, Literal

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

from app.artifact_archive import (
    ArtifactArchiveError,
    ArtifactArchiveResult,
    build_artifact_archive,
)
from app.dependencies import get_client_manager
from deerflow.config.app_config import get_app_config
from deerflow.config.paths import get_paths
from deerflow.runtime import RunStatus

logger = logging.getLogger(__name__)
router = APIRouter(tags=["runs"])
_artifact_archive_slots = asyncio.Semaphore(4)


class CancelRunRequest(BaseModel):
    action: Literal["interrupt", "rollback"] = "interrupt"


def _archive_response_chunks(result: ArtifactArchiveResult):
    try:
        while chunk := result.file.read(1024 * 1024):
            yield chunk
    finally:
        result.file.close()


async def _build_archive_without_abandoning_worker(
    outputs_dir,
    user_data_dir,
    artifacts: list[str],
    *,
    extra_reserved_dir_names: set[str],
) -> ArtifactArchiveResult:
    if _artifact_archive_slots.locked():
        raise ArtifactArchiveError(
            "Too many artifact archives are being created; try again shortly",
            429,
        )
    await _artifact_archive_slots.acquire()
    build_task = asyncio.create_task(
        asyncio.to_thread(
            build_artifact_archive,
            outputs_dir,
            artifacts,
            user_data_dir=user_data_dir,
            extra_reserved_dir_names=extra_reserved_dir_names,
        )
    )
    try:
        return await asyncio.shield(build_task)
    except asyncio.CancelledError:
        while not build_task.done():
            try:
                await asyncio.shield(build_task)
            except asyncio.CancelledError:
                continue
            except Exception:  # noqa: BLE001 - wait for the worker before releasing its slot
                break
        if not build_task.cancelled():
            try:
                build_task.result().file.close()
            except Exception:
                logger.debug(
                    "Failed to close cancelled artifact archive", exc_info=True
                )
        raise
    finally:
        _artifact_archive_slots.release()


def _run_artifacts(record) -> list[str]:
    artifacts = (
        record.metadata.get("artifacts") if isinstance(record.metadata, dict) else None
    )
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or any(not isinstance(path, str) for path in artifacts)
    ):
        raise HTTPException(
            status_code=409,
            detail="This run has no recorded artifact delivery",
        )
    return artifacts


async def _resolve_terminal_run(thread_id: str, run_id: str):
    manager = get_client_manager()
    record = manager.run_manager.get(run_id)
    if record is None or record.thread_id != thread_id:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    if record.status in {RunStatus.pending, RunStatus.running}:
        raise HTTPException(status_code=409, detail="This run has not finished")
    return manager, record


def _run_to_response(record) -> dict:
    return {
        "run_id": record.run_id,
        "thread_id": record.thread_id,
        "assistant_id": record.assistant_id,
        "status": record.status.value
        if hasattr(record.status, "value")
        else str(record.status),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "error": record.error,
        "metadata": record.metadata,
    }


@router.get("/runs/{run_id}")
async def get_run(run_id: str):
    """Get the current status for a run."""
    manager = get_client_manager()
    record = manager.run_manager.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
    return _run_to_response(record)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: str,
    req: Annotated[CancelRunRequest | None, Body()] = None,
):
    """Cancel an in-flight run."""
    req = req or CancelRunRequest()
    manager = get_client_manager()
    record = manager.run_manager.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    cancelled = await manager.cancel_run(run_id, action=req.action)
    if not cancelled:
        if record.status in {RunStatus.pending, RunStatus.running}:
            raise HTTPException(
                status_code=409, detail=f"Run {run_id} could not be cancelled"
            )
        raise HTTPException(status_code=409, detail=f"Run {run_id} is already terminal")

    updated = manager.run_manager.get(run_id) or record
    return {"success": True, "run": _run_to_response(updated)}


@router.get("/threads/{thread_id}/runs/{run_id}/artifacts/archive")
async def get_run_artifact_archive_manifest(thread_id: str, run_id: str):
    """Return the number of run-recorded files eligible for an archive."""
    _, record = await _resolve_terminal_run(thread_id, run_id)
    return {"file_count": len(dict.fromkeys(_run_artifacts(record)))}


@router.post("/threads/{thread_id}/runs/{run_id}/artifacts/archive")
async def create_run_artifact_archive(thread_id: str, run_id: str):
    """Download a validated ZIP containing artifacts recorded for one run."""
    manager, record = await _resolve_terminal_run(thread_id, run_id)
    artifacts = _run_artifacts(record)
    if not manager.try_reserve_artifact_archive(thread_id):
        raise HTTPException(
            status_code=409,
            detail="Artifacts are currently being modified; try again shortly",
        )

    config = get_app_config()
    storage_subdir = getattr(
        getattr(config, "tool_output", None), "storage_subdir", None
    )
    reserved = {storage_subdir} if isinstance(storage_subdir, str) else set()
    paths = get_paths()
    outputs_dir = paths.sandbox_outputs_dir(thread_id)
    user_data_dir = paths.sandbox_user_data_dir(thread_id)
    try:
        try:
            result = await _build_archive_without_abandoning_worker(
                outputs_dir,
                user_data_dir,
                artifacts,
                extra_reserved_dir_names=reserved,
            )
        except ArtifactArchiveError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    finally:
        manager.release_artifact_archive(thread_id)

    safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "", run_id)[:32] or "run"
    logger.info(
        "Created artifact archive thread_id=%s run_id=%s members=%d input_bytes=%d output_bytes=%d",
        thread_id,
        run_id,
        result.member_count,
        result.input_bytes,
        result.size,
    )
    return StreamingResponse(
        _archive_response_chunks(result),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="artifacts-{safe_run_id}.zip"',
            "Content-Length": str(result.size),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        },
        background=BackgroundTask(result.file.close),
    )
