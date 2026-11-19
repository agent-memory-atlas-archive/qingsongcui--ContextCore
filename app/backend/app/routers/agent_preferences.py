from fastapi import APIRouter, Body

from app.routers.manage import invoke
from app.services import agent_preferences, management_service

router = APIRouter(prefix="/api/manage/agent-preferences", tags=["management"])


@router.get("")
def preferences():
    return invoke(agent_preferences.read, management_service.service())


@router.post("")
def update_preferences(body: dict = Body(...)):
    return invoke(agent_preferences.update, management_service.service(), body.get("host_id"),
                  body.get("action"), body.get("provider_id"), body.get("neighbor_id"))
