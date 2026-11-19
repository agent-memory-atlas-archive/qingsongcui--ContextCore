from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.database import create_schema, db
from app.routers import price_corrections
from app.services import usage_service as usage


def attempt(name, day, tokens):
    usage.ingest([{"observation_id": name, "attempt_id": name, "origin": "manual",
                   "provider_id": "p", "model": "alias", "billing_model": "native",
                   "started_at": day + "T12:00:00Z",
                   "raw_usage": {"input_tokens": tokens, "output_tokens": 0}}])


def test_preview_recheck_and_repricing_uses_billing_model_and_effective_version():
    create_schema()
    api = FastAPI()
    api.include_router(price_corrections.router)
    client = TestClient(api)
    tariff = {"provider_id": "p", "model": "native", "effective_from": "2026-01-01T00:00:00Z",
              "currency": "USD", "input_per_million": "1", "output_per_million": "0"}
    usage.add_price(tariff)
    attempt("january", "2026-01-04", 100)
    attempt("february", "2026-02-28", 20)
    attempt("unknown", "2026-01-02", None)
    replacement = usage.add_price({**tariff, "currency": "CNY", "input_per_million": "2"})
    usage.add_price({**tariff, "effective_from": "2026-02-01T00:00:00Z"})
    url = "/api/usage/prices/" + replacement["id"]
    preview = client.post(url + "/preview").json()
    assert preview["before"] == {"USD": "0.0001"} and preview["after"] == {"CNY": "0.0002"}
    assert preview["count"] == 2 and preview["after_unknown"] == 1
    assert "CNY" not in usage.summary()["cost_by_currency"]
    assert client.post(url + "/apply", json={"fingerprint": preview["fingerprint"]}).status_code == 422
    usage.ingest([{"observation_id": "supplement", "attempt_id": "unknown", "origin": "proxy",
                   "started_at": "2026-01-02T12:00:00Z", "raw_usage": {"input_tokens": 10, "output_tokens": 0}}])
    assert client.post(url + "/apply", json={"confirmed": True, "fingerprint": preview["fingerprint"]}).status_code == 409
    refreshed = client.post(url + "/preview").json()
    result = client.post(url + "/apply", json={"confirmed": True, "fingerprint": refreshed["fingerprint"]})
    assert result.status_code == 200 and result.json()["count"] == 2
    assert usage.summary()["cost_by_currency"] == {"CNY": "0.00022", "USD": "0.00002"}
    assert client.post(url + "/preview").json()["count"] == 0
    with db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_price_corrections").fetchone()[0] == 2


def test_late_observation_preserves_unpriced_snapshot():
    create_schema()
    attempt("unknown-rate", "2026-01-04", 120)
    usage.add_price({"provider_id": "p", "model": "native", "effective_from": "2026-01-01T00:00:00Z",
                     "input_per_million": "2", "output_per_million": "1"})
    usage.ingest([{"origin": "native_log", "observation_id": "late-receipt", "attempt_id": "unknown-rate",
                   "started_at": "2026-01-04T12:00:00Z", "raw_usage": {"input_tokens": 130, "output_tokens": 2}}])
    assert usage.requests()["items"][0]["cost"]["status"] == "unpriced"
