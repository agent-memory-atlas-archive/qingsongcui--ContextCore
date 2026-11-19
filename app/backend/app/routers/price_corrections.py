from fastapi import APIRouter, Body, HTTPException

from app.services.price_corrections import price_correction

router = APIRouter(prefix="/api/usage/prices", tags=["usage"])


@router.post("/{price_id}/preview")
def preview(price_id: str):
    return price_correction(price_id)


@router.post("/{price_id}/apply")
def apply(price_id: str, body: dict = Body(...)):
    fingerprint = body.get("fingerprint")
    if body.get("confirmed") is not True or not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise HTTPException(422, "Confirm the reviewed price correction")
    return price_correction(price_id, fingerprint)
