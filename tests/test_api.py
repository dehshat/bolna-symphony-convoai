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


def test_agent_uses_current_sarvam_models():
    tools = bolna_client.build_agent_payload("hi")["agent_config"]["tasks"][0]["tools_config"]
    assert tools["synthesizer"]["provider_config"]["model"] == "bulbul:v3"
    assert tools["synthesizer"]["provider_config"]["voice_id"] == "shubh"


def test_deprecated_model_is_swapped_and_retried(monkeypatch):
    sent = []

    async def fake_list(self):
        return []

    async def fake_create(self, payload):
        model = payload["agent_config"]["tasks"][0]["tools_config"]["synthesizer"]["provider_config"]["model"]
        sent.append(model)
        if model == "bulbul:v3":
            raise bolna_client.BolnaError('Bolna POST /v2/agent -> 400: {"message":"Model \'bulbul:v3\' is '
                                          'deprecated and can no longer be used. Use \'bulbul:v4\' instead."}')
        return {"agent_id": "agent-new"}

    monkeypatch.setattr(bolna_client.BolnaClient, "list_agents", fake_list)
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", fake_create)
    config.settings.bolna_api_key = "bn-test"
    result = asyncio.run(bolna_client.sync_agents(["hi"], "https://x.onrender.com"))
    assert sent == ["bulbul:v3", "bulbul:v4"] and result["agents"] == {"hi": "agent-new"}


class FakeBolnaValidator:
    """Mimics the hosted API's validation messages seen in production."""

    def __init__(self, synth_wants_plain=False, reject_lang=None):
        self.synth_wants_plain = synth_wants_plain
        self.reject_lang = reject_lang
        self.attempts = 0

    async def create(self, _client, payload):
        self.attempts += 1
        tools = payload["agent_config"]["tasks"][0]["tools_config"]
        asr, tts = tools["transcriber"], tools["synthesizer"]["provider_config"]
        if self.reject_lang and asr["language"].startswith(self.reject_lang):
            raise bolna_client.BolnaError('Bolna POST /v2/agent -> 400: {"message":"Something else entirely"}')
        if tts["model"] == "bulbul:v2":
            raise bolna_client.BolnaError("Bolna POST /v2/agent -> 400: {\"message\":\"Model 'bulbul:v2' is deprecated "
                                          "and can no longer be used. Use 'bulbul:v3' instead.\"}")
        if "-" in asr["language"]:
            raise bolna_client.BolnaError('Bolna POST /v2/agent -> 400: {"message":"Provided language: '
                                          f'{asr["language"]} is not available for the model: {asr["model"]}"}}')
        if self.synth_wants_plain and "-" in tts["language"]:
            raise bolna_client.BolnaError('Bolna POST /v2/agent -> 400: {"message":"Provided language: '
                                          f'{tts["language"]} is not available for the model: {tts["model"]}"}}')
        return {"agent_id": f"agent-{asr['language']}"}


def _install(monkeypatch, validator):
    async def fake_list(self):
        return []
    monkeypatch.setattr(bolna_client.BolnaClient, "list_agents", fake_list)
    async def create(client_self, payload):
        return await validator.create(client_self, payload)
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", create)
    config.settings.bolna_api_key = "bn-test"


def test_default_payload_passes_known_validation_first_time(monkeypatch):
    v = FakeBolnaValidator()
    _install(monkeypatch, v)
    result = asyncio.run(bolna_client.sync_agents(["hi", "kn"], "https://x.onrender.com"))
    assert result["agents"] == {"hi": "agent-hi", "kn": "agent-kn"} and v.attempts == 2
    tools = bolna_client.build_agent_payload("hi")["agent_config"]["tasks"][0]["tools_config"]
    assert tools["transcriber"]["model"] == "saaras:v4" and tools["transcriber"]["language"] == "hi"
    assert "sampling_rate" not in tools["transcriber"] and "encoding" not in tools["transcriber"]


def test_locale_format_is_self_corrected(monkeypatch):
    config.settings.sarvam_tts_model = "bulbul:v2"  # also exercise the retired-model swap
    v = FakeBolnaValidator(synth_wants_plain=True)
    _install(monkeypatch, v)
    payload = bolna_client.build_agent_payload("hi")
    payload["agent_config"]["tasks"][0]["tools_config"]["transcriber"]["language"] = "hi-IN"
    created = asyncio.run(bolna_client._push_agent(bolna_client.BolnaClient(), payload, ""))
    tools = payload["agent_config"]["tasks"][0]["tools_config"]
    assert created["agent_id"] == "agent-hi"
    assert tools["transcriber"]["language"] == "hi" and tools["synthesizer"]["provider_config"]["language"] == "hi"
    assert tools["synthesizer"]["provider_config"]["model"] == "bulbul:v3"


def test_one_language_failing_does_not_block_the_other(monkeypatch):
    from app import main
    v = FakeBolnaValidator(reject_lang="kn")
    _install(monkeypatch, v)
    config.settings.bolna_agent_languages = "hi,kn"
    asyncio.run(main.auto_provision_bolna())
    assert main.BOLNA_SYNC["state"] == "partial" and main.BOLNA_SYNC["agents"] == {"hi": "agent-hi"}
    assert config.settings.agent_id_for("hi") == "agent-hi"


def test_unfixable_error_is_reported_not_looped(monkeypatch):
    async def always(self, payload):
        raise bolna_client.BolnaError('Bolna POST /v2/agent -> 400: {"message":"Provided language: hi is not '
                                      'available for the model: saaras:v4"}')
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", always)
    config.settings.bolna_api_key = "bn-test"
    with pytest.raises(bolna_client.BolnaError):
        asyncio.run(bolna_client._push_agent(bolna_client.BolnaClient(), bolna_client.build_agent_payload("hi"), ""))


def test_trial_unverified_number_error_is_explained(client, monkeypatch):
    config.settings.bolna_api_key = "bn-test"
    config.settings.bolna_agent_ids = {"hi": "agent-hi"}

    async def trial_reject(self, agent_id, phone, user_data, from_number=None):
        raise bolna_client.BolnaError('Bolna POST /call -> 400: {"message":"Trial accounts can only make calls to '
                                      'verified phone numbers."}')

    monkeypatch.setattr(bolna_client.BolnaClient, "place_call", trial_reject)
    sid = client.post("/api/session", json={"preset": "airport_innova", "mode": "hybrid",
                                            "live_phone": "+918317494732"}).json()["session_id"]
    snap = wait_for(client, sid, lambda s: s["legs"][0]["status"] == "FAILED")
    assert "Verified numbers" in snap["legs"][0]["error"] and "+918317494732" in snap["legs"][0]["error"]
