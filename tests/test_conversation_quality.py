"""Conversation-quality behaviour: natural wording never changes numbers, calls converge, live lines are native script."""

import asyncio

from app import briefs, llm
from app.negotiation import Competitor, LegNegotiationState, decide
from app.pricing import ServiceRequest, normalize_quote

ALL_IN = {"toll": "included", "driver_bata": "included"}


def req():
    return ServiceRequest(category="cab_outstation", target_price=2200, walkaway_ceiling=2600, est_km=45,
                          min_km_per_day=0)


def step(r, s, price, competitor=None):
    previous = s.best_all_in
    q = normalize_quote(r, price, "flat", ALL_IN)
    if s.best_all_in is None or q.all_in < s.best_all_in:
        s.best_all_in = q.all_in
    return decide(r, s, q, competitor, "hi", previous_best=previous)


def test_amount_guard_rejects_invented_or_dropped_prices():
    assert llm._amounts_ok("Bhaiya ₹2,450 wala offer hai, ₹2,300 kar do?", [2450, 2300])
    assert not llm._amounts_ok("₹2,300 kar do", [2450, 2300])                 # dropped the competitor quote
    assert not llm._amounts_ok("₹2,450 hai, ₹2,300 ya ₹2,350 kar do", [2450, 2300])  # invented ₹2,350
    assert llm._amounts_ok("kal 6 baje, 1BHK ke liye ₹3,500", [3500])         # small numbers are fine


def test_agent_line_falls_back_when_model_invents_a_price(monkeypatch):
    async def fake_generate(*a, **k):
        return "Bhaiya ₹2,450 ka offer hai, aap ₹2,200 kar do?"  # changed ₹2,300 -> ₹2,200
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", fake_generate)
    original = "Bhaiya, ₹2,450 ka offer saamne hai. ₹2,300 last kar do?"
    assert asyncio.run(llm.agent_line("hi", original)) == original


def test_agent_line_accepts_faithful_rewrite(monkeypatch):
    async def fake_generate(*a, **k):
        return "Achha bhaiya, ₹2,450 ka offer mere paas hai, ₹2,300 final kar do na?"
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", fake_generate)
    out = asyncio.run(llm.agent_line("hi", "Bhaiya, ₹2,450 ka offer saamne hai. ₹2,300 last kar do?"))
    assert out.startswith("Achha bhaiya")


def test_repeated_counter_uses_shorter_follow_up_line():
    r, s = req(), LegNegotiationState()
    comp = Competitor("B", "Other", 2450)
    first = step(r, s, 2900, comp)
    second = step(r, s, 2800, comp)
    assert "operator" in first.say and "operator" not in second.say and "2,450" in second.say


def test_vendor_who_will_not_move_is_closed_politely():
    r, s = req(), LegNegotiationState()
    step(r, s, 2550)
    step(r, s, 2500)
    step(r, s, 2500)
    d = step(r, s, 2500)
    assert d.action == "FINAL_HOLD" and d.binding_price == 2500 and d.end_call


def test_live_instruction_asks_for_native_script():
    d = decide(req(), LegNegotiationState(), None, None, "hi")
    assert "Devanagari" in d.instruction
    d = decide(req(), LegNegotiationState(), None, None, "kn")
    assert "Kannada script" in d.instruction


def test_live_opening_line_is_native_script():
    r = briefs.request_from_preset("airport_innova")
    hi = briefs.opening_line(r, "hi", native=True)
    kn = briefs.opening_line(r, "kn", native=True)
    assert "कल सुबह 6 बजे" in hi and "Innova" in hi
    assert "ನಾಳೆ ಬೆಳಿಗ್ಗೆ 6 ಗಂಟೆಗೆ" in kn
    assert briefs.opening_line(r, "hi").startswith("Haan bhaiya")  # on-screen simulation stays romanized


def test_rate_limited_gemini_answers_instantly(monkeypatch):
    import time
    monkeypatch.setattr(llm, "enabled", lambda: True)
    for model in llm.FALLBACK_MODELS + (llm.config.settings.gemini_model,):
        llm._cooldown[model] = time.time() + 3600
    try:
        start = time.time()
        assert asyncio.run(llm.generate("hi")) is None
        assert time.time() - start < 0.2
        assert llm.status()["rate_limited_models"]
    finally:
        llm._cooldown.clear()


def test_tied_vendor_is_not_called_the_best():
    r, s = req(), LegNegotiationState()
    d = step(r, s, 2500, Competitor("B", "Other", 2500))
    assert d.action == "COUNTER" and "best" not in d.say.lower() and "achha" not in d.say.lower()
