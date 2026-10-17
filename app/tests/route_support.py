"""The routes of the app as the tests read them.

FastAPI keeps an included router as one lazy node of ``app.routes``; ``iter_route_contexts`` walks it
and gives each endpoint with the path, the methods and the dependencies it has once mounted.
"""
from __future__ import annotations

from collections.abc import Iterator

from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

import main


def all_routes() -> Iterator[RouteContext]:
    return iter_route_contexts(main.app.routes)


def api_routes() -> Iterator[RouteContext]:
    """The endpoints (``APIRoute``) of the app, as mounted."""
    return (route for route in all_routes() if isinstance(route.original_route, APIRoute))
