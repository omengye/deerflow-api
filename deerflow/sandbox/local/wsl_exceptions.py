"""Exceptions raised by :class:`~deerflow.sandbox.local.local_wsl_provider.LocalWslProvider`."""


class WslSandboxError(RuntimeError):
    """Base class for WSL sandbox configuration and runtime errors."""


class WslUnavailableError(WslSandboxError):
    """Raised when wsl.exe is not installed or not reachable on this host."""


class WslDistroNotFoundError(WslSandboxError):
    """Raised when the configured WSL distro is not registered."""
