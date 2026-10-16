"""The requester's side of the download approval policy: their requests, and
cancelling one that waits (``services/download_requests.py``)."""
from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query

from core import download_policy as policy
from routes._pagination import paginated_response
from routes.deps import CurrentUserDep, SessionDep
from services import download_requests

RequestStatus = Literal[policy.STATUSES]  # type: ignore[valid-type]

router = APIRouter()


@router.get("/download-requests")
def list_my_requests(
    session: SessionDep,
    user: CurrentUserDep,
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    status: Annotated[list[RequestStatus] | None, Query()] = None,
):
    """The caller's own download requests, newest first (``status``, repeatable,
    filters). Only the policy's own requests are recorded (a user's, with
    authentication on): an admin has none."""
    items, total = download_requests.list_for_user(session, user, statuses=status or [], page=page, limit=limit)
    return paginated_response(items, total=total, page=page, limit=limit)


@router.post("/download-requests/{request_id}/cancel")
def cancel_my_request(request_id: str, session: SessionDep, user: CurrentUserDep):
    """Cancels the caller's own request while it waits. Cancelling one already
    cancelled answers the same (``changed: false``); one an admin decided
    meanwhile is 409; somebody else's, or none, is 404."""
    result = download_requests.cancel(session, request_id, user)
    if result.outcome == download_requests.NOT_FOUND:
        raise HTTPException(status_code=404, detail="Request not found")
    if result.outcome == download_requests.CONFLICT:
        raise HTTPException(status_code=409, detail={
            "error_code": "request_already_decided",
            "error_message": f"The request was already {result.request.status}.",
        })
    return {"request": download_requests.public_request(result.request), "changed": result.outcome == download_requests.DECIDED}
