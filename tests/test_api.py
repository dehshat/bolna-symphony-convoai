"""HTTP surface: Bolna tool webhooks, execution webhook, hybrid mode with a mocked Bolna, full simulation."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import bolna_client, config


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait_for(client, sid, predicate, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = client.get(f"/api/session/{sid}").json()
        if predicate(snap):
            return snap
        time.sleep(0.1)
    raise AssertionError(f"timeout; last snapshot: {snap}")


def test_index_and_config(client):
    assert client.get("/").status_code == 200
    cfg = client.get("/api/config").json()
    assert "coorg_suv" in cfg["presets"] and cfg["bolna_ready"] is False


def test_full_simulation_end_to_end(client):
    sid = client.post("/api/session", json={"preset": "airport_innova", "mode": "simulation"}).json()["session_id"]
    snap = wait_for(client, sid, lambda s: all(l["status"] in {"HOLD", "ENDED", "FAILED"} for l in s["legs"]))
    assert snap["current_best_quote"]["price"] <= snap["request"]["walkaway_ceiling"]
    assert snap["arbitrage_log"], "cross-call leverage must have happened"
    assert any(l["first_hidden"] > 0 for l in snap["legs"]), "the hidden-charge trap must be caught"
    best = snap["current_best_quote"]["leg_id"]
    res = client.post(f"/api/session/{sid}/lock", json={"leg_id": best}).json()
    assert res["payment"]["provider"] == "mock" and res["payment"]["amount"] >= 100
    assert res["whatsapp"]["link"].startswith("https://wa.me/")
    paid = client.post(f"/api/session/{sid}/mock_pay").json()
    assert paid["payment"]["status"] == "paid"


def test_lock_rejected_above_ceiling(client):
    from app import state
    sid = client.post("/api/session", json={"preset": "airport_innova", "autostart": False}).json()["session_id"]
    asyncio.run(state.engine.submit_and_counter(sid, "C", {"price": "3200", "toll": "included",
                                                            "driver_bata": "included", "vendor_said": "3200"}))
    r = client.post(f"/api/session/{sid}/lock", json={"leg_id": "C"})
    assert r.status_code == 409 and "ceiling" in r.json()["detail"]


def bolna_args(sid, leg, **kw):
    """Exactly what Bolna POSTs: the tool's $var param template filled with the LLM's string arguments."""
    base = {f: "unknown" for f in bolna_client.TOOL_FIELDS}
    base.update(session_id=sid, leg_id=leg, **kw)
    return base


def test_hybrid_mode_with_mocked_bolna(client, monkeypatch):
    """Leg A rings 'my phone' through Bolna; B and C are simulated. Then we act as Bolna: tool calls + webhook."""
    config.settings.bolna_api_key = "test-key"
    config.settings.bolna_agent_ids = {"hi": "agent-hi"}
    placed = {}

    async def fake_place_call(self, agent_id, phone, user_data, from_number=None):
        placed.update(agent_id=agent_id, phone=phone, user_data=user_data)
        return {"status": "queued", "execution_id": "exec-123"}

    async def fake_get_execution(self, execution_id):
        raise bolna_client.BolnaError("not needed")

    monkeypatch.setattr(bolna_client.BolnaClient, "place_call", fake_place_call)
    monkeypatch.setattr(bolna_client.BolnaClient, "get_execution", fake_get_execution)

    sid = client.post("/api/session", json={"preset": "airport_innova", "mode": "hybrid",
                                            "live_phone": "98450 12345"}).json()["session_id"]
    wait_for(client, sid, lambda s: s["legs"][0]["execution_id"] == "exec-123")
    assert placed["phone"] == "+919845012345" and placed["agent_id"] == "agent-hi"
    ud = placed["user_data"]
    assert ud["session_id"] == sid and ud["leg_id"] == "A" and "opening_line" in ud
    assert "2600" not in str(ud) and "2,600" not in str(ud), "the walk-away ceiling must never reach Bolna"

    # wait until a simulated competitor has a verified quote, then the human vendor (me) quotes high
    wait_for(client, sid, lambda s: any(l["best_verified"] for l in s["legs"][1:]))
    r = client.post("/api/tools/submit_and_counter", json=bolna_args(
        sid, "A", vendor_said="3000 lagega sab milake", price="3000", pricing_model="flat",
        toll="included", driver_bata="included", other_charges="included", availability="yes")).json()
    assert r["action"] == "COUNTER" and r["best_competing_quote_inr"] is not None
    assert r["counter_price_inr"] <= r["best_competing_quote_inr"]
    assert "operator" in r["say_this"] and r["say_this"] in r["instruction"]

    status = client.post("/api/tools/check_auction_status", json={"session_id": sid, "leg_id": "A"}).json()
    assert status["ok"]

    # execution webhook: in-progress, then completed with transcript + recording
    client.post("/api/webhooks/bolna_execution", json={"id": "exec-123", "status": "in-progress"})
    hook = client.post("/api/webhooks/bolna_execution", json={
        "id": "exec-123", "status": "completed", "transcript": "assistant: Namaste\nuser: 2800 lagega\n",
        "telephony_data": {"recording_url": "https://cdn.example.com/rec.mp3"},
        "context_details": {"recipient_data": {"session_id": sid, "leg_id": "A"}}}).json()
    assert hook["matched"]
    snap = client.get(f"/api/session/{sid}").json()
    leg = snap["legs"][0]
    assert leg["recording_url"] == "https://cdn.example.com/rec.mp3"
    assert leg["transcript"][0] == {"role": "agent", "text": "Namaste", "t": None}


def test_tool_call_with_mangled_ids_falls_back_to_the_single_live_leg(client, monkeypatch):
    from app import state
    sid = client.post("/api/session", json={"preset": "airport_innova", "autostart": False}).json()["session_id"]
    session = state.engine.get(sid)
    session.legs["A"].mode = "live"
    session.status = "RUNNING"
    r = client.post("/api/tools/submit_and_counter",
                    json=bolna_args("{session_id}", "{leg_id}", price="2500", vendor_said="2500")).json()
    assert r["ok"] and session.legs["A"].first_stated == 2500


def test_unresolvable_tool_call_returns_safe_instruction(client):
    r = client.post("/api/tools/submit_and_counter", json=bolna_args("nope", "Z", price="2500")).json()
    assert r["ok"] is False and "do not agree" in r["instruction"]


def test_tool_secret_enforced(client):
    config.settings.tool_shared_secret = "s3cret"
    sid = client.post("/api/session", json={"preset": "airport_innova", "autostart": False}).json()["session_id"]
    assert client.post("/api/tools/submit_and_counter", json=bolna_args(sid, "A")).status_code == 401
    ok = client.post("/api/tools/submit_and_counter", json=bolna_args(sid, "A", price="2400"),
                     headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    assert client.post("/api/webhooks/bolna_execution?secret=s3cret", json={"id": "x"}).status_code == 200


def test_agent_payload_shape():
    payload = bolna_client.build_agent_payload("kn", "https://swarm.example.com")
    task = payload["agent_config"]["tasks"][0]
    tools = task["tools_config"]
    assert tools["synthesizer"]["provider"] == "sarvam" and tools["synthesizer"]["provider_config"]["language"] == "kn-IN"
    assert tools["transcriber"]["provider"] == "sarvam"
    names = [t["name"] for t in tools["api_tools"]["tools"]]
    assert names == ["submit_and_counter", "check_auction_status"]
    params = tools["api_tools"]["tools_params"]["submit_and_counter"]
    assert params["url"] == "https://swarm.example.com/api/tools/submit_and_counter"
    assert params["param"]["price"] == {"$var": "price"}
    assert set(params["param"]) == set(tools["api_tools"]["tools"][0]["parameters"]["required"])
    assert payload["agent_config"]["webhook_url"].startswith("https://swarm.example.com/api/webhooks/bolna_execution")
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "{session_id}" in prompt and "{leg_id}" in prompt
    assert "ceiling" not in prompt.lower() or "do not know" in prompt.lower()


def test_parse_brief_rules_fallback(client):
    r = client.post("/api/parse_brief", json={"text": "1BHK shifting from JP Nagar to HSR Layout tomorrow, "
                                                      "budget 3000, max 4000"}).json()
    assert r["category"] == "mini_truck" and r["target_price"] == 3000 and r["walkaway_ceiling"] == 4000
    assert r["parsed_by"] == "rules"


def test_admin_dry_run_and_token(client):
    r = client.post("/api/admin/sync_agent", json={"dry_run": True, "languages": ["hi"]}).json()
    assert r["dry_run"] and "hi" in r["payloads"]
    config.settings.admin_token = "adm"
    assert client.post("/api/admin/sync_agent", json={"dry_run": True}).status_code == 401


def test_public_url_from_railway_domain(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "swarm-production.up.railway.app")
    assert config.reload_settings().public_base_url == "https://swarm-production.up.railway.app"


def test_auto_provision_reuses_existing_agent_and_creates_missing(monkeypatch):
    from app import main
    calls = []

    async def fake_list(self):
        return [{"id": "agent-hi-old", "name": "Swarm Negotiator (HI)"}]

    async def fake_update(self, agent_id, payload):
        calls.append(("update", agent_id, payload["agent_config"]["agent_name"]))
        return {"agent_id": agent_id, "state": "updated"}

    async def fake_create(self, payload):
        calls.append(("create", None, payload["agent_config"]["agent_name"]))
        return {"agent_id": "agent-kn-new", "state": "created"}

    monkeypatch.setattr(bolna_client.BolnaClient, "list_agents", fake_list)
    monkeypatch.setattr(bolna_client.BolnaClient, "update_agent", fake_update)
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", fake_create)
    config.settings.bolna_api_key = "bn-test"
    config.settings.bolna_agent_languages = "hi,kn"
    asyncio.run(main.auto_provision_bolna())
    assert main.BOLNA_SYNC["state"] == "ok"
    assert main.BOLNA_SYNC["agents"] == {"hi": "agent-hi-old", "kn": "agent-kn-new"}
    assert calls == [("update", "agent-hi-old", "Swarm Negotiator (HI)"), ("create", None, "Swarm Negotiator (KN)")]
    assert config.settings.bolna_ready and config.settings.agent_id_for("kn") == "agent-kn-new"


def test_auto_provision_reports_errors_without_crashing(monkeypatch):
    from app import main

    async def boom(self):
        raise bolna_client.BolnaError("Bolna GET /v2/agent/all -> 401: invalid key")

    async def fail_create(self, payload):
        raise bolna_client.BolnaError("Bolna POST /v2/agent -> 401: invalid key")

    monkeypatch.setattr(bolna_client.BolnaClient, "list_agents", boom)
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", fail_create)
    config.settings.bolna_api_key = "bn-bad"
    asyncio.run(main.auto_provision_bolna())
    assert main.BOLNA_SYNC["state"] == "error" and "401" in main.BOLNA_SYNC["detail"]
