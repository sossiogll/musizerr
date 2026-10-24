"""
Administration of the root folders (ADR-0008): the places the administrator allows the product to use. Admin only.

A root is a label and an absolute path as the containers see it; the gateway, the only process that sees every mount, judges
it (``state`` ``accepted``/``refused`` and the ``reason``). A root that is refused is saved and not used. A root in use (a
folder setting points at it) cannot be removed (409 ``root_in_use``, with ``used_by``) and moving it asks for
``?confirm=true`` (409 ``confirmation_required``, with ``used_by``). Every change is pushed to the gateway at once, and the
reply carries the verdict. A refusal answers ``{"detail": {"error_code", "error_message", ...}}``.
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from routes.deps import AdminUserDep, RegistryDep, SessionDep
from services import roots as roots_service
from services.gateway_client import GatewayClient, GatewayRefused, GatewayUnreachable
from services.gateway_sync import GatewaySync
from services.roots import RootError

router = APIRouter()


class AddRoot(BaseModel):
    path: str
    label: Optional[str] = None


class EditRoot(BaseModel):
    """Only the fields present are changed."""

    label: Optional[str] = None
    path: Optional[str] = None


@contextmanager
def _refusals():
    try:
        yield
    except RootError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"error_code": exc.code, "error_message": exc.message, **exc.details},
        ) from None


def _gateway() -> GatewayClient:
    return GatewayClient()


def _push(session) -> None:
    """Tell the gateway the roots and the working folder now and record its verdict (best effort: the watcher repeats it)."""
    GatewaySync(_gateway()).push(session)


def _gateway_error(exc: GatewayUnreachable) -> HTTPException:
    refused = isinstance(exc, GatewayRefused)
    return HTTPException(
        status_code=422 if refused else 503,
        detail={
            "error_code": "gateway_refused" if refused else "gateway_unreachable",
            "error_message": exc.error if isinstance(exc, GatewayRefused) else str(exc),
        },
    )


@router.get("/roots")
def list_roots(session: SessionDep, registry: RegistryDep, _admin: AdminUserDep):  # noqa: ARG001
    """The root folders (with the folder settings that use each) and every folder setting with what it resolves to."""
    return {"roots": roots_service.list_roots(session, registry), "folders": roots_service.folders_view(session, registry)}


@router.get("/roots/browse")
def browse(_admin: AdminUserDep, path: Optional[str] = Query(None)):  # noqa: ARG001
    """The folder picker: the subdirectories of ``path``, which must lie inside a mounted volume (the gateway's rule); without
    a path, the mounted volumes. Never files."""
    try:
        return _gateway().browse(path or None)
    except GatewayUnreachable as exc:
        raise _gateway_error(exc) from None


@router.post("/roots", status_code=201)
def add_root(body: AddRoot, session: SessionDep, registry: RegistryDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        root = roots_service.create_root(session, body.path, body.label)
    _push(session)
    session.expire_all()
    return roots_service.view(roots_service.get_root(session, root.id))


@router.patch("/roots/{root_id}")
def edit_root(
    root_id: str, body: EditRoot, session: SessionDep, registry: RegistryDep, _admin: AdminUserDep,  # noqa: ARG001
    confirm: bool = Query(False),
):
    """Rename a root and/or move it. Moving a root in use needs ``confirm=true``."""
    with _refusals():
        root, moved = roots_service.update_root(session, registry, root_id, path=body.path, label=body.label, confirm=confirm)
    if moved:
        _push(session)
        session.expire_all()
    return roots_service.view(
        roots_service.get_root(session, root.id), roots_service.usages(session, registry, root.id),
    )


@router.delete("/roots/{root_id}")
def delete_root(root_id: str, session: SessionDep, registry: RegistryDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        roots_service.delete_root(session, registry, root_id)
    _push(session)
    return {"ok": True}


@router.get("/roots/{root_id}/check")
def check_root(root_id: str, session: SessionDep, _admin: AdminUserDep):  # noqa: ARG001
    """The explicit Check: what the gateway sees at the path (exists, directory, writable by a real write test, mounted
    volume, device, free space) and whether the application sees it too."""
    with _refusals():
        return roots_service.check(session, _gateway(), root_id)
