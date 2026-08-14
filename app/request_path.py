"""Canonical request-path projection for routing security decisions."""

from starlette._utils import get_route_path
from starlette.requests import Request


def get_request_route_path(request: Request) -> str:
    """Return the root-path-adjusted value Starlette's router matches."""
    return get_route_path(request.scope)
