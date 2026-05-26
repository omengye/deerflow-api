from pydantic import BaseModel, ConfigDict, Field


class VolumeMountConfig(BaseModel):
    """Configuration for a volume mount."""

    host_path: str = Field(..., description="Path on the host machine")
    container_path: str = Field(..., description="Path inside the container")
    read_only: bool = Field(default=False, description="Whether the mount is read-only")


class SandboxConfig(BaseModel):
    """Config section for a sandbox.

    Common options:
        use: Class path of the sandbox provider (required)
        allow_host_bash: Enable host-side bash execution for LocalSandboxProvider.
            Dangerous and intended only for fully trusted local workflows.

    AioSandboxProvider specific options:
        image: Docker image to use (default: enterprise-public-cn-beijing.cr.volces.com/vefaas-public/all-in-one-sandbox:latest)
        port: Base port for sandbox containers (default: 8080)
        replicas: Maximum number of concurrent sandbox containers (default: 3). When the limit is reached the least-recently-used sandbox is evicted to make room.
        container_prefix: Prefix for container names (default: deer-flow-sandbox)
        idle_timeout: Idle timeout in seconds before sandbox is released (default: 600 = 10 minutes). Set to 0 to disable.
        mounts: List of volume mounts to share directories with the container
        environment: Environment variables to inject into the container (values starting with $ are resolved from host env)
        security_opt: List of Docker --security-opt values to apply to the sandbox container
        container_user: User for docker run/exec ('auto' uses host UID:GID, null omits --user, 'UID:GID' sets explicit value)

    LocalWslProvider specific options (Windows only):
        wsl_distro: WSL distro name (e.g. "Ubuntu-22.04"). If None, the default
            distro is used.
        wsl_user: User to run commands as inside the WSL distro. If None, the
            distro's default user is used.
        wsl_shell: Shell executable inside the distro (default: "bash"). Always
            invoked with the ``-lc`` flag so that PATH/venv setup in the user's
            login shell rc files takes effect.
        wsl_mount_prefix: Mount prefix used by WSL for Windows drives (default:
            "/mnt"). Override if the distro's /etc/wsl.conf customizes
            automount.root.
    """

    use: str = Field(
        ...,
        description="Class path of the sandbox provider (e.g. deerflow.sandbox.local:LocalSandboxProvider)",
    )
    allow_host_bash: bool = Field(
        default=False,
        description="Allow the bash tool to execute directly on the host when using LocalSandboxProvider. Dangerous; intended only for fully trusted local environments.",
    )
    image: str | None = Field(
        default=None,
        description="Docker image to use for the sandbox container",
    )
    port: int | None = Field(
        default=None,
        description="Base port for sandbox containers",
    )
    replicas: int | None = Field(
        default=None,
        description="Maximum number of concurrent sandbox containers (default: 3). When the limit is reached the least-recently-used sandbox is evicted to make room.",
    )
    container_prefix: str | None = Field(
        default=None,
        description="Prefix for container names",
    )
    idle_timeout: int | None = Field(
        default=None,
        description="Idle timeout in seconds before sandbox is released (default: 600 = 10 minutes). Set to 0 to disable.",
    )
    mounts: list[VolumeMountConfig] = Field(
        default_factory=list,
        description="List of volume mounts to share directories between host and container",
    )
    environment: dict[str, str] = Field(
        default_factory=dict,
        description="Environment variables to inject into the sandbox container. Values starting with $ will be resolved from host environment variables.",
    )
    security_opt: list[str] = Field(
        default_factory=list,
        description="List of Docker --security-opt values to apply to the sandbox container.",
    )
    container_user: str | None = Field(
        default="auto",
        description=(
            "User to run inside the sandbox container (docker run --user / docker exec -u). "
            "'auto' (default) uses the host process UID:GID so that files written to volume-mounted "
            "thread directories are owned by the current user rather than root. "
            "Set to an explicit 'UID:GID' string to override, or null/empty to omit --user entirely "
            "(reverts to the image's default user, typically root)."
        ),
    )

    bash_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from bash tool output. Output exceeding this limit is middle-truncated (head + tail), preserving the first and last half. Set to 0 to disable truncation.",
    )
    read_file_output_max_chars: int = Field(
        default=50000,
        ge=0,
        description="Maximum characters to keep from read_file tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )
    ls_output_max_chars: int = Field(
        default=20000,
        ge=0,
        description="Maximum characters to keep from ls tool output. Output exceeding this limit is head-truncated. Set to 0 to disable truncation.",
    )

    wsl_distro: str | None = Field(
        default=None,
        description="WSL distro name for LocalWslProvider (e.g. 'Ubuntu-22.04'). None uses the default distro.",
    )
    wsl_user: str | None = Field(
        default=None,
        description="User to run commands as inside the WSL distro. None uses the distro's default user.",
    )
    wsl_shell: str = Field(
        default="bash",
        description="Shell executable inside the WSL distro. Invoked with '-lc' for login-shell semantics.",
    )
    wsl_mount_prefix: str = Field(
        default="/mnt",
        description="Mount prefix used by WSL for Windows drives. Override if /etc/wsl.conf customizes automount.root.",
    )

    model_config = ConfigDict(extra="allow")
