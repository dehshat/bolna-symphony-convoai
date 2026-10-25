"""Any-service flow: chat intake → provider search/ranking → calls (negotiation or appointment) → finalize."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import bolna_client, config, intake, places, services
from app.auction_engine import AuctionEngine, Store
from app.negotiation import LegNegotiationState, decide
from app.pricing import ServiceRequest, normalize_quote


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app) as c:
        yield c


def chat(msgs):
    async def run():
        cid, r = None, None
        for m in msgs:
            r = await intake.chat(cid, m)
            cid = r["conversation_id"]
        return r
    return asyncio.run(run())


# ---------------------------------------------------------------- catalogue + intake

@pytest.mark.parametrize("text, key", [
    ("need a cook for my family", "home_cook"), ("my kitchen tap is leaking", "plumber"),
    ("book a dentist", "dentist"), ("fever, need a doctor", "doctor_clinic"), ("haircut at home", "salon"),
    ("maths tuition for class 8", "tutor"), ("shift my 2bhk", "mini_truck"), ("cab to airport", "cab_outstation"),
    ("cockroach problem", "pest_control"), ("terrace garden setup", "gardener"), ("ac not cooling", "ac_repair"),
])
def test_service_detection(text, key):
    assert services.detect(text).key == key


def test_chat_collects_everything_for_a_negotiated_service():
    r = chat(["", "I need a cook for my family", "Koramangala", "from next monday", "4 people, veg, 2 meals",
              "around 9000, max 12000"])
    t = r["task"]
    assert r["ready"] and t["service_key"] == "home_cook" and t["goal"] == "negotiate"
    assert t["location"] == "Koramangala, Bengaluru" and t["when"] == "next monday"
    assert (t["target_price"], t["walkaway_ceiling"]) == (9000, 12000) and "veg" in t["details"]


def test_chat_appointment_needs_no_budget_and_reads_one_line():
    r = chat(["", "Book a dentist appointment in HSR Layout tomorrow 11 am", "tooth pain"])
    t = r["task"]
    assert r["ready"] and t["goal"] == "appointment" and not t.get("walkaway_ceiling")
    assert t["when"] == "tomorrow 11 am" and t["details"] == "tooth pain"


def test_chat_suggests_a_budget_when_user_is_unsure():
    r = chat(["", "plumber needed in JP Nagar today", "skip", "no idea"])
    assert r["ready"] and r["task"]["walkaway_ceiling"] == services.BY_KEY["plumber"].typical[1]


def test_chat_handles_unlisted_services():
    r = chat(["", "someone to deliver groceries to my mom", "Jayanagar", "today 5 pm", "suggest"])
    assert r["ready"] and r["task"]["service_key"] == "other"
    req = intake.to_request(r["task"])
    assert req.category == "other" and req.goal == "negotiate" and req.walkaway_ceiling > 0


# ---------------------------------------------------------------- places

def test_ranking_prefers_proven_popularity_over_few_reviews():
    ps = [places.Provider(id="a", name="New", rating=5.0, reviews=3),
          places.Provider(id="b", name="Popular", rating=4.6, reviews=2400),
          places.Provider(id="c", name="Meh", rating=3.9, reviews=900)]
    assert [p.name for p in places.rank(ps)] == ["Popular", "New", "Meh"]


def test_test_mode_dials_the_test_phone_not_the_business():
    config.settings.live_test_phone = "+918317494732"
    ps = places.assign_dial_numbers([places.Provider(id="a", name="X", phone="080 4123 4567")])
    assert ps[0].phone == "+918041234567" and ps[0].dial_phone == "+918317494732" and ps[0].test_number
    config.settings.call_real_numbers = True
    ps = places.assign_dial_numbers([places.Provider(id="a", name="X", phone="080 4123 4567")])
    assert ps[0].dial_phone == "+918041234567" and not ps[0].test_number


def test_search_falls_back_to_demo_listings_when_no_source_is_available(monkeypatch):
    async def none(*a, **k):
        return []
    monkeypatch.setattr(places, "openstreetmap", none)
    monkeypatch.setattr(places, "BACKENDS", (("google_places", places.google_places),
                                             ("openstreetmap", none), ("demo", places.demo)))
    config.settings.live_test_phone = "+918317494732"
    r = asyncio.run(places.search("home_cook", "home cook", "Koramangala, Bengaluru"))
    assert r["source"] == "demo" and len(r["providers"]) >= 3
    scores = [p["score"] for p in r["providers"]]
    assert scores == sorted(scores, reverse=True)
    assert all(p["dial_phone"] == "+918317494732" for p in r["providers"])


# ---------------------------------------------------------------- appointment strategy

def appt_req(**kw):
    return ServiceRequest(category="dentist", goal="appointment", target_price=0, walkaway_ceiling=0,
                          when="tomorrow 11 am", **kw)


def test_booking_asks_slot_then_fee_then_holds_without_haggling():
    r, s = appt_req(), LegNegotiationState()
    assert decide(r, s, None, None, "en").action == "ASK_SLOT"
    s.slot = "tomorrow 11 AM"
    assert decide(r, s, None, None, "en").action == "ASK_FEE"
    q = normalize_quote(r, "600", "per_visit", {"other_charges": "included"})
    d = decide(r, s, q, None, "en")
    assert d.action == "HOLD_SLOT" and d.binding_price == 600 and "tomorrow 11 AM" in d.say and d.end_call
    assert d.counter_price is None


def test_booking_over_budget_is_declined():
    r = ServiceRequest(category="dentist", goal="appointment", target_price=300, walkaway_ceiling=500)
    s = LegNegotiationState(slot="today 5 PM", fee_asks=1)
    q = normalize_quote(r, "900", "per_visit", {"other_charges": "included"})
    assert decide(r, s, q, None, "en").action == "BOOKING_OVER_BUDGET"


def test_negotiation_needs_a_ceiling_but_appointment_does_not():
    with pytest.raises(ValueError):
        ServiceRequest(category="plumber", goal="negotiate", target_price=0, walkaway_ceiling=0)
    assert not appt_req().has_budget


def test_appointment_lock_requires_a_slot():
    async def scenario():
        e = AuctionEngine(store=Store(":memory:"))
        s = await e.create_session(appt_req(), [{"name": "Smile Dental"}, {"name": "City Dental"}])
        await e.submit_and_counter(s.session_id, "A", {"vendor_said": "kal 11 baje", "slot": "kal 11 baje"})
        await e.submit_and_counter(s.session_id, "A", {"vendor_said": "500 fees", "price": "500",
                                                       "pricing_model": "per_visit", "other_charges": "included"})
        from app.auction_engine import GuardrailViolation
        with pytest.raises(GuardrailViolation):
            await e.lock_winner(s.session_id, "B")
        return await e.lock_winner(s.session_id, "A")
    res = asyncio.run(scenario())
    assert res["slot"] == "kal 11 baje" and res["price"] == 500


# ---------------------------------------------------------------- API: full task flows

def wait(client, sid, pred, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        snap = client.get(f"/api/session/{sid}").json()
        if pred(snap):
            return snap
        time.sleep(0.1)
    raise AssertionError(snap)


def run_flow(client, msgs, mode="simulation", n=3):
    cid = None
    for m in msgs:
        r = client.post("/api/chat", json={"conversation_id": cid, "message": m}).json()
        cid = r["conversation_id"]
    assert r["ready"], r
    found = client.post("/api/providers/search", json={"conversation_id": cid}).json()
    assert found["providers"]
    sid = client.post("/api/session", json={"task": r["task"], "providers": found["providers"][:n], "mode": mode,
                                            "language": "hi"}).json()["session_id"]
    return sid, found


def test_cook_flow_simulated_end_to_end(client):
    sid, found = run_flow(client, ["", "I need a cook for my family", "Koramangala", "next monday",
                                   "4 people veg", "around 9000, max 12000"])
    snap = wait(client, sid, lambda s: all(l["status"] in {"HOLD", "ENDED"} for l in s["legs"]))
    assert snap["legs"][0]["provider"]["rating"] is not None
    best = snap["current_best_quote"]
    assert best and best["price"] <= 12000
    lock = client.post(f"/api/session/{sid}/lock", json={"leg_id": best["leg_id"]}).json()
    assert lock["payment"] and "home cook" in lock["whatsapp"]["text"]


def test_dentist_flow_books_a_slot_without_payment(client):
    sid, _ = run_flow(client, ["", "Book a dentist appointment in HSR Layout tomorrow 11 am", "tooth pain"])
    snap = wait(client, sid, lambda s: all(l["status"] in {"HOLD", "ENDED"} for l in s["legs"]))
    assert all(l["slot"] for l in snap["legs"])
    best = snap["current_best_quote"]
    lock = client.post(f"/api/session/{sid}/lock", json={"leg_id": best["leg_id"]}).json()
    assert lock["goal"] == "appointment" and lock["slot"] and lock["payment"] is None
    assert "Appointment booked" in lock["whatsapp"]["text"]


def test_live_test_mode_calls_one_after_another_and_confirms_the_winner(client, monkeypatch):
    config.settings.bolna_api_key = "bn-test"
    config.settings.bolna_agent_ids = {"hi": "agent-hi"}
    config.settings.live_test_phone = "+918317494732"
    placed = []

    async def fake_place_call(self, agent_id, phone, user_data, from_number=None):
        placed.append((phone, user_data["leg_id"], user_data["call_goal"][:12]))
        return {"execution_id": f"exec-{len(placed)}"}

    async def no_poll(self, execution_id):
        raise bolna_client.BolnaError("skip")

    monkeypatch.setattr(bolna_client.BolnaClient, "place_call", fake_place_call)
    monkeypatch.setattr(bolna_client.BolnaClient, "get_execution", no_poll)
    sid, found = run_flow(client, ["", "plumber needed in JP Nagar today", "leaking tap", "around 500, max 900"],
                          mode="live", n=2)
    wait(client, sid, lambda s: s["legs"][0]["execution_id"] == "exec-1")
    time.sleep(0.5)
    assert len(placed) == 1, "second call must wait: both legs ring the same test phone"
    assert placed[0][0] == "+918317494732"
    # vendor A (me, on the test phone) quotes, agent counters, call ends
    client.post("/api/tools/submit_and_counter", json={"session_id": sid, "leg_id": "A", "vendor_said": "600",
                                                       "price": "600", "other_charges": "included"})
    client.post("/api/webhooks/bolna_execution", json={"id": "exec-1", "status": "completed"})
    wait(client, sid, lambda s: s["legs"][1]["execution_id"] == "exec-2", timeout=15)
    assert [p[1] for p in placed] == ["A", "B"]
    client.post("/api/tools/submit_and_counter", json={"session_id": sid, "leg_id": "B", "vendor_said": "700",
                                                       "price": "700", "other_charges": "included"})
    client.post("/api/webhooks/bolna_execution", json={"id": "exec-2", "status": "completed"})
    client.post("/api/session/" + sid + "/lock", json={"leg_id": "A"})
    end = time.time() + 10
    while len(placed) < 3 and time.time() < end:
        time.sleep(0.1)
    assert placed[2][1] == "A" and placed[2][2].startswith("This is a CO")  # confirmation call to the winner
