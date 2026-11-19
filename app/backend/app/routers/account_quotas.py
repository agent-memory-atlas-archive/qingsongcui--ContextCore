"""Local CSRF-protected quota reads. GET returns cached evidence only."""
from fastapi import APIRouter, Body, Query

from app.routers.manage import invoke
from app.services import account_quotas, management_service

router = APIRouter(prefix="/api/account-quotas", tags=["usage"])


@router.get("")
def list_accounts(host_id: str | None = Query(None, max_length=64), account_id: str | None = Query(None, max_length=128)):
    return invoke(account_quotas.list_quotas, management_service.service(), host_id, account_id)


@router.post("/{source_id}/refresh")
def refresh(source_id: str, body: dict = Body(...)):
    return invoke(account_quotas.refresh_quota, management_service.service(), source_id,
                  body.get("revision"), body.get("confirmed"))
