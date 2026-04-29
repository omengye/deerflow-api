from pathlib import Path

from pydantic import BaseModel, Field

from deerflow.config.project_root import find_project_root


class SkillsConfig(BaseModel):
    """Configuration for skills system"""

    enabled: bool = Field(default=True, description="Whether the skills system is enabled")
    path: str | None = Field(
        default=None,
        description="Path to skills directory. If not specified, defaults to ../skills relative to backend directory",
    )
    directories: list[str] = Field(
        default_factory=list,
        description="Legacy list of skills directories. The first entry is used when path is not set.",
    )
    extensions_file: str | None = Field(
        default=None,
        description="Legacy path to extensions_config.json; retained for config compatibility.",
    )
    container_path: str = Field(
        default="/mnt/skills",
        description="Path where skills are mounted in the sandbox container",
    )

    def get_skills_path(self) -> Path:
        """
        Get the resolved skills directory path.

        Returns:
            Path to the skills directory
        """
        configured_path = self.path or (self.directories[0] if self.directories else None)
        if configured_path:
            # Use configured path (can be absolute or relative)
            path = Path(configured_path)
            if not path.is_absolute():
                # If relative, resolve from the API project root for deterministic behavior.
                path = find_project_root(Path(__file__)) / path
            return path.resolve()
        else:
            # Default: ../skills relative to backend directory
            from deerflow.skills.loader import get_skills_root_path

            return get_skills_root_path()

    def get_skill_container_path(self, skill_name: str, category: str = "public") -> str:
        """
        Get the full container path for a specific skill.

        Args:
            skill_name: Name of the skill (directory name)
            category: Category of the skill (public or custom)

        Returns:
            Full path to the skill in the container
        """
        return f"{self.container_path}/{category}/{skill_name}"
