"""
Administration of the sources of providers: catalogs (version 2), what is installed from them,
and the gateway's state. Admin only. A refusal answers
``{"detail": {"error_code", "error_message"}}`` with the status ``services.sources.SourceError``
carries; no response ever holds a source's credential (``has_credential`` says whether there is one).
"""
from __future__ import annotations

from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from routes.deps import AdminUserDep, RegistryDep, SourceManagerDep
from services.sources import UNSET, SourceError, installed_view, source_view

router = APIRouter()


class AddSourceRequest(BaseModel):
    url: str
    credential: str | None = None
    allow_insecure_registry: bool = False


class EditSourceRequest(BaseModel):
    """Only the fields present are changed; ``credential: null`` removes the token."""

    enabled: bool | None = None
    credential: str | None = None
    allow_insecure_registry: bool | None = None


class InstallRequest(BaseModel):
    source_id: str
    provider_name: str
    digest: str  # the digest the admin was shown and approved


class UpdateRequest(BaseModel):
    digest: str  # the digest the admin was shown and approved


@contextmanager
def _refusals():
    try:
        yield
    except SourceError as exc:
        raise HTTPException(
            status_code=exc.status, detail={"error_code": exc.code, "error_message": str(exc)},
        ) from None


# ------------------------------------------------------------------
# Sources
# ------------------------------------------------------------------


@router.get("/sources")
def list_sources(sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    return [source_view(source) for source in sm.list_sources()]


@router.post("/sources", status_code=201)
def add_source(body: AddSourceRequest, sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        return source_view(sm.add_source(body.url, body.credential, body.allow_insecure_registry))


@router.patch("/sources/{source_id}")
def edit_source(source_id: str, body: EditSourceRequest, sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    given = body.model_fields_set
    with _refusals():
        return source_view(sm.edit_source(
            source_id,
            enabled=body.enabled if "enabled" in given and body.enabled is not None else UNSET,
            credential=body.credential if "credential" in given else UNSET,
            allow_insecure_registry=(
                body.allow_insecure_registry if "allow_insecure_registry" in given and body.allow_insecure_registry is not None else UNSET
            ),
        ))


@router.delete("/sources/{source_id}")
def remove_source(source_id: str, sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        return {"ok": True, "uninstalled": sm.remove_source(source_id)}


@router.get("/sources/{source_id}/catalog")
def browse_catalog(source_id: str, sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        return sm.browse(source_id)


# ------------------------------------------------------------------
# Installed providers
# ------------------------------------------------------------------


@router.get("/sources/providers/installed")
def list_installed(sm: SourceManagerDep, _admin: AdminUserDep, updates: bool = True):  # noqa: ARG001
    return sm.list_installed(check_updates=updates)


@router.get("/sources/providers/gateway")
def gateway_state(sm: SourceManagerDep, registry: RegistryDep, _admin: AdminUserDep):  # noqa: ARG001
    return sm.gateway_state(registry.rejected())


@router.post("/sources/providers/install", status_code=201)
def install_provider(body: InstallRequest, sm: SourceManagerDep, admin: AdminUserDep):
    with _refusals():
        row = sm.install(body.source_id, body.provider_name, body.digest, approved_by=admin.id)
        return installed_view(row, sm.get_source(row.source_id))


@router.post("/sources/providers/{name}/update")
def update_provider(name: str, body: UpdateRequest, sm: SourceManagerDep, admin: AdminUserDep):
    with _refusals():
        row = sm.update(name, body.digest, approved_by=admin.id)
        return installed_view(row, sm.get_source(row.source_id))


@router.delete("/sources/providers/{name}")
def uninstall_provider(name: str, sm: SourceManagerDep, _admin: AdminUserDep):  # noqa: ARG001
    with _refusals():
        sm.uninstall(name)
        return {"ok": True}
