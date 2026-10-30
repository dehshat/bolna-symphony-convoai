"""Advisor agent: when the calls are done, pick the option to recommend and say why, in plain words.

Deterministic and explainable: price (or fee) matters most, then how well-rated and how proven the provider is, then
whether they actually agreed on the call. Only options the user could lock are considered (never above the ceiling).
"""

from __future__ import annotations

from typing import Optional

from app.places import popularity_score
from app.pricing import format_inr

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


def _rating(leg) -> tuple[float, str]:
    p = leg.provider or {}
    if p.get("rating") is None:
        return 0.75, ""
    reviews = int(p.get("reviews") or 0)
    return popularity_score(p["rating"], reviews) / 5, f"rated ★{float(p['rating']):.1f} from {reviews:,} reviews"


def recommend(session) -> Optional[dict]:
    legs = list(session.legs.values())
    if not legs:
        return None
    all_done = all(l.status in TERMINAL or l.status == "HOLD" for l in legs)
    candidates = [(leg, price) for leg in legs if (price := lockable_price(session, leg)) is not None]
    if not candidates:
        if not all_done:
            return None
        return {"leg_id": None, "all_done": True, "reasons": [],
                "summary": "No provider came in within your budget or had a slot. Try raising the max, another time, "
                           "or more providers."}
    appointment = session.request.goal == "appointment"
    cheapest = min(p for _, p in candidates) or 0
    scored = []
    for leg, price in candidates:
        rating, rating_text = _rating(leg)
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
        rating_text = _rating(best)[1]

    reasons = []
    if appointment:
        if best.neg.slot:
            reasons.append(f"slot {best.neg.slot}")
        if price:
            reasons.append(f"fee {format_inr(price)}")
    else:
        reasons.append(f"{format_inr(price)} all-inclusive" + (" (lowest)" if price == cheapest and
                                                                len(candidates) > 1 else ""))
        others = sorted(p for leg, p in candidates if leg is not best)
        if others and others[0] > price:
            reasons.append(f"{format_inr(others[0] - price)} below the next best")
    if rating_text:
        reasons.append(rating_text)
    if best.status in {"HOLD", "WON"}:
        reasons.append("agreed on the call")
    if best.neg.inspection:
        reasons.append("final price after they see the job")
    excluded = [e["item"] for e in best.excluded_items()]
    if excluded:
        reasons.append(f"{', '.join(excluded[:2])} billed separately")
    runner = next(((leg, p) for _, leg, p, _ in scored if leg is not best), None)
    what = (f"{best.neg.slot}" + (f", fee {format_inr(price)}" if price else "")) if appointment \
        else f"{format_inr(price)} all-inclusive"
    return {
        "leg_id": best.leg_id, "vendor_name": best.vendor_name, "price": price or None, "slot": best.neg.slot,
        "rating": (best.provider or {}).get("rating"), "reasons": reasons, "all_done": all_done,
        "confirmed": session.locked_leg_id == best.leg_id,
        "runner_up": {"leg_id": runner[0].leg_id, "vendor_name": runner[0].vendor_name, "price": runner[1]}
        if runner else None,
        "summary": f"{best.vendor_name}: {what}" + (f", {rating_text}" if rating_text else "") + ".",
    }
