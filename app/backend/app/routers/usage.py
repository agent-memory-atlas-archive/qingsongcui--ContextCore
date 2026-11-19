from fastapi import APIRouter, Body, Depends, Query
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from app.services import usage_reports as reports
from app.services import usage_service as service

router = APIRouter(prefix="/api/usage", tags=["usage"])


def filters(start=None, end=None, host_id=None, provider_id=None, model=None, project_id=None, account_id=None, status=None, logical_request_id=None):
    return {k: v for k, v in locals().items() if v is not None}


@router.get("/summary")
def summary(start: str | None = None, end: str | None = None, tz: str = "Asia/Shanghai", host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None):
    return service.summary(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id), tz)


@router.get("/requests")
def requests(start: str | None = None, end: str | None = None, host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return service.requests(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id), limit, offset)


@router.get("/coverage")
def coverage(start: str | None = None, end: str | None = None, host_id: str | None = None, provider_id: str | None = None, model: str | None = None, project_id: str | None = None, account_id: str | None = None, status: str | None = None, logical_request_id: str | None = None):
    return service.coverage(filters(start, end, host_id, provider_id, model, project_id, account_id, status, logical_request_id))


def report_filters(start: str | None = None, end: str | None = None, host_id: str | None = None,
                   provider_id: str | None = None, model: str | None = None, project_id: str | None = None,
                   account_id: str | None = None, status: str | None = None, origin: str | None = None,
                   scope: str = "user", request_status: str | None = None, missing: list[str] | None = Query(None),
                   tz: str = "Asia/Shanghai"):
    reports.timezone_for(tz)
    return {k: v for k, v in locals().items() if k != "tz" and v is not None}


@router.get("/overview")
def overview(criteria: dict = Depends(report_filters), tz: str = "Asia/Shanghai"):
    return service.summary(criteria, tz, calendar=True)


@router.get("/breakdown")
def breakdown(dimension: str, criteria: dict = Depends(report_filters)):
    return reports.breakdown(criteria, dimension)


@router.get("/filter-options")
def filter_options(criteria: dict = Depends(report_filters)):
    return reports.filter_options(criteria)


@router.get("/logical-requests")
def logical_requests(criteria: dict = Depends(report_filters), limit: int = Query(50, ge=1, le=500),
                     offset: int = Query(0, ge=0)):
    return reports.logical_requests(criteria, limit, offset)


@router.get("/attempts")
def attempts(logical_request_id: str = Query(min_length=1, max_length=256),
             limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
    return reports.attempts(logical_request_id, limit, offset)


@router.get("/export")
def export(criteria: dict = Depends(report_filters), tz: str = "Asia/Shanghai"):
    output = reports.export_csv(criteria, tz)
    return StreamingResponse(iter(lambda: output.read(65536), b""), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="senrelay-usage.csv"'},
                             background=BackgroundTask(output.close))


@router.post("/ingest")
def ingest(data: dict = Body(...)):
    from fastapi import HTTPException
    if set(data) != {"observations"}:
        raise HTTPException(422, "Expected an explicit observations batch")
    return service.ingest(data["observations"])


@router.get("/prices")
def prices():
    return {"items": service.list_prices()}


@router.post("/prices")
def price(data: dict = Body(...)):
    return service.add_price(data)
