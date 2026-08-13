"""Security helpers for sandbox capability gating."""

from deerflow.config import get_app_config
from deerflow.sandbox.provider_paths import is_host_fs_sandbox_provider_path

LOCAL_HOST_BASH_DISABLED_MESSAGE = (
    "Bash execution is disabled for this host-filesystem-backed sandbox because it is not "
    "a secure isolation boundary. Switch to AioSandboxProvider for isolated bash access, "
    "or set sandbox.allow_host_bash: true only in a fully trusted local environment."
)

LOCAL_BASH_SUBAGENT_DISABLED_MESSAGE = (
    "Bash subagent is disabled for this host-filesystem-backed sandbox because bash execution "
    "is not a secure isolation boundary. Switch to AioSandboxProvider for isolated bash "
    "access, or set sandbox.allow_host_bash: true only in a fully trusted local environment."
)

HOST_TOOLS_DISABLED_MESSAGE = (
    "Host tools are disabled because they execute in the API host process or reuse host "
    "credentials/login state. Set sandbox.allow_host_tools: true only in a fully trusted "
    "local environment."
)


def uses_host_filesystem_sandbox_provider(config=None) -> bool:
    """Return True for Local and WSL providers that share the host filesystem."""
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    sandbox_use = getattr(sandbox_cfg, "use", "")
    return is_host_fs_sandbox_provider_path(sandbox_use)


def uses_local_sandbox_provider(config=None) -> bool:
    """Back-compatible alias for the broader host-filesystem safety check."""
    return uses_host_filesystem_sandbox_provider(config)


def is_host_bash_allowed(config=None) -> bool:
    """Return whether host bash execution is explicitly allowed."""
    if config is None:
        config = get_app_config()

    sandbox_cfg = getattr(config, "sandbox", None)
    if sandbox_cfg is None:
        return False
    if not uses_host_filesystem_sandbox_provider(config):
        return True
    return bool(getattr(sandbox_cfg, "allow_host_bash", False))


def is_host_tool_allowed(config=None) -> bool:
    """Return whether host-process tools are explicitly enabled."""
    if config is None:
        config = get_app_config()
    sandbox_cfg = getattr(config, "sandbox", None)
    return bool(getattr(sandbox_cfg, "allow_host_tools", False))
