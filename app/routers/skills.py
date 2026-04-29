"""Skill management endpoints."""
import logging

from fastapi import APIRouter, HTTPException

from app.dependencies import get_client_manager
from app.schemas import SkillInfo, SkillListResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["skills"])


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
                skills.append(SkillInfo(
                    name=s.get("name", ""),
                    display_name=s.get("display_name", s.get("name", "")),
                    description=s.get("description", ""),
                    enabled=s.get("enabled", False),
                ))
        return SkillListResponse(skills=skills)
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/skills/{skill_name}/enable")
async def enable_skill(skill_name: str):
    """Enable a skill."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        client.update_skill(skill_name, enabled=True)
        return {"success": True, "message": f"Skill '{skill_name}' enabled"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/skills/{skill_name}/disable")
async def disable_skill(skill_name: str):
    """Disable a skill."""
    manager = get_client_manager()
    client = manager.get_client()

    try:
        client.update_skill(skill_name, enabled=False)
        return {"success": True, "message": f"Skill '{skill_name}' disabled"}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("Unhandled error")
        raise HTTPException(status_code=500, detail="Internal server error")
