"""Smarter calls: extras billed separately stay out of the budget, everything said is captured, little haggling."""

import asyncio

from app import payments
from app.auction_engine import AuctionEngine, Store
from app.negotiation import LegNegotiationState, decide
from app.pricing import ServiceRequest, normalize_quote, parse_extras


def plumber(**kw):
    base = dict(category="plumber", target_price=500, walkaway_ceiling=800)
    base.update(kw)
    return ServiceRequest(**base)


# ---------------------------------------------------------------- extras: excluded vs add-on

def test_parts_billed_separately_are_excluded_not_added_to_the_price():
    q = normalize_quote(plumber(), 600, "flat", {"other_charges": "parts extra at actuals"})
    assert q.all_in == 600 and q.status == "VALID"
    assert q.excluded == [{"key": "other_charges", "label": "material / parts", "amount": None}]


def test_job_addons_still_count_towards_the_real_price():
    q = normalize_quote(plumber(), 600, "flat", {"other_charges": "parts extra", "driver_bata": "visiting 200 extra"})
    assert q.all_in == 800 and q.hidden_charges == 200
    q = normalize_quote(plumber(), 600, "flat", {"other_charges": "labour 300 extra"})
    assert q.all_in == 900 and not q.excluded  # labour is part of the job even in a "parts" slot


def test_ac_gas_refill_is_excluded_but_visiting_charge_counts():
    r = ServiceRequest(category="ac_repair", target_price=600, walkaway_ceiling=900)
    q = normalize_quote(r, 600, "flat", {"other_charges": "gas refill 1500 extra", "driver_bata": "299 extra"})
    assert q.all_in == 899 and q.excluded[0]["amount"] == 1500


def test_unknown_parts_are_asked_about_once_but_never_padded():
    r, s = plumber(), LegNegotiationState()
    q = normalize_quote(r, 600, "flat", {})
    assert q.all_in == 600 and q.ask_items == ["other_charges"] and q.status == "NEEDS_INCLUSIONS"
    first = decide(r, s, q, None, "en")
    assert first.action == "ASK_INCLUSIONS" and "material / parts" in first.say
    second = decide(r, s, q, None, "en")
    assert second.action != "ASK_INCLUSIONS"


def test_parse_extras_itemizes_everything_said():
    items = parse_extras("visiting 200 extra; parts at actuals, GST 18% extra; travel included")
    by = {i["item"]: i for i in items}
    assert by["visiting"]["amount"] == 200 and by["visiting"]["kind"] == "addon"
    assert by["parts"]["kind"] == "excluded" and by["parts"]["status"] == "extra"
    assert by["GST 18%"]["amount"] is None  # a percentage, not ₹18
    assert by["travel"]["status"] == "included"
    assert parse_extras("none") == []


# ---------------------------------------------------------------- haggling

def step(r, s, price, charges=None):
    q = normalize_quote(r, price, "flat", charges or {"other_charges": "included"})
    if s.best_all_in is None or q.all_in < s.best_all_in:
        s.best_all_in = q.all_in
    return decide(r, s, q, None, "en")


def test_one_counter_then_takes_the_vendors_best_within_budget():
    r, s = plumber(), LegNegotiationState()
    assert step(r, s, 750).action == "COUNTER"
    d = step(r, s, 700)
    assert d.action == "FINAL_HOLD" and d.binding_price == 700 and d.end_call


def test_one_counter_then_walks_away_above_the_ceiling():
    r, s = plumber(), LegNegotiationState()
    assert step(r, s, 1000).action == "COUNTER"
    assert step(r, s, 950).action == "WALK_AWAY"


def test_small_change_above_target_is_accepted_without_haggling():
    r = ServiceRequest(category="home_cook", target_price=10000, walkaway_ceiling=13000)
    d = step(r, LegNegotiationState(), 10400)
    assert d.action == "ACCEPT_HOLD" and d.reason == "close_enough_to_target"


def test_closing_line_repeats_what_is_billed_separately():
    r, s = plumber(), LegNegotiationState()
    q = normalize_quote(r, 500, "flat", {"other_charges": "parts extra"})
    s.best_all_in = q.all_in
    d = decide(r, s, q, None, "en", excluded=["parts"])
    assert d.action == "ACCEPT_HOLD" and "parts separately" in d.say


# ---------------------------------------------------------------- capture, end to end through the engine

def test_engine_captures_extras_and_details_and_keeps_parts_out_of_the_price(tmp_path):
    async def scenario():
        e = AuctionEngine(Store(str(tmp_path / "t.db")))
        s = await e.create_session(plumber(), [{"name": "Lakshmi Plumbing", "language": "en"}])
        resp = await e.submit_and_counter(s.session_id, "A", {
            "vendor_said": "Visiting charge is 200 extra, labour 400, parts you pay at actuals. Can come by 6 pm.",
            "price": "400", "pricing_model": "flat", "toll": "na", "driver_bata": "visiting 200 extra",
            "other_charges": "parts extra at actuals",
            "extras": "visiting 200 extra; parts at actuals", "notes": "Can come today by 6 pm; 3 month warranty",
            "availability": "yes", "slot": "unknown"})
        return s, resp
    s, resp = asyncio.run(scenario())
    leg = s.legs["A"]
    assert leg.neg.best_all_in == 600                      # 400 labour + 200 visiting; parts are not in the price
    assert [e["item"] for e in leg.excluded_items()] == ["parts"]
    assert leg.details == ["Can come today by 6 pm; 3 month warranty"]
    assert "parts" in resp["excluded_items"] and "Billed separately" in resp["heard"]
    pub = leg.public()
    assert pub["excluded"] and pub["details"] and pub["extras"]
    assert "6 pm" in pub["transcript"][0]["text"]


def test_whatsapp_summary_lists_what_is_not_included():
    text = payments.deal_summary({"service_label": "plumber"}, "Lakshmi Plumbing", 600, None, None,
                                 excluded=[{"item": "parts", "amount": None}], details=["3 month warranty"])
    assert "Not included, pay separately (outside your budget): parts (at actuals)" in text
    assert "3 month warranty" in text
