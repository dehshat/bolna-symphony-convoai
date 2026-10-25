import pytest

from app.pricing import ServiceRequest, format_inr, merge_charges, normalize_quote, parse_charge, parse_inr


@pytest.mark.parametrize("spoken, expected", [
    ("₹2,500", 2500), ("Rs. 2500/-", 2500), ("2.5k", 2500), ("2500 rupees only", 2500),
    ("pacchees sau", 2500), ("do hazaar paanch sau", 2500), ("dhai hazaar", 2500),
    ("saade teen hazaar", 3500), ("sava do hazaar", 2250), ("paune teen hazaar", 2750),
    ("eradu saavira aidu nooru", 2500), ("rendu aayiram ainooru", 2500), ("don hajar pachshe", 2500),
    ("2 hazaar 500", 2500), ("can you do 2200 only sir", 2200), ("2200 plus 400 bata", 2200),
    ("1,20,000", 120000), (2400, 2400), ("unknown", None), ("", None), (None, None),
])
def test_parse_inr_multilingual(spoken, expected):
    assert parse_inr(spoken) == expected


@pytest.mark.parametrize("value, status, amount", [
    ("included", "included", None), ("sab milake", "included", None), ("koi extra nahi", "included", None),
    ("extra 400", "extra", 400), ("400", "extra", 400), ("toll alag", "extra", None),
    ("not included", "extra", None), ("unknown", "unknown", None), ("na", "na", None),
])
def test_parse_charge(value, status, amount):
    item = parse_charge(value)
    assert (item.status, item.amount) == (status, amount)


def cab(**kw):
    base = dict(category="cab_outstation", target_price=11000, walkaway_ceiling=13000, est_km=560, days=2)
    base.update(kw)
    return ServiceRequest(**base)


def test_hidden_charges_are_added_to_all_in():
    q = normalize_quote(cab(), 11200, "flat", {"toll": "extra 650", "driver_bata": "extra 400"})
    # bata is per day on a 2-day trip
    assert q.status == "VALID"
    assert q.all_in == 11200 + 650 + 800
    assert q.hidden_charges == 1450


def test_unknown_addons_are_padded_and_flagged():
    q = normalize_quote(cab(), 11200, "flat", {})
    assert q.status == "NEEDS_INCLUSIONS"
    assert set(q.unknown_items) == {"toll", "driver_bata"}
    assert q.all_in > 11200  # conservative padding, never an optimistic number


def test_per_km_is_normalized_with_minimum_billing():
    req = cab(est_km=300, days=2)  # 2 days x 250 km minimum beats the 300 km estimate
    q = normalize_quote(req, "22", "per_km", {"toll": "included", "driver_bata": "included"})
    assert q.base_price == 22 * 500
    assert q.all_in == 11000


def test_pricing_model_detected_from_speech():
    q = normalize_quote(cab(), "22", "unknown", {"toll": "included", "driver_bata": "included"},
                        said="22 rupaye per km lagega")
    assert q.pricing_model == "per_km"


@pytest.mark.parametrize("price, model, reason", [
    ("25", "flat", "price_too_low_maybe_unit_rate"),        # "25" was really ₹25/km
    ("2500", "per_km", "per_km_rate_too_high"),             # ₹2,500 per km is a total, not a rate
    ("250000", "flat", "price_too_high_maybe_misheard"),
])
def test_implausible_quotes_require_clarification(price, model, reason):
    q = normalize_quote(cab(), price, model, {})
    assert q.status == "CLARIFY" and q.clarify_reason == reason


def test_merge_charges_never_erases_known_info_with_unknown():
    merged = merge_charges({"toll": "extra 300"}, {"toll": "unknown", "driver_bata": "included"})
    assert merged["toll"].amount == 300 and merged["driver_bata"].status == "included"


def test_ceiling_below_target_rejected():
    with pytest.raises(ValueError):
        ServiceRequest(target_price=3000, walkaway_ceiling=2500)


def test_indian_grouping():
    assert format_inr(120000) == "₹1,20,000" and format_inr(2500) == "₹2,500"
