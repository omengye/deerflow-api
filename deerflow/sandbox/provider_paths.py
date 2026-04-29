"""Sandbox provider path helpers."""

LOCAL_SANDBOX_PROVIDER_PATH = "deerflow.sandbox.local:LocalSandboxProvider"

_SANDBOX_PROVIDER_ALIASES = {
    "local": LOCAL_SANDBOX_PROVIDER_PATH,
    "local-sandbox": LOCAL_SANDBOX_PROVIDER_PATH,
    "local_sandbox": LOCAL_SANDBOX_PROVIDER_PATH,
}

_LOCAL_SANDBOX_PROVIDER_PATHS = {
    LOCAL_SANDBOX_PROVIDER_PATH,
    "deerflow.sandbox.local.local_sandbox_provider:LocalSandboxProvider",
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
