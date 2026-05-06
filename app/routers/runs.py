"""Run lifecycle endpoints."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel

from app.dependencies import get_client_manager
from deerflow.runtime import RunStatus

router = APIRouter(tags=["runs"])


class CancelRunRequest(BaseModel):
    action: Literal["interrupt", "rollback"] = "interrupt"


def _run_to_response(record) -> dict:
    return {
        "run_id": record.run_id,
        "thread_id": record.thread_id,
        "assistant_id": record.assistant_id,
        "status": record.status.value if hasattr(record.status, "value") else str(record.status),
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
async def cancel_run(run_id: str, req: CancelRunRequest = Body(default_factory=CancelRunRequest)):
    """Cancel an in-flight run."""
    manager = get_client_manager()
    record = manager.run_manager.get(run_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    cancelled = await manager.cancel_run(run_id, action=req.action)
    if not cancelled:
        if record.status in {RunStatus.pending, RunStatus.running}:
            raise HTTPException(status_code=409, detail=f"Run {run_id} could not be cancelled")
        raise HTTPException(status_code=409, detail=f"Run {run_id} is already terminal")

    updated = manager.run_manager.get(run_id) or record
    return {"success": True, "run": _run_to_response(updated)}
