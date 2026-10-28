"""Every scripted call scenario must pass, and the tool endpoint must cope with what Bolna really sends."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import scenarios, state
from app.pricing import ServiceRequest


@pytest.mark.parametrize("scenario", scenarios.SCENARIOS, ids=lambda s: s.name)
def test_scenario(scenario):
    report = asyncio.run(scenarios.run_all([scenario.name]))
    result = report["results"][0]
    assert result["ok"], "\n".join(result["problems"]) + "\n" + scenarios.render(report)


def test_scenario_names_are_unique():
    names = [s.name for s in scenarios.SCENARIOS]
    assert len(names) == len(set(names))


def test_tool_endpoint_accepts_arguments_as_a_json_string():
    from app.main import app
    with TestClient(app) as client:
        req = ServiceRequest(category="plumber", service_label="plumber", target_price=500, walkaway_ceiling=800)
        session = asyncio.run(state.engine.create_session(req, [{"name": "Lakshmi", "language": "hi",
                                                                 "mode": "live"}], "live"))
        args = {"session_id": session.session_id, "leg_id": "a", "vendor_said": "450 all included", "price": "₹450",
                "pricing_model": "flat", "toll": "na", "driver_bata": "included", "other_charges": "included",
                "extras": "none", "notes": "none", "availability": "yes", "slot": "unknown",
                "situation": "%(situation)s"}
        r = client.post("/api/tools/submit_and_counter", json={"arguments": json.dumps(args)})
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "ACCEPT_HOLD" and body["agreed_price_inr"] == 450


def test_self_test_endpoint_runs_every_scenario():
    from app.main import app
    with TestClient(app) as client:
        r = client.get("/api/selftest")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == len(scenarios.SCENARIOS) and body["passed"] == body["total"]


def test_a_crash_inside_the_tool_gives_the_agent_a_safe_line(monkeypatch):
    from app.main import app

    async def boom(*a, **k):
        raise RuntimeError("bug")
    with TestClient(app) as client:
        req = ServiceRequest(category="plumber", service_label="plumber", target_price=500, walkaway_ceiling=800)
        session = asyncio.run(state.engine.create_session(req, [{"name": "L", "language": "hi", "mode": "live"}],
                                                          "live"))
        monkeypatch.setattr(state.engine, "submit_and_counter", boom)
        r = client.post("/api/tools/submit_and_counter", json={"session_id": session.session_id, "leg_id": "A",
                                                                "vendor_said": "500", "price": "500"})
        assert r.status_code == 200 and r.json()["action"] == "SAFE_FALLBACK"
        assert "Do not agree" in r.json()["instruction"]


def test_rate_limit_on_map_search(monkeypatch):
    from app import config
    from app.main import app
    monkeypatch.setattr(config.settings, "rate_limit_search", 2)
    with TestClient(app) as client:
        body = {"task": {"service_key": "plumber", "service_label": "plumber", "location": "Koramangala"}}
        codes = [client.post("/api/providers/search", json=body).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_repeat_search_is_served_from_cache(monkeypatch):
    from app import places
    calls = []

    async def fake_google(svc, location, lat, lng, limit):
        calls.append(location)
        return [places.Provider(id="g1", name="Real Plumber", phone="+919812345678", rating=4.8, reviews=120,
                                source="Google Maps")]
    monkeypatch.setattr(places, "BACKENDS", (("google_places", fake_google), ("demo", places.demo)))
    first = asyncio.run(places.search("plumber", "plumber", "Koramangala"))
    second = asyncio.run(places.search("plumber", "plumber", "Koramangala"))
    assert len(calls) == 1 and first["providers"][0]["name"] == second["providers"][0]["name"] == "Real Plumber"
    assert second["tried"][0].get("cached")


def test_health_reports_the_deployed_commit(monkeypatch):
    from app.main import app
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abc1234def")
    with TestClient(app) as client:
        assert client.get("/api/health").json()["commit"] == "abc1234"
