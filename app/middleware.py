"""HTTP middleware for request identity and API-key authentication."""

from __future__ import annotations

import secrets
import uuid
from contextvars import ContextVar
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.request_path import get_request_route_path

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def get_request_id() -> str | None:
    """Return the current request id, when running inside an HTTP request."""
    return _request_id_var.get()


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a stable request id to request state, response headers and logs."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        token = _request_id_var.set(request_id)
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            _request_id_var.reset(token)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """Protect externally useful routes with a Bearer token when auth is enabled."""

    _PUBLIC_PATHS = {"/health"}
    _PROTECTED_PATHS = {
        "/health/ready",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
    }
    _PROTECTED_PREFIXES = ("/api", "/v1")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not _should_authenticate(request):
            return await call_next(request)

        if not _is_authorized(request):
            return Response(
                content='{"detail":"Unauthorized"}',
                status_code=401,
                media_type="application/json",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)


def _should_authenticate(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    route_path = get_request_route_path(request)
    if route_path in ApiKeyAuthMiddleware._PUBLIC_PATHS:
        return False
    if not settings.auth_enabled:
        return False
    if route_path in ApiKeyAuthMiddleware._PROTECTED_PATHS:
        return True
    return route_path.startswith(ApiKeyAuthMiddleware._PROTECTED_PREFIXES)


def _is_authorized(request: Request) -> bool:
    if not settings.api_keys:
        return False

    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return False

    return any(secrets.compare_digest(token, key) for key in settings.api_keys)
