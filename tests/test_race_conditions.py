"""Concurrency + shared-memory arbitrage: the core claim of the product."""

import asyncio

import pytest

from app.auction_engine import AuctionEngine, GuardrailViolation, LegStatus, Store
from app.pricing import ServiceRequest

VENDORS = [{"name": "Sri Sai Travels", "language": "kn"}, {"name": "Namma Cabs", "language": "hi"},
           {"name": "Metro Cab", "language": "hi"}]
ALL_IN = {"toll": "included", "driver_bata": "included", "other_charges": "included", "pricing_model": "flat"}


def make_engine():
    return AuctionEngine(store=Store(":memory:"))


def req():
    return ServiceRequest(category="cab_outstation", target_price=2200, walkaway_ceiling=2600, est_km=45,
                          min_km_per_day=0)


async def new_session(engine):
    return await engine.create_session(req(), VENDORS)


def quote(price, said=""):
    return {"price": str(price), "vendor_said": said or f"{price} all inclusive", **ALL_IN}


def run(coro):
    return asyncio.run(coro)


def test_quote_on_leg_a_becomes_leverage_on_leg_b():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        a = await e.submit_and_counter(s.session_id, "A", quote(2500))
        b = await e.submit_and_counter(s.session_id, "B", quote(2700))
        return s, a, b
    s, a, b = run(scenario())
    assert a["best_competing_quote_inr"] is None
    assert b["action"] == "COUNTER" and b["best_competing_quote_inr"] == 2500
    assert b["counter_price_inr"] == 2250 and "2,500" in b["say_this"]
    assert s.arbitrage_log[-1]["from_leg"] == "A" and s.arbitrage_log[-1]["to_leg"] == "B"


def test_all_three_pick_up_simultaneously_with_no_anchor():
    """Nobody has quoted: every leg must make the vendor quote first, without inventing a competitor."""
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        asks = [{"vendor_said": "budget kitna hai?", "price": "unknown"}] * 3
        return await asyncio.gather(*(e.submit_and_counter(s.session_id, leg, args)
                                      for leg, args in zip("ABC", asks)))
    results = run(scenario())
    assert {r["action"] for r in results} == {"ASK_VENDOR_FIRST"}
    for r in results:
        assert "operator" not in r["say_this"] and "2,200" not in r["say_this"]


def test_simultaneous_quotes_serialize_with_monotonic_versions():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        results = await asyncio.gather(*(e.submit_and_counter(s.session_id, leg, quote(p))
                                         for leg, p in zip("ABC", (2500, 2450, 2700))))
        return s, results
    s, results = run(scenario())
    versions = [r["state_version"] for r in results]
    assert len(set(versions)) == 3, "every tool call must see a distinct, serialized state version"
    # Whoever was processed later saw the earlier quotes; nobody was ever offered their own quote as leverage.
    for entry in s.arbitrage_log:
        assert entry["from_leg"] != entry["to_leg"]
    assert s.current_best().price == 2450


def test_many_concurrent_tool_calls_never_break_invariants():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        calls = []
        for i in range(60):
            leg = "ABC"[i % 3]
            calls.append(e.submit_and_counter(s.session_id, leg, quote(2900 - (i * 13) % 700)))
            calls.append(e.check_auction_status(s.session_id, "ABC"[(i + 1) % 3]))
        results = await asyncio.gather(*calls)
        return s, results
    s, results = run(scenario())
    ceiling = s.request.walkaway_ceiling
    for r in results:
        for key in ("counter_price_inr", "agreed_price_inr"):
            assert r.get(key) is None or r[key] <= ceiling
        assert "walkaway" not in str(r) and "ceiling" not in str(r)
    assert s.state_version >= len(results)


def test_hidden_charge_trap_is_caught_then_resolved():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        first = await e.submit_and_counter(s.session_id, "A", {"price": "2250", "vendor_said": "2250 only"})
        second = await e.submit_and_counter(s.session_id, "A", {"price": "unknown", "toll": "extra 120",
                                                                "driver_bata": "extra 400",
                                                                "vendor_said": "toll and bata extra"})
        return s, first, second
    s, first, second = run(scenario())
    assert first["action"] == "ASK_INCLUSIONS"
    assert second["vendor_all_in_estimate_inr"] == 2250 + 120 + 400
    leg = s.legs["A"]
    assert leg.first_stated == 2250 and leg.first_all_in == 2770 and leg.first_hidden == 520


def test_padded_quotes_are_never_cited_as_competitor_leverage():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        await e.submit_and_counter(s.session_id, "A", {"price": "2100", "vendor_said": "2100 only"})  # unresolved
        return await e.submit_and_counter(s.session_id, "B", quote(2700))
    b = run(scenario())
    assert b["best_competing_quote_inr"] is None  # A's add-ons are unknown, so citing it would be untruthful


def test_vendor_raising_price_keeps_binding_lower_quote():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        await e.submit_and_counter(s.session_id, "A", quote(2400))
        r = await e.submit_and_counter(s.session_id, "A", quote(2700, "actually 2700"))
        return s, r
    s, r = run(scenario())
    assert s.legs["A"].neg.best_all_in == 2400
    assert "2,400" in r["say_this"]


def test_lock_winner_releases_others_and_late_tool_calls_get_release():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        await e.submit_and_counter(s.session_id, "A", quote(2400))
        await e.submit_and_counter(s.session_id, "B", quote(2500))
        locked = await e.lock_winner(s.session_id, "A")
        late_b = await e.submit_and_counter(s.session_id, "B", quote(2300, "ok 2300 final"))
        status_a = await e.check_auction_status(s.session_id, "A")
        return s, locked, late_b, status_a
    s, locked, late_b, status_a = run(scenario())
    assert locked["released"] == ["B", "C"]
    assert late_b["action"] == "RELEASE" and late_b["end_call"]
    assert status_a["action"] == "WON"
    assert s.legs["B"].status == LegStatus.RELEASED


def test_lock_guardrails():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        with pytest.raises(GuardrailViolation):
            await e.lock_winner(s.session_id, "A")  # no quote yet
        await e.submit_and_counter(s.session_id, "C", quote(3000))  # above ceiling
        with pytest.raises(GuardrailViolation):
            await e.lock_winner(s.session_id, "C")
        await e.submit_and_counter(s.session_id, "A", quote(2400))
        await e.lock_winner(s.session_id, "A")
        with pytest.raises(GuardrailViolation):
            await e.lock_winner(s.session_id, "A")  # double lock
    run(scenario())


def test_late_webhook_does_not_resurrect_finished_leg():
    async def scenario():
        e = make_engine()
        s = await new_session(e)
        await e.submit_and_counter(s.session_id, "A", quote(2200))  # at target -> HOLD
        await e.set_leg_status(s.session_id, "A", LegStatus.ENDED)
        await e.set_leg_status(s.session_id, "A", LegStatus.CONNECTED)
        return s
    s = run(scenario())
    assert s.legs["A"].status == LegStatus.HOLD
