"""Sandbox provider path helpers."""

LOCAL_SANDBOX_PROVIDER_PATH = "deerflow.sandbox.local:LocalSandboxProvider"
WSL_SANDBOX_PROVIDER_PATH = "deerflow.sandbox.local:LocalWslProvider"
AIO_SANDBOX_PROVIDER_PATH = "deerflow.sandbox.aio:AioSandboxProvider"

_SANDBOX_PROVIDER_ALIASES = {
    "local": LOCAL_SANDBOX_PROVIDER_PATH,
    "local-sandbox": LOCAL_SANDBOX_PROVIDER_PATH,
    "local_sandbox": LOCAL_SANDBOX_PROVIDER_PATH,
    "wsl": WSL_SANDBOX_PROVIDER_PATH,
    "local-wsl": WSL_SANDBOX_PROVIDER_PATH,
    "local_wsl": WSL_SANDBOX_PROVIDER_PATH,
    "aio": AIO_SANDBOX_PROVIDER_PATH,
    "docker": AIO_SANDBOX_PROVIDER_PATH,
    "docker-sandbox": AIO_SANDBOX_PROVIDER_PATH,
    "docker_sandbox": AIO_SANDBOX_PROVIDER_PATH,
}

_LOCAL_SANDBOX_PROVIDER_PATHS = {
    LOCAL_SANDBOX_PROVIDER_PATH,
    "deerflow.sandbox.local.local_sandbox_provider:LocalSandboxProvider",
}

_WSL_SANDBOX_PROVIDER_PATHS = {
    WSL_SANDBOX_PROVIDER_PATH,
    "deerflow.sandbox.local.local_wsl_provider:LocalWslProvider",
}


def normalize_sandbox_provider_path(provider_path: str | None) -> str:
    """Return the canonical provider path for supported shorthand aliases."""
    value = (provider_path or "").strip()
    return _SANDBOX_PROVIDER_ALIASES.get(value.lower(), value)


def is_local_sandbox_provider_path(provider_path: str | None) -> bool:
    """Return whether a provider path or supported alias points at LocalSandboxProvider."""
    value = normalize_sandbox_provider_path(provider_path)
    if value in _LOCAL_SANDBOX_PROVIDER_PATHS:
        return True

    module_path, separator, class_name = value.partition(":")
    if separator != ":" or class_name != "LocalSandboxProvider":
        return False
    return module_path == "deerflow.sandbox.local" or module_path.startswith("deerflow.sandbox.local.")


def is_wsl_sandbox_provider_path(provider_path: str | None) -> bool:
    """Return whether a provider path or supported alias points at LocalWslProvider."""
    value = normalize_sandbox_provider_path(provider_path)
    if value in _WSL_SANDBOX_PROVIDER_PATHS:
        return True

    module_path, separator, class_name = value.partition(":")
    if separator != ":" or class_name != "LocalWslProvider":
        return False
    return module_path == "deerflow.sandbox.local" or module_path.startswith("deerflow.sandbox.local.")


def is_host_fs_sandbox_provider_path(provider_path: str | None) -> bool:
    """Return whether a provider path uses the host filesystem with virtual path translation.

    Currently matches LocalSandboxProvider and LocalWslProvider. AioSandbox-style
    providers (Docker, k8s) do not need virtual path translation since they mount
    /mnt/user-data natively inside the container.
    """
    return is_local_sandbox_provider_path(provider_path) or is_wsl_sandbox_provider_path(provider_path)
