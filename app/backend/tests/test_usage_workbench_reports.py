import csv
import io

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_schema, db, encode
from app.routers.usage import router
from app.services import usage_service as ledger
from app.services import management_service


@pytest.fixture
def api(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    monkeypatch.setenv("CONTEXTCORE_DB", str(path))
    create_schema(path)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def observation(key, **changes):
    return {"origin": "proxy", "observation_id": key, "logical_request_id": "run", "attempt_id": key,
            "started_at": "2026-11-01T05:30:00Z", "host_id": "codex", "provider_id": "first",
            "model": "m", "status": "succeeded", "raw_usage": {"input_tokens": 20, "output_tokens": 10}, **changes}


def data(api, endpoint, **params):
    response = api.get("/api/usage/" + endpoint, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_fallback_currency_unknowns_and_scope_agree_across_pages(api):
    for pid, currency in [("first", "USD"), ("reserve", "CNY")]:
        ledger.add_price({"provider_id": pid, "model": "m", "currency": currency, "input_per_million": 2,
                          "output_per_million": 3, "effective_from": "2026-01-01T00:00:00Z"})
    ledger.ingest([observation("failed", status="failed", error_code="upstream_http_429"),
                   observation("success", provider_id="reserve", account_id="saved", started_at="2026-11-01T06:30:00Z"),
                   observation("unknown", logical_request_id="other", host_id=None, provider_id=None, raw_usage={}),
                   observation("check", origin="provider_check", logical_request_id="sys"),
                   observation("compute", origin="contextcore_compute", logical_request_id="sys2")])
    result = data(api, "overview")
    assert (result["logical_requests"], result["attempts"], result["known_tokens"]) == (2, 3, 60)
    assert result["known_token_attempts"] == 2 and result["unknown_cost_count"] == 1
    assert result["cost_by_currency"] == {"USD": "0.00007", "CNY": "0.00007"}
    assert data(api, "overview", scope="system")["attempts"] == 2
    assert data(api, "overview", scope="all")["attempts"] == 5
    buckets = data(api, "breakdown", dimension="provider_id")["items"]
    assert {b["value"] for b in buckets} == {"first", "reserve", None}
    assert sum(b["logical_requests"] for b in buckets) == 3  # one request used two providers
    unknown = data(api, "overview", missing="provider_id")
    assert unknown["unknown_usage_count"] == 1 and unknown["known_token_attempts"] == 0
    assert any(o["value"] is None for o in data(api, "filter-options")["provider_id"])
    for n in (0, 1):
        page = data(api, "logical-requests", limit=1, offset=n)
        assert len(page["items"]) == 1 and page["total"] == 2
        if page["items"][0]["id"] == "run":
            assert page["items"][0]["attempts"] == 2 and page["items"][0]["status"] == "succeeded"
    failed_attempt = data(api, "logical-requests", provider_id="first")["items"][0]
    assert failed_attempt["status"] == "failed" and failed_attempt["request_status"] == "succeeded"
    assert data(api, "logical-requests", request_status="failed")["total"] == 0
    assert data(api, "overview", request_status="succeeded")["attempts"] == 3
    all_attempts = data(api, "attempts", logical_request_id="run", limit=1)
    assert all_attempts["total"] == 2 and all_attempts["items"][0]["error_code"] == "upstream_http_429"
    assert data(api, "attempts", logical_request_id="run", offset=1)["items"][0]["account_id"] == "saved"
    assert data(api, "overview", origin="proxy", host_id="codex")["attempts"] == 2


def test_dates_dst_midnight_half_open_and_unobserved_days(api):
    ledger.ingest([observation("one", started_at="2026-11-01T07:10:00Z"),  # LA midnight on DST end
                   observation("retry", started_at="2026-11-02T08:10:00Z"),  # next midnight at new offset
                   observation("boundary", logical_request_id="excluded", started_at="2026-11-04T08:00:00Z")])
    query = dict(tz="America/Los_Angeles", start="2026-10-31T00:00:00-07:00", end="2026-11-04T00:00:00-08:00")
    rows = data(api, "overview", **query)["heatmap"]
    assert [r["date"] for r in rows] == ["2026-10-31", "2026-11-01", "2026-11-02", "2026-11-03"]
    assert [r["logical_requests"] for r in rows] == [None, 1, 0, None]
    assert [r["attempts"] for r in rows] == [None, 1, 1, None]
    assert [r["observed"] for r in rows] == [False, True, True, False]
    assert data(api, "logical-requests", **query)["total"] == 1
    assert data(api, "breakdown", dimension="model", **query)["items"][0]["attempts"] == 2
    assert api.get("/api/usage/overview", params={"tz": "../bad"}).status_code == 422
    for endpoint in ("overview", "logical-requests", "filter-options", "export", "breakdown?dimension=model"):
        assert api.get("/api/usage/" + endpoint, params={"missing": "typo", "dimension": "model"}).status_code == 422


def test_names_coverage_and_excel_safe_scoped_csv(api):
    with db() as conn:
        management_service.ensure_schema(conn)
        conn.execute("INSERT INTO management_entities VALUES(?, 'provider', ?, 1, '', '')",
                     ("first", encode({"name": "Primary"})))
    ledger.ingest([observation("one", model="=unsafe", account_id="@value", account_name="Archived account"), observation("empty", raw_usage={}),
                   observation("test", origin="provider_check", logical_request_id="check")])
    result = data(api, "filter-options")
    assert result["provider_id"] == [{"value": "first", "label": "Primary"}]
    assert {"value": "@value", "label": "Archived account"} in result["account_id"]
    assert data(api, "breakdown", dimension="provider_id")["items"][0]["label"] == "Primary"
    hosts = data(api, "overview")["coverage"]["hosts"]
    assert hosts[0]["host_id"] == "codex" and hosts[0]["metered_attempts"] == 1
    assert hosts[0]["observed_attempts"] == 2 and hosts[0]["origins"] == ["proxy"]
    response = api.get("/api/usage/export", params={"tz": "America/New_York", "account_id": "@value"})
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert len(rows) == 1 and rows[0]["model"] == "'=unsafe" and rows[0]["account_id"] == "'@value"
    assert rows[0]["started_at"] == "2026-11-01T01:30:00-04:00"
    assert "amount" not in rows[0] and rows[0]["cost.amount"] == ""


def test_clock_sources_do_not_mix_or_claim_measured_for_an_import_or_old_row(api):
    ledger.ingest([observation("gateway", upstream_request_id="up", first_content_at="2026-11-01T05:30:01Z")], local_gateway=True)
    ledger.ingest([observation("log", origin="native_log", upstream_request_id="up",
                               started_at="2026-11-01T06:30:00Z", ended_at="2026-11-01T06:30:03Z"),
                   observation("external", first_content_at="2026-11-01T05:30:02Z")])
    result = data(api, "overview")["performance_by_measurement"]
    assert result["stream"]["ttft_ms"]["samples"] == 1 and result["stream"]["ttft_ms"]["p50"] == 1000
    assert result["stream"]["end_to_end_ms"]["samples"] == 0
    assert result["reported"]["end_to_end_ms"]["p50"] == 3000
    assert result["reported"]["ttft_ms"]["p50"] == 2000
    with db() as conn:
        conn.execute("UPDATE usage_v2_attempts SET data=json_remove(data,'$.performance.measurement_by_metric')")
    assert data(api, "overview")["performance_by_measurement"]["stream"]["ttft_ms"]["samples"] == 0


def test_gateway_adopts_an_earlier_unnamed_native_group_and_replay_preserves_identity(api):
    native = observation("native", origin="native_log", logical_request_id=None, upstream_request_id="up")
    unnamed = ledger.ingest([native])["items"][0]["logical_request_id"]
    gateway = observation("received", status="failed", upstream_request_id="up")
    ledger.ingest([gateway], local_gateway=True)
    ledger.ingest([observation("fallback", started_at="2026-11-01T07:00:00Z", provider_id="reserve")], local_gateway=True)
    assert data(api, "attempts", logical_request_id=unnamed)["total"] == 0
    attempts = data(api, "attempts", logical_request_id="run")
    assert attempts["total"] == 2 and attempts["items"][0]["observations"] == 2
    assert not attempts["items"][0]["possible_duplicate"]
    assert data(api, "overview")["logical_requests"] == 1
    assert ledger.ingest([native])["duplicates"] == ledger.ingest([gateway], local_gateway=True)["duplicates"] == 1
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_v2_requests").fetchone()[0] == 1
    conflict = observation("changed", attempt_id="received", logical_request_id="unrelated", upstream_request_id="up")
    assert api.post("/api/usage/ingest", json={"observations": [conflict]}).status_code == 409


def test_model_dimension_tracks_upstream_observation_and_legacy_attempt_api_keeps_dispatch_model(api):
    ledger.ingest([observation("resolved", model="dispatch-alias", requested_model="public-alias", actual_model="version-3"),
                   observation("native", origin="native_log", model="native-model", logical_request_id="second")])
    assert {i["value"] for i in data(api, "filter-options")["model"]} == {"version-3", "native-model"}
    result = data(api, "breakdown", dimension="model", model="version-3")
    assert result["items"][0]["value"] == "version-3"
    assert data(api, "overview", model="version-3")["attempts"] == 1
    assert data(api, "logical-requests", model="version-3")["items"][0]["model"] == "version-3"
    assert data(api, "requests", model="dispatch-alias")["items"][0]["requested_model"] == "public-alias"


@pytest.mark.parametrize("legacy", [False, True])
def test_unidentifiable_cumulative_log_is_metered_without_fabricating_a_request(api, legacy):
    settings = dict(origin="native_log", logical_request_id=None, attempt_id=None, cumulative=True, source_id="native")
    ledger.ingest([observation("meter", **settings, sequence=0), observation("meter2", **settings, sequence=1,
                                                                             raw_usage={"input_tokens": 25, "output_tokens": 12})])
    if legacy:
        with db() as conn:
            conn.execute("UPDATE usage_v2_attempts SET data=json_remove(data,'$.unattributed_sample')")
    summary = data(api, "overview")
    assert summary["logical_requests"] == summary["attempts"] == 0
    assert summary["known_tokens"] == 37 and summary["unattributed_samples"] == 2
    assert summary["coverage"]["hosts"][0]["metered_attempts"] == 0
    assert summary["coverage"]["hosts"][0]["unattributed_samples"] == 2
    assert data(api, "filter-options")["status"] == [{"value": "unknown", "label": "unknown"}]
    assert data(api, "logical-requests", status="succeeded")["total"] == 0
    assert data(api, "logical-requests", request_status="succeeded")["total"] == 0
    first = data(api, "logical-requests", limit=1)
    assert first["total"] == 2 and first["items"][0]["unattributed_sample"] and first["items"][0]["request_status"] == "unknown"
    detail = data(api, "attempts", logical_request_id=first["items"][0]["id"])["items"][0]
    assert detail["unattributed_sample"] and detail["status"] == "unknown"
    assert data(api, "breakdown", dimension="host_id")["items"][0]["unattributed_samples"] == 2
    rows = list(csv.DictReader(io.StringIO(api.get("/api/usage/export").content.decode("utf-8-sig"))))
    assert rows[0]["record_kind"] == "usage_sample" and rows[0]["status"] == "unknown"


@pytest.mark.parametrize("actual_price_at_start", [False, True])
def test_gateway_resolution_uses_only_actual_model_tariffs_present_at_dispatch(api, actual_price_at_start):
    def price(model, rate):
        return ledger.add_price({"provider_id": "first", "model": model, "effective_from": "2026-01-01T00:00:00Z",
                                 "input_per_million": rate, "output_per_million": rate})
    price("alias", 1)
    expected = price("resolved", 10) if actual_price_at_start else None
    ledger.ingest([observation("start", model="alias", billing_model="alias", actual_model="alias",
                               attempt_id="one", status="running", raw_usage={})], local_gateway=True)
    price("resolved", 99)
    ledger.ingest([observation("finish", model="alias", actual_model="resolved", billing_model="resolved",
                               attempt_id="one")], local_gateway=True)
    row = data(api, "attempts", logical_request_id="run")["items"][0]
    assert row["cost"]["price_snapshot"] == expected
    assert row["cost"]["amount"] == ("0.0003" if expected else None)
    price("resolved", 77)
    ledger.ingest([observation("supplement", origin="native_log", attempt_id="one", model="alias",
                               actual_model="resolved", billing_model="resolved")])
    assert data(api, "attempts", logical_request_id="run")["items"][0]["cost"]["price_snapshot"] == expected
