"""Advisor agent: when the calls are done, pick the option to recommend and say why, in plain words.

Deterministic and explainable: price (or fee) matters most, then how well-rated and how proven the provider is, then
whether they actually agreed on the call. Only options the user could lock are considered (never above the ceiling).
The summary and the reasons are written in the language the user talks to the app in (`app.userlex`).
"""

from __future__ import annotations

from typing import Optional

from app import lexicon, userlex
from app.places import popularity_score

TERMINAL = {"WON", "RELEASED", "ENDED", "FAILED"}


def lockable_price(session, leg) -> Optional[int]:
    """The price a lock would commit to, or None if this leg can't be locked (same rules as AuctionEngine.lock_winner)."""
    req = session.request
    if leg.status == "FAILED":
        return None
    if req.goal == "appointment":
        return (leg.neg.best_all_in or 0) if leg.neg.slot else None
    price = leg.neg.best_all_in
    if price is None and leg.neg.inspection_fee:
        price = leg.neg.inspection_fee
    if price is None or (req.has_budget and price > req.walkaway_ceiling):
        return None
    return price


def _rating(leg, lang: str = "en") -> tuple[float, str]:
    p = leg.provider or {}
    if p.get("rating") is None:
        return 0.75, ""
    reviews = int(p.get("reviews") or 0)
    return popularity_score(p["rating"], reviews) / 5, userlex.line(lang, "rated", rating=f"{float(p['rating']):.1f}",
                                                                     reviews=f"{reviews:,}")


def _when(slot: str, lang: str) -> str:
    return lexicon.localize_when(slot, lang) if lang != "en" else slot


def user_slot(slot: str, request) -> str:
    """A vendor's slot ('tomorrow 11 AM') as the user's language says it, for narration."""
    return _when(slot, userlex.code(getattr(request, "user_language", "en"))) if slot else ""


def user_items(items: list[str], request) -> list[str]:
    """Charge names ('material / parts') as the user's language says them, for the screen."""
    lang = userlex.code(getattr(request, "user_language", "en"))
    return [_items([item], lang) for item in items]


def _items(items: list[str], lang: str) -> str:
    if lang == "en":
        return ", ".join(items)
    return lexicon.charge_words(" / ".join(items), lang, joiner="and")


def recommend(session) -> Optional[dict]:
    legs = list(session.legs.values())
    if not legs:
        return None
    all_done = all(l.status in TERMINAL or l.status == "HOLD" for l in legs)
    lang = userlex.code(getattr(session.request, "user_language", "en"))
    say = lambda key, **kw: userlex.line(lang, key, **kw)  # noqa: E731
    candidates = [(leg, price) for leg in legs if (price := lockable_price(session, leg)) is not None]
    if not candidates:
        if not all_done:
            return None
        return {"leg_id": None, "all_done": True, "reasons": [], "language": lang, "summary": say("no_fit")}
    appointment = session.request.goal == "appointment"
    cheapest = min(p for _, p in candidates) or 0
    scored = []
    for leg, price in candidates:
        rating, rating_text = _rating(leg, lang)
        price_score = (cheapest / price) if price and cheapest else (0.8 if appointment else 1.0)
        distance = (leg.provider or {}).get("distance_km")
        score = (0.4 if appointment else 0.62) * price_score + (0.5 if appointment else 0.26) * rating
        score += 0.1 if leg.status in {"HOLD", "WON"} else 0
        score -= 0.08 if leg.neg.inspection else 0
        score -= 0.02 * len(leg.excluded_items())
        score -= min(0.05, 0.008 * distance) if distance else 0
        scored.append((round(score, 4), leg, price, rating_text))
    scored.sort(key=lambda x: (-x[0], x[2]))
    _, best, price, rating_text = scored[0]
    if session.locked_leg_id and session.locked_leg_id in session.legs:
        best = session.legs[session.locked_leg_id]
        price = lockable_price(session, best) or best.neg.best_all_in or 0
        rating_text = _rating(best, lang)[1]

    reasons = []
    if appointment:
        if best.neg.slot:
            reasons.append(say("slot_r", slot=_when(best.neg.slot, lang)))
        if price:
            reasons.append(say("fee_r", fee=price))
    else:
        reasons.append(say("lowest" if price == cheapest and len(candidates) > 1 else "all_in", price=price))
        others = sorted(p for leg, p in candidates if leg is not best)
        if others and others[0] > price:
            reasons.append(say("below", amount=others[0] - price))
    if rating_text:
        reasons.append(rating_text)
    if best.status in {"HOLD", "WON"}:
        reasons.append(say("agreed"))
    if best.neg.inspection:
        reasons.append(say("inspection"))
    excluded = [e["item"] for e in best.excluded_items()]
    if excluded:
        reasons.append(say("separately", items=_items(excluded[:2], lang)))
    runner = next(((leg, p) for _, leg, p, _ in scored if leg is not best), None)
    what = (_when(best.neg.slot or "", lang) + (", " + say("fee_r", fee=price) if price else "")).strip(", ") \
        if appointment else say("all_in", price=price)
    return {
        "leg_id": best.leg_id, "vendor_name": best.vendor_name, "price": price or None, "slot": best.neg.slot,
        "rating": (best.provider or {}).get("rating"), "reasons": reasons, "all_done": all_done,
        "confirmed": session.locked_leg_id == best.leg_id,
        "runner_up": {"leg_id": runner[0].leg_id, "vendor_name": runner[0].vendor_name, "price": runner[1]}
        if runner else None,
        "what": what, "language": lang,
        "summary": f"{best.vendor_name}: {what}" + (f", {rating_text}" if rating_text else "") + ".",
    }
