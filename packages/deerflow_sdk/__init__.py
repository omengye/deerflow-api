"""DeerFlow SDK — public API.

This is the v0.1 contract. Everything imported here is a public symbol
covered by the SemVer policy described in CHANGELOG.md.

Anything NOT listed in ``__all__`` is private and may change at any time.

Design rules (do NOT violate):
  1. Zero langchain/langgraph imports in any user-facing type.
  2. All state lives on a Harness instance. No module-level mutable globals.
  3. Engine (LangGraph) is an internal detail in ``deerflow_sdk._engine``.
"""

from importlib.metadata import PackageNotFoundError, version

from deerflow_sdk._config import HarnessConfig, ModelConfig
from deerflow_sdk._errors import (
    DeerFlowError,
    ModelError,
    PermissionDenied,
    SandboxError,
    ToolError,
)
from deerflow_sdk._events import (
    RunComplete,
    StreamEvent,
    SubagentEnd,
    SubagentStart,
    TextDelta,
    ToolCall,
    ToolResult,
)
from deerflow_sdk._harness import Harness
from deerflow_sdk._hooks import Hook, HookContext
from deerflow_sdk._permissions import (
    Permission,
    PermissionContext,
    PermissionDecision,
)
from deerflow_sdk._sandbox.base import Sandbox
from deerflow_sdk._sandbox.local import LocalSandbox
from deerflow_sdk._subagents import SubagentSpec, subagent
from deerflow_sdk._tools import Tool, ToolContext, tool

__all__ = [
    # Core
    "Harness",
    "HarnessConfig",
    "ModelConfig",
    # Tools
    "tool",
    "Tool",
    "ToolContext",
    # Subagents
    "subagent",
    "SubagentSpec",
    # Hooks
    "Hook",
    "HookContext",
    # Permissions
    "Permission",
    "PermissionDecision",
    "PermissionContext",
    # Sandbox
    "Sandbox",
    "LocalSandbox",
    # Events
    "StreamEvent",
    "TextDelta",
    "ToolCall",
    "ToolResult",
    "SubagentStart",
    "SubagentEnd",
    "RunComplete",
    # Errors
    "DeerFlowError",
    "ToolError",
    "PermissionDenied",
    "SandboxError",
    "ModelError",
]

try:
    __version__ = version("deerflow-api")
except PackageNotFoundError:
    __version__ = "0+unknown"
