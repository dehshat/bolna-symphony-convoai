"""The agents around the calls: concierge (talks to you), linguist, dialer (on-demand voice agents), advisor, voice."""

import asyncio
import base64
import json
import struct
import time

import pytest
from fastapi.testclient import TestClient

from app import advisor, bolna_client, concierge, config, intake, llm, localizer, places
from app.auction_engine import AuctionEngine, Store
from app.pricing import ServiceRequest


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def chat(msgs, **kw):
    async def run():
        cid, r = None, None
        for m in msgs:
            r = await intake.chat(cid, m, **kw)
            cid = r["conversation_id"]
        return r
    return asyncio.run(run())


# ---------------------------------------------------------------- concierge: rules path (no Gemini)

def test_location_picks_the_call_language_and_yes_starts_the_calls():
    r = chat(["", "I need a cook in Kothrud, Pune", "from next monday", "4 people veg", "around 9000, max 12000"])
    assert r["task"]["location"] == "Kothrud, Pune" and r["call_language"]["code"] == "mr"
    assert "Marathi" in r["reply"] and r["action"] == ""
    assert any(t["agent"] == "linguist" for t in r["trace"])
    r = asyncio.run(intake.chat(r["conversation_id"], "हाँ"))
    assert r["action"] == "go"


def test_asking_for_a_language_overrides_the_place_and_is_not_a_place():
    r = chat(["", "plumber in Chennai today", "leak", "around 500 max 900"])
    assert r["call_language"]["code"] == "ta"
    r = asyncio.run(intake.chat(r["conversation_id"], "call them in Hindi"))
    assert r["action"] == "" and r["task"]["location"] == "Chennai"
    assert r["call_language"]["code"] == "hi" and r["call_language"]["locked"]
    r = asyncio.run(intake.chat(r["conversation_id"], "Velachery, Chennai"))   # a later place doesn't undo the choice
    assert r["call_language"]["code"] == "hi"


def test_no_at_the_confirm_step_then_a_correction_reconfirms():
    r = chat(["", "need a dentist in Andheri, Mumbai tomorrow 11 am", "checkup", "no"])
    assert "change" in r["reply"].lower()
    r = asyncio.run(intake.chat(r["conversation_id"], "tomorrow 5 pm instead"))
    assert r["task"]["when"] == "tomorrow 5 pm" and "Shall I call" in r["reply"]
    assert asyncio.run(intake.chat(r["conversation_id"], "ok"))["action"] == "go"


def test_picking_the_language_on_screen_is_sticky():
    r = chat([""])
    r = asyncio.run(intake.chat(r["conversation_id"], "", call_language="od"))
    assert r["call_language"]["code"] == "od" and r["call_language"]["locked"]
    r = asyncio.run(intake.chat(r["conversation_id"], "plumber in Koramangala, Bengaluru"))
    assert r["call_language"]["code"] == "od"


def test_gps_state_picks_the_language(monkeypatch):
    async def fake_reverse(lat, lng):
        places._geo_state[(round(lat, 3), round(lng, 3))] = "Maharashtra"
        return "Shivaji Nagar"
    monkeypatch.setattr(places, "reverse_geocode", fake_reverse)

    async def run():
        r = await intake.chat(None, "I need an electrician near me")
        return await intake.chat(r["conversation_id"], "", lat=18.53, lng=73.85)
    r = asyncio.run(run())
    assert r["task"]["location"] == "Shivaji Nagar" and r["call_language"]["code"] == "mr"


def test_hindi_speaker_gets_hindi_questions_without_gemini():
    r = chat([""], user_lang="hi")
    assert r["reply"].startswith("नमस्ते") and r["reply_language"] == "hi"


def test_start_over():
    r = chat(["", "plumber in JP Nagar today", "start over"])
    assert not r["task"].get("service_key")


# ---------------------------------------------------------------- concierge: Gemini path

def fake_gemini(monkeypatch, reply: dict):
    async def gen(*a, **k):
        assert k.get("role") == "chat"
        return "```json\n" + json.dumps(reply, ensure_ascii=False) + "\n```"   # fenced, as Gemma returns it
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", gen)


def test_gemini_understands_hindi_and_its_reply_is_used(monkeypatch):
    fake_gemini(monkeypatch, {"updates": {"service_key": "plumber", "service_label": "plumber",
                                          "location": "Kothrud, Pune"},
                              "asking": "when", "reply": "ठीक है, पुणे में प्लंबर। कब चाहिए?", "reply_language": "hi",
                              "intent": "inform"})
    r = asyncio.run(intake.chat(None, "मुझे कोथरूड पुणे में नल ठीक करने वाला चाहिए"))
    assert r["task"]["service_key"] == "plumber" and r["call_language"]["code"] == "mr"
    assert r["reply"] == "ठीक है, पुणे में प्लंबर। कब चाहिए?" and r["reply_language"] == "hi"


def test_gemini_reply_on_the_wrong_topic_or_with_an_invented_price_is_not_used(monkeypatch):
    fake_gemini(monkeypatch, {"updates": {"service_key": "plumber"}, "asking": "budget",
                              "reply": "Plumbers here charge 2,750, okay?", "intent": "inform"})
    r = asyncio.run(intake.chat(None, "need a plumber"))
    assert r["reply"].startswith("Got it: plumber. Which area")     # rules decide what is asked next
    task = {"service_key": "plumber"}
    assert not concierge.reply_ok("It'll be about ₹2,750", task)
    assert concierge.reply_ok("Plumbers usually charge ₹300 to ₹1,500. What's your max?", task)


def test_gemini_yes_in_any_language_starts_the_calls(monkeypatch):
    r = chat(["", "plumber in JP Nagar today", "leak", "around 500 max 900"])
    fake_gemini(monkeypatch, {"updates": {}, "asking": "none", "reply": "ਠੀਕ ਹੈ", "intent": "yes"})
    assert asyncio.run(intake.chat(r["conversation_id"], "ਠੀਕ ਹੈ ਜੀ ਕਰ ਦਿਓ"))["action"] == "go"


# ---------------------------------------------------------------- Gemini plumbing

def test_each_model_family_gets_a_request_it_accepts():
    body = {"contents": [{"role": "user", "parts": [{"text": "hi"}]}], "systemInstruction": {"parts": [{"text": "S"}]},
            "generationConfig": {"responseMimeType": "application/json"}}
    gemma = llm._adapt("gemma-3-27b-it", body)
    assert "systemInstruction" not in gemma and gemma["contents"][0]["parts"][0]["text"].startswith("S")
    assert "responseMimeType" not in gemma["generationConfig"]
    assert llm._adapt("gemini-2.5-flash-lite", body)["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 0}
    assert "thinkingConfig" not in llm._adapt("gemini-2.0-flash", body)["generationConfig"]
    assert "thinkingConfig" not in llm._adapt("gemini-2.5-flash-preview-tts", body)["generationConfig"]


def test_roles_use_separate_models():
    assert llm.ladder("chat")[0] != llm.ladder("parse")[0]      # the concierge doesn't eat the parser's quota
    assert all("tts" in m for m in llm.ladder("tts")) and "gemma-3-27b-it" in llm.ladder("chat")


def test_speak_returns_a_playable_wav(monkeypatch):
    pcm = struct.pack("<4h", 0, 1000, -1000, 0)

    async def fake_call(body, timeout, role="parse"):
        assert role == "tts" and body["generationConfig"]["responseModalities"] == ["AUDIO"]
        return {"content": {"parts": [{"inlineData": {"mimeType": "audio/L16;codec=pcm;rate=24000",
                                                      "data": base64.b64encode(pcm).decode()}}]}}
    monkeypatch.setattr(llm, "_call", fake_call)
    audio = asyncio.run(llm.speak("नमस्ते"))
    assert audio[:4] == b"RIFF" and audio[8:12] == b"WAVE" and audio.endswith(pcm)
    assert struct.unpack("<I", audio[24:28])[0] == 24000


def test_transcribe_parses_the_model_json(monkeypatch):
    async def fake_call(body, timeout, role="parse"):
        assert role == "stt" and body["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "audio/wav"
        return {"content": {"parts": [{"text": '{"text": "मला प्लंबर हवा आहे", "language": "mr"}'}]}}
    monkeypatch.setattr(llm, "_call", fake_call)
    assert asyncio.run(llm.transcribe(b"RIFF....", "audio/wav", "mr")) == {"text": "मला प्लंबर हवा आहे",
                                                                          "language": "mr"}


def test_voice_endpoints(client, monkeypatch):
    assert client.post("/api/tts", json={"text": "hello"}).status_code == 503    # no Gemini key in tests
    assert client.post("/api/stt", json={"audio": "!!not base64!!", "mime": "audio/wav"}).status_code == 422
    assert client.post("/api/stt", json={"audio": "A" * 3_000_004, "mime": "audio/wav"}).status_code == 413

    async def fake_speak(text):
        return llm.wav(b"\x00\x00" * 10)

    async def fake_transcribe(audio, mime, hint):
        return {"text": "plumber in Pune", "language": "en"}
    monkeypatch.setattr(llm, "speak", fake_speak)
    monkeypatch.setattr(llm, "transcribe", fake_transcribe)
    r = client.post("/api/tts", json={"text": "नमस्ते"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav" and r.content[:4] == b"RIFF"
    audio = base64.b64encode(llm.wav(b"\x00\x00" * 100)).decode()
    assert client.post("/api/stt", json={"audio": audio, "mime": "audio/wav"}).json()["text"] == "plumber in Pune"


def test_languages_endpoint(client):
    r = client.get("/api/languages", params={"location": "Kothrud, Pune"}).json()
    assert r["detected"] == "mr" and len(r["languages"]) == 11
    cfg = client.get("/api/config").json()
    assert len(cfg["languages"]) == 11 and "हाँ" in cfg["voice_commands"]["yes"]


# ---------------------------------------------------------------- linguist

def test_localizer_keeps_only_pure_native_fields(monkeypatch):
    async def gen(*a, **k):
        assert k["role"] == "localize"
        return json.dumps({"service": "ನಲ್ಲಿ ರಿಪೇರಿ ಮಾಡೋರು", "details": "kitchen ನಲ್ಲಿ leak",
                           "place": "ಕೋರಮಂಗಲ", "when": "ಇವತ್ತು"})
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", gen)
    localizer._cache.clear()
    req = ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800, location="Koramangala",
                         when="today", details="kitchen tap leaking")
    out = asyncio.run(localizer.localize_job(req, "kn"))
    assert out == {"service": "ನಲ್ಲಿ ರಿಪೇರಿ ಮಾಡೋರು", "place": "ಕೋರಮಂಗಲ", "when": "ಇವತ್ತು"}   # English details dropped


# ---------------------------------------------------------------- dialer: a voice agent per language, on demand

def test_marathi_call_creates_the_marathi_agent_on_first_use(client, monkeypatch):
    config.settings.bolna_api_key = "bn-test"
    config.settings.bolna_agent_ids = {"hi": "agent-hi"}
    config.settings.live_test_phone = "+918317494732"
    created, placed = [], []

    async def fake_list(self):
        return []

    async def fake_create(self, payload):
        created.append(payload["agent_config"]["agent_name"])
        return {"agent_id": "agent-mr"}

    async def fake_place(self, agent_id, phone, user_data, from_number=None):
        placed.append((agent_id, user_data))
        return {"execution_id": "exec-mr"}

    async def no_poll(self, execution_id):
        raise bolna_client.BolnaError("skip")
    monkeypatch.setattr(bolna_client.BolnaClient, "list_agents", fake_list)
    monkeypatch.setattr(bolna_client.BolnaClient, "create_agent", fake_create)
    monkeypatch.setattr(bolna_client.BolnaClient, "place_call", fake_place)
    monkeypatch.setattr(bolna_client.BolnaClient, "get_execution", no_poll)
    cid = None
    for m in ["", "plumber in Kothrud, Pune today", "tap leaking", "around 500, max 900"]:
        r = client.post("/api/chat", json={"conversation_id": cid, "message": m}).json()
        cid = r["conversation_id"]
    found = client.post("/api/providers/search", json={"conversation_id": cid}).json()
    sid = client.post("/api/session", json={"task": r["task"], "providers": found["providers"][:1],
                                            "mode": "live"}).json()["session_id"]
    end = time.time() + 10
    while not placed and time.time() < end:
        time.sleep(0.05)
    assert created == ["Swarm Negotiator (MR)"] and placed[0][0] == "agent-mr"
    ud = placed[0][1]
    assert ud["language"] == "mr" and ud["opening_line"] == ud["job_native"] and "प्लंबर" in ud["opening_line"]
    assert "900" not in json.dumps(ud), "the max never reaches the voice agent"
    trace = client.get(f"/api/session/{sid}").json()["trace"]
    assert any("Marathi voice agent" in t["text"] for t in trace)


def test_a_language_agent_is_never_borrowed_from_another_language(monkeypatch):
    config.settings.bolna_api_key = "bn-test"
    config.settings.bolna_agent_ids = {"hi": "agent-hi"}
    config.settings.public_base_url = ""
    with pytest.raises(bolna_client.BolnaError):
        asyncio.run(bolna_client.ensure_agent("ta"))
    assert asyncio.run(bolna_client.ensure_agent("hi")) == "agent-hi"


# ---------------------------------------------------------------- advisor

def make_session(goal="negotiate"):
    async def run():
        e = AuctionEngine(Store(":memory:"))
        if goal == "negotiate":
            req = ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800)
        else:
            req = ServiceRequest(category="dentist", goal="appointment", target_price=0, walkaway_ceiling=0)
        legs = [{"name": "A Plumbing", "provider": {"rating": 4.8, "reviews": 900}},
                {"name": "B Plumbing", "provider": {"rating": 3.9, "reviews": 20}},
                {"name": "C Plumbing", "provider": {"rating": 4.9, "reviews": 3000}}]
        s = await e.create_session(req, legs)
        return e, s
    return asyncio.run(run())


def test_advisor_recommends_with_reasons():
    e, s = make_session()
    quote = lambda p: {"vendor_said": f"{p} all included", "price": str(p), "other_charges": "included",  # noqa: E731
                       "driver_bata": "included"}
    asyncio.run(e.submit_and_counter(s.session_id, "A", quote(520)))
    asyncio.run(e.submit_and_counter(s.session_id, "B", quote(500)))
    asyncio.run(e.submit_and_counter(s.session_id, "C", quote(1200)))       # above the ceiling: never recommended
    for leg in s.legs.values():
        leg.status = "HOLD" if leg.leg_id != "C" else "ENDED"
    rec = advisor.recommend(s)
    assert rec["all_done"] and rec["leg_id"] == "A"    # ₹20 more, but ★4.8 from 900 beats ★3.9 from 20
    assert any("rated ★4.8" in r for r in rec["reasons"]) and rec["runner_up"]["leg_id"] == "B"


def test_advisor_says_so_when_nothing_fits():
    e, s = make_session()
    for leg in s.legs.values():
        leg.status = "ENDED"
    rec = advisor.recommend(s)
    assert rec["leg_id"] is None and "budget" in rec["summary"]


def test_advisor_for_appointments_needs_a_slot():
    e, s = make_session("appointment")
    asyncio.run(e.submit_and_counter(s.session_id, "B", {"vendor_said": "tomorrow 11", "slot": "tomorrow 11 AM"}))
    s.legs["B"].status = "HOLD"
    s.legs["A"].status = s.legs["C"].status = "ENDED"
    rec = advisor.recommend(s)
    assert rec["leg_id"] == "B" and "slot tomorrow 11 AM" in rec["reasons"]


# ---------------------------------------------------------------- housekeeping (Render free plan: 512 MB)

def test_old_sessions_and_conversations_are_pruned(client):
    from app import main, state
    sid = client.post("/api/session", json={"preset": "airport_innova", "autostart": False}).json()["session_id"]
    cid = client.post("/api/chat", json={"message": ""}).json()["conversation_id"]
    assert main.prune(time.time() + 3600) == {"sessions": 0, "conversations": 0}
    later = time.time() + 7 * 3600
    assert main.prune(later) == {"sessions": 1, "conversations": 1}
    assert sid not in state.engine.sessions and cid not in intake.CONVERSATIONS


def test_selftest_reports_every_language(client):
    body = client.get("/api/selftest").json()
    assert body["languages"]["passed"] == body["languages"]["total"]
    assert len(body["languages"]["languages"]) == 11
