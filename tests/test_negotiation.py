import random

import pytest

from app.negotiation import Competitor, LegNegotiationState, counter_for_round, decide
from app.pricing import ServiceRequest, normalize_quote

ALL_IN = {"toll": "included", "driver_bata": "included"}


def req(**kw):
    base = dict(category="cab_outstation", target_price=2200, walkaway_ceiling=2600, est_km=45, min_km_per_day=0)
    base.update(kw)
    return ServiceRequest(**base)


def quote(r, price, charges=ALL_IN):
    return normalize_quote(r, price, "flat", charges)


def step(r, state, price, competitor=None, **kw):
    q = quote(r, price)
    if state.best_all_in is None or q.all_in < state.best_all_in:
        state.best_all_in = q.all_in
    return decide(r, state, q, competitor, "en", **kw)


def test_spec_example_competitor_2500_gives_counter_2250():
    """Vendor A quotes ₹2,500 → Vendor B (quoting ₹2,700) is told about ₹2,500 and asked to match ₹2,250."""
    r = req()
    d = step(r, LegNegotiationState(), 2700, Competitor("A", "Sri Sai Travels", 2500))
    assert d.action == "COUNTER"
    assert d.counter_price == 2250
    assert d.leverage["price"] == 2500 and "2,500" in d.say and "2,250" in d.say


def test_no_anchor_never_reveals_budget_first_and_never_invents_competitor():
    r, s = req(), LegNegotiationState()
    first = decide(r, s, None, None, "hi")
    second = decide(r, s, None, None, "hi")
    third = decide(r, s, None, None, "hi")
    assert first.action == "ASK_VENDOR_FIRST" and "2,200" not in first.say
    assert second.action == "STALL_FOR_ANCHOR" and "2,200" not in second.say
    assert third.action == "COUNTER" and third.leverage is None  # budget framing only as a last resort
    assert all("operator" not in d.say for d in (first, second, third))


def test_competitor_available_before_own_quote_asks_to_beat():
    d = decide(req(), LegNegotiationState(), None, Competitor("A", "X", 2450), "en")
    assert d.action == "ASK_TO_BEAT" and "2,450" in d.say


def test_hidden_addons_trigger_one_inclusion_question():
    r, s = req(), LegNegotiationState()
    q = normalize_quote(r, 2250, "flat", {})
    d = decide(r, s, q, None, "en")
    assert d.action == "ASK_INCLUSIONS" and "toll" in d.say
    s.best_all_in = q.all_in
    assert decide(r, s, q, None, "en").action != "ASK_INCLUSIONS"  # asked once, then padded


def test_vendor_meeting_counter_is_accepted_on_soft_hold():
    r, s = req(), LegNegotiationState()
    d = step(r, s, 2700, Competitor("A", "X", 2500))
    d2 = step(r, s, d.counter_price)
    assert d2.action == "ACCEPT_HOLD" and d2.binding_price == d.counter_price
    assert "booked" not in d2.say.lower() and d2.end_call


def test_walks_away_after_max_rounds_above_ceiling():
    r, s = req(), LegNegotiationState()
    last = None
    for _ in range(5):
        last = step(r, s, 3000)
        if last.end_call:
            break
    assert last.action == "WALK_AWAY" and last.binding_price is None


def test_leverage_counter_never_exceeds_competitor_quote():
    r, s = req(), LegNegotiationState()
    comp = Competitor("B", "Y", 2300)
    for price in (2800, 2700, 2600):
        d = step(r, s, price, comp)
        if d.action == "COUNTER" and d.leverage:
            assert d.counter_price <= comp.price


def test_locked_session_releases_other_legs():
    s = LegNegotiationState(best_all_in=2400)
    assert decide(req(), s, None, None, "kn", locked_leg_id="A", leg_id="B").action == "RELEASE"
    assert decide(req(), s, None, None, "kn", locked_leg_id="B", leg_id="B").action == "WON"


def test_raised_price_gets_reminder_of_binding_quote():
    r, s = req(), LegNegotiationState(best_all_in=2400, last_counter=2250, rounds=1)
    d = decide(r, s, quote(r, 2600), None, "en", previous_best=2400, raised_price=True)
    assert "2,400 earlier" in d.say


def test_not_available_ends_call():
    d = decide(req(), LegNegotiationState(), None, None, "hi", available=False)
    assert d.action == "NOT_AVAILABLE" and d.end_call


@pytest.mark.parametrize("seed", range(200))
def test_property_never_agrees_or_counters_above_ceiling(seed):
    """Randomized vendors (incl. absurd and adversarial prices): no spoken counter or accepted price may exceed
    the walk-away ceiling, and no leverage line may cite the leg's own quote."""
    rng = random.Random(seed)
    target = rng.randrange(800, 15000, 50)
    r = req(target_price=target, walkaway_ceiling=target + rng.randrange(0, 4000, 50))
    s = LegNegotiationState()
    for _ in range(6):
        competitor = Competitor("Z", "Other", rng.randrange(int(target * 0.8), target * 2, 50)) \
            if rng.random() < 0.6 else None
        d = step(r, s, rng.randrange(int(target * 0.7), target * 3, 50), competitor)
        if d.counter_price is not None:
            assert d.counter_price <= r.walkaway_ceiling
        if d.binding_price is not None:
            assert d.binding_price <= r.walkaway_ceiling
        if d.leverage:
            assert d.leverage["leg_id"] == "Z"
        if d.end_call:
            break


def test_counter_curve_concedes_gradually():
    r = req()
    assert counter_for_round(r, 2600, 0, None) < counter_for_round(r, 2600, 1, None) < counter_for_round(r, 2600, 2, None)
