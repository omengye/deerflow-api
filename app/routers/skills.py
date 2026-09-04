"""Skill management endpoints."""
import asyncio
import logging
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.dependencies import get_client_manager
from app.schemas import SkillInfo, SkillListResponse
from deerflow.skills import SkillAlreadyExistsError, SkillSecurityScanError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])

_MAX_SKILL_ARCHIVE_UPLOAD_BYTES = 100 * 1024 * 1024
_UPLOAD_COPY_CHUNK_BYTES = 1024 * 1024


@router.post("/skills/install/upload")
async def upload_and_install_skill(
    archive: UploadFile = File(...),
):
    """Upload a local .skill archive and install it as a custom skill."""
    filename = archive.filename or ""
    if not filename.casefold().endswith(".skill"):
        raise HTTPException(
            status_code=400,
            detail="Skill archive filename must end with .skill",
        )

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="deerflow-skill-",
            suffix=".skill",
            delete=False,
        ) as target:
            temporary_path = Path(target.name)
            total = 0
            while chunk := await archive.read(_UPLOAD_COPY_CHUNK_BYTES):
                total += len(chunk)
                if total > _MAX_SKILL_ARCHIVE_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            "Skill archive exceeds the "
                            f"{_MAX_SKILL_ARCHIVE_UPLOAD_BYTES // (1024 * 1024)} MiB upload limit"
                        ),
                    )
                target.write(chunk)

        manager = get_client_manager()
        client = manager.get_client()
        return await asyncio.to_thread(client.install_skill, temporary_path)
    except SkillAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except SkillSecurityScanError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    finally:
        await archive.close()
        if temporary_path is not None:
            await asyncio.to_thread(temporary_path.unlink, missing_ok=True)


@router.get("/skills", response_model=SkillListResponse)
async def list_skills(enabled_only: bool = False):
    """List all available skills."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        result = client.list_skills(enabled_only=enabled_only)
        skills = []
        if isinstance(result, dict) and "skills" in result:
            for s in result["skills"]:
                category = s.get("category", "public")
                if category not in ("public", "custom"):
                    category = "public"
                skills.append(SkillInfo(
                    name=s.get("name", ""),
                    display_name=s.get("display_name", s.get("name", "")),
                    description=s.get("description", ""),
                    category=category,
                    enabled=s.get("enabled", False),
                ))
        return SkillListResponse(skills=skills)
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/skills/{skill_name}/enable")
async def enable_skill(skill_name: str):
    """Enable a skill."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        await asyncio.to_thread(client.update_skill, skill_name, enabled=True)
        return {"success": True, "message": f"Skill '{skill_name}' enabled"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/skills/{skill_name}/disable")
async def disable_skill(skill_name: str):
    """Disable a skill."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        await asyncio.to_thread(client.update_skill, skill_name, enabled=False)
        return {"success": True, "message": f"Skill '{skill_name}' disabled"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
