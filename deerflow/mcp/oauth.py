"""OAuth token support for MCP HTTP/SSE servers."""

from __future__ import annotations

import asyncio
import logging
import threading
import weakref
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from deerflow.config.extensions_config import ExtensionsConfig, McpOAuthConfig

logger = logging.getLogger(__name__)


@dataclass
class _OAuthToken:
    """Cached OAuth token."""

    access_token: str
    token_type: str
    expires_at: datetime


class OAuthTokenManager:
    """Acquire/cache/refresh OAuth tokens for MCP servers.

    Callers reach ``get_authorization_header`` from many different event
    loops, not just one long-lived loop: sync tool paths (see
    ``deerflow.tools.sync.make_sync_tool_wrapper`` and
    ``deerflow.mcp.cache.get_cached_mcp_tools``) run the coroutine via
    ``asyncio.run(...)`` on a worker-thread pool, and ``asyncio.run``
    creates and tears down a brand-new event loop for *every single call*.
    An ``asyncio.Lock`` binds to whichever loop first awaits it, so a lock
    created once in ``__init__`` (the previous implementation) would raise
    "Future attached to a different loop" -- or simply hang -- the moment a
    second loop tried to acquire it.

    Because the refresh critical section must ``await`` an HTTP call
    (``_fetch_token``), we cannot dodge the problem by only ever holding a
    plain ``threading.Lock`` (that would mean holding a thread lock across
    an ``await``, which is its own deadlock hazard under Windows'
    ProactorEventLoop / thread-pool combo). Instead we keep one
    ``asyncio.Lock`` *per event loop* for each server, created lazily and
    stored in a ``WeakKeyDictionary`` keyed by the loop object so locks for
    loops that have since been closed (e.g. every ``asyncio.run()`` call)
    are garbage-collected instead of leaking. Bookkeeping around that
    dictionary -- and around the token cache -- is protected by a plain
    ``threading.Lock`` that is *only* ever held across synchronous dict
    operations, never across an ``await``.

    The tradeoff: concurrent refreshes from *different* event loops no
    longer serialize against each other and may each fetch a fresh token
    independently. That's a benign, last-write-wins race on the token
    cache -- at worst one extra token request -- which is strictly better
    than the deadlock/crash risk of a single cross-loop lock. Concurrent
    callers *within the same loop* still coalesce onto a single in-flight
    refresh, preserving the original single-fetch behavior.
    """

    def __init__(self, oauth_by_server: dict[str, McpOAuthConfig]):
        self._oauth_by_server = oauth_by_server
        self._tokens: dict[str, _OAuthToken] = {}
        # Guards `_tokens` reads/writes and `_loop_locks` bookkeeping below.
        # Never held across an `await`.
        self._state_lock = threading.Lock()
        self._loop_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = weakref.WeakKeyDictionary()

    @classmethod
    def from_extensions_config(cls, extensions_config: ExtensionsConfig) -> OAuthTokenManager:
        oauth_by_server: dict[str, McpOAuthConfig] = {}
        for server_name, server_config in extensions_config.get_enabled_mcp_servers().items():
            if server_config.oauth and server_config.oauth.enabled:
                oauth_by_server[server_name] = server_config.oauth
        return cls(oauth_by_server)

    def has_oauth_servers(self) -> bool:
        return bool(self._oauth_by_server)

    def oauth_server_names(self) -> list[str]:
        return list(self._oauth_by_server.keys())

    async def get_authorization_header(self, server_name: str) -> str | None:
        oauth = self._oauth_by_server.get(server_name)
        if not oauth:
            return None

        token = self._get_cached_token(server_name)
        if token and not self._is_expiring(token, oauth):
            return f"{token.token_type} {token.access_token}"

        lock = self._get_loop_lock(server_name)
        async with lock:
            token = self._get_cached_token(server_name)
            if token and not self._is_expiring(token, oauth):
                return f"{token.token_type} {token.access_token}"

            fresh = await self._fetch_token(oauth)
            self._store_token(server_name, fresh)
            logger.info(f"Refreshed OAuth access token for MCP server: {server_name}")
            return f"{fresh.token_type} {fresh.access_token}"

    def _get_cached_token(self, server_name: str) -> _OAuthToken | None:
        with self._state_lock:
            return self._tokens.get(server_name)

    def _store_token(self, server_name: str, token: _OAuthToken) -> None:
        with self._state_lock:
            self._tokens[server_name] = token

    def _get_loop_lock(self, server_name: str) -> asyncio.Lock:
        """Return the per-current-event-loop asyncio.Lock for `server_name`.

        See the class docstring for why the lock must be scoped to the
        running event loop rather than shared process-wide.
        """
        loop = asyncio.get_running_loop()
        with self._state_lock:
            per_loop = self._loop_locks.get(loop)
            if per_loop is None:
                per_loop = {}
                self._loop_locks[loop] = per_loop
            lock = per_loop.get(server_name)
            if lock is None:
                lock = asyncio.Lock()
                per_loop[server_name] = lock
            return lock

    @staticmethod
    def _is_expiring(token: _OAuthToken, oauth: McpOAuthConfig) -> bool:
        now = datetime.now(UTC)
        return token.expires_at <= now + timedelta(seconds=max(oauth.refresh_skew_seconds, 0))

    async def _fetch_token(self, oauth: McpOAuthConfig) -> _OAuthToken:
        import httpx  # pyright: ignore[reportMissingImports]

        data: dict[str, str] = {
            "grant_type": oauth.grant_type,
            **oauth.extra_token_params,
        }

        if oauth.scope:
            data["scope"] = oauth.scope
        if oauth.audience:
            data["audience"] = oauth.audience

        if oauth.grant_type == "client_credentials":
            if not oauth.client_id or not oauth.client_secret:
                raise ValueError("OAuth client_credentials requires client_id and client_secret")
            data["client_id"] = oauth.client_id
            data["client_secret"] = oauth.client_secret
        elif oauth.grant_type == "refresh_token":
            if not oauth.refresh_token:
                raise ValueError("OAuth refresh_token grant requires refresh_token")
            data["refresh_token"] = oauth.refresh_token
            if oauth.client_id:
                data["client_id"] = oauth.client_id
            if oauth.client_secret:
                data["client_secret"] = oauth.client_secret
        else:
            raise ValueError(f"Unsupported OAuth grant type: {oauth.grant_type}")

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(oauth.token_url, data=data)
            response.raise_for_status()
            payload = response.json()

        access_token = payload.get(oauth.token_field)
        if not access_token:
            raise ValueError(f"OAuth token response missing '{oauth.token_field}'")

        token_type = str(payload.get(oauth.token_type_field, oauth.default_token_type) or oauth.default_token_type)

        expires_in_raw = payload.get(oauth.expires_in_field, 3600)
        try:
            expires_in = int(expires_in_raw)
        except (TypeError, ValueError):
            expires_in = 3600

        expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in, 1))
        return _OAuthToken(access_token=access_token, token_type=token_type, expires_at=expires_at)


def build_oauth_tool_interceptor(extensions_config: ExtensionsConfig) -> Any | None:
    """Build a tool interceptor that injects OAuth Authorization headers."""
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return None

    async def oauth_interceptor(request: Any, handler: Any) -> Any:
        header = await token_manager.get_authorization_header(request.server_name)
        if not header:
            return await handler(request)

        updated_headers = dict(request.headers or {})
        updated_headers["Authorization"] = header
        return await handler(request.override(headers=updated_headers))

    return oauth_interceptor


async def get_initial_oauth_headers(extensions_config: ExtensionsConfig) -> dict[str, str]:
    """Get initial OAuth Authorization headers for MCP server connections."""
    token_manager = OAuthTokenManager.from_extensions_config(extensions_config)
    if not token_manager.has_oauth_servers():
        return {}

    headers: dict[str, str] = {}
    for server_name in token_manager.oauth_server_names():
        headers[server_name] = await token_manager.get_authorization_header(server_name) or ""

    return {name: value for name, value in headers.items() if value}
