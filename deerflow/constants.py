"""Shared dependency-free runtime constants."""

# Bound MCP server bring-up (spawn/connect + initialize + tools/list) so one
# broken external server cannot block agent construction indefinitely.
DEFAULT_MCP_SESSION_INIT_TIMEOUT = 60.0
