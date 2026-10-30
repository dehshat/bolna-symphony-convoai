"""Demo presets, buyer-brief parsing fallback, and the anti-hangup opening lines."""

from __future__ import annotations

import re
from typing import Optional

from app import lexicon, llm
from app.pricing import CATEGORIES, ServiceRequest, parse_inr

PRESETS: dict[str, dict] = {
    "coorg_suv": {
        "title": "Bengaluru → Coorg 7-seater SUV",
        "brief": "Need an Innova 7-seater tomorrow 6 AM from JP Nagar to Coorg, 2 day round trip. "
                 "Target ₹11,000, hard max ₹13,000.",
        "request": dict(category="cab_outstation", summary="Innova 7-seater, JP Nagar → Coorg, 2-day round trip",
                        pickup="JP Nagar, Bengaluru", drop="Madikeri, Coorg", when="Tomorrow 6:00 AM",
                        target_price=11000, walkaway_ceiling=13000, est_km=560, days=2, language_hint="kn"),
    },
    "airport_innova": {
        "title": "JP Nagar → Airport Innova (6 AM)",
        "brief": "Need an Innova 7-seater tomorrow 6 AM from JP Nagar to Kempegowda airport. "
                 "Target ₹2,200, hard max ₹2,600.",
        "request": dict(category="cab_outstation", summary="Innova 7-seater, JP Nagar → KIA airport drop",
                        pickup="JP Nagar, Bengaluru", drop="Kempegowda Airport", when="Tomorrow 6:00 AM",
                        target_price=2200, walkaway_ceiling=2600, est_km=45, days=1, min_km_per_day=0,
                        language_hint="kn"),
    },
    "tempo_shift": {
        "title": "JP Nagar 1BHK mini-tempo shift",
        "brief": "1BHK shifting from JP Nagar to HSR Layout this Saturday morning, need a Tata Ace mini tempo "
                 "with 2 helpers. Target ₹3,500, max ₹4,500.",
        "request": dict(category="mini_truck", summary="1BHK shift, Tata Ace + 2 helpers, JP Nagar → HSR Layout",
                        pickup="JP Nagar 2nd Phase", drop="HSR Layout Sector 2", when="Saturday 8:00 AM",
                        target_price=3500, walkaway_ceiling=4500, est_km=12, days=1, est_hours=4,
                        min_km_per_day=0, language_hint="hi"),
    },
    "ac_repair": {
        "title": "Urgent split-AC repair",
        "brief": "My 1.5 ton split AC is not cooling, need a technician today in JP Nagar. Target ₹1,200, max ₹1,800.",
        "request": dict(category="ac_repair", summary="1.5T split AC not cooling, JP Nagar, today",
                        pickup="JP Nagar 6th Phase", drop="", when="Today, ASAP", target_price=1200,
                        walkaway_ceiling=1800, est_km=0, days=1, min_km_per_day=0, language_hint="kn"),
    },
}

DEFAULT_PRESET = "coorg_suv"

# Simulated vendors per category. Ratios are relative to the buyer's target (T) / ceiling (C) so the
# same personas work for every preset.
#   open_all_in: what the vendor really wants at first (all-in)    floor: the lowest all-in they accept
#   trap: add-ons hidden in the first quote and revealed only when asked
VENDOR_PERSONAS: dict[str, list[dict]] = {
    "cab_outstation": [
        {"name": "Sri Sai Travels", "language": "kn", "style": "trap", "open_ratio_t": 1.02, "floor_ratio_t": 1.06,
         "concession": 0.5},
        {"name": "Namma Cabs & Tours", "language": "hi", "style": "budget_first", "open_ratio_t": 1.14,
         "floor_ratio_t": 1.04, "concession": 0.6},
        {"name": "Metro Cab Service", "language": "hi", "style": "sharp", "open_ratio_c": 1.08, "per_km_if_long": True,
         "floor_ratio_t": 1.10, "concession": 0.4},
    ],
    "mini_truck": [
        {"name": "Namma Mini Trucks", "language": "hi", "style": "trap", "open_ratio_t": 0.97, "floor_ratio_t": 1.05,
         "concession": 0.5},
        {"name": "Sri Ganesh Packers", "language": "kn", "style": "budget_first", "open_ratio_t": 1.20,
         "floor_ratio_t": 1.03, "concession": 0.6},
        {"name": "Quick Shift Tempo", "language": "ta", "style": "sharp", "open_ratio_c": 1.10, "floor_ratio_t": 1.12,
         "concession": 0.4},
    ],
    "ac_repair": [
        {"name": "Cool Care AC Service", "language": "kn", "style": "trap", "open_ratio_t": 0.6, "floor_ratio_t": 1.08,
         "concession": 0.5},
        {"name": "Sharma Refrigeration", "language": "hi", "style": "budget_first", "open_ratio_t": 1.25,
         "floor_ratio_t": 1.04, "concession": 0.6},
        {"name": "Arctic Fix Technicians", "language": "en", "style": "sharp", "open_ratio_c": 1.15,
         "floor_ratio_t": 1.12, "concession": 0.4},
    ],
}


def request_from_preset(key: str, **overrides) -> ServiceRequest:
    preset = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
    data = {**preset["request"], **{k: v for k, v in overrides.items() if v not in (None, "")}}
    return ServiceRequest(**data)


def localize_when(when: str, language: str) -> str:
    """'Tomorrow 6:00 AM' -> 'ನಾಳೆ ಬೆಳಿಗ್ಗೆ 6 ಗಂಟೆಗೆ' for a Kannada call; unchanged for English."""
    return lexicon.localize_when(when or "tomorrow", language)


native_when = localize_when


def spoken(label: str, language: str) -> str:
    """'AC repair / service' -> 'AC repair or service'; in other languages the native words for those charges."""
    return lexicon.charge_words(label, language)


def _short(text: str, limit: int = 70) -> str:
    text = re.sub(r"\s+", " ", text or "").strip(" .")
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0]


def short_name(name: str) -> str:
    """'Sri Sai Travels, JP Nagar' -> 'Sri Sai Travels' (how a shop answers its phone)."""
    return (name or "").split(",")[0].strip()[:40]


def _first(place: str) -> str:
    return (place or "").split(",")[0].strip()


def opening_line(req: ServiceRequest, language: str, native: bool = True, local: Optional[dict] = None) -> str:
    """The first sentence after pickup: the job, the date and the place, no company name, no pitch.

    Always in the call's own script (Sarvam pronounces it far better, and the call never switches to English).
    `local` holds Gemini's translation of the user's own words (service, details, place…) into that language; without
    it the service gets its catalogue name and free-text details are left for the voice agent to explain."""
    local = local or {}
    if language in PRESET_OPENINGS and not local and req.category in PRESET_OPENINGS[language] and \
            any(req.summary == p["request"]["summary"] for p in PRESETS.values()):
        return PRESET_OPENINGS[language][req.category].format(when=localize_when(req.when, language),
                                                              pickup=_first(req.pickup), drop=_first(req.drop))
    table = lexicon.OPENINGS.get(language) or lexicon.OPENINGS["en"]
    if language in lexicon.NATIVE:
        service = local.get("service") or lexicon.service_name(req.category, req.service_label, language)
        detail = local.get("details", "")
        when = local.get("when") or localize_when(req.when, language)
    else:
        service = lexicon.spoken_en(req.service_label or "service")
        detail = _short(req.details)
        when = req.when or "soon"
    place = local.get("place") or _first(req.location or req.pickup) or "Bengaluru"
    pickup, drop = local.get("pickup") or _first(req.pickup), local.get("drop") or _first(req.drop)
    fields = dict(service=service, when=when, place=place, pickup=pickup, drop=drop,
                  detail=f", {detail}" if detail else "")
    if pickup and drop:
        return table["route"].format(**fields)
    if req.goal == "appointment":
        return table["appointment"].format(**fields)
    return table["generic"].format(**fields)


def opening_gloss(req: ServiceRequest) -> str:
    """English meaning of the opening line, for the screen."""
    return opening_line(req, "en")


# The demo presets keep their richer openings (vehicle model, 1BHK, split AC) in Hindi and Kannada
PRESET_OPENINGS = {
    "hi": {"cab_outstation": "नमस्ते भैया, {when} {pickup} से {drop} के लिए इनोवा सात सीटर गाड़ी चाहिए, मिल जाएगी?",
           "mini_truck": "नमस्ते भैया, {when} {pickup} से {drop} एक कमरे का सामान शिफ़्ट करना है, छोटा टेम्पो मिलेगा?",
           "ac_repair": "नमस्ते जी, {pickup} में एसी ठंडा नहीं कर रहा, {when} कोई मिस्त्री आ सकता है?"},
    "kn": {"cab_outstation": "ನಮಸ್ಕಾರ ಅಣ್ಣ, {when} {pickup} ಇಂದ {drop} ಗೆ ಇನ್ನೋವಾ ಏಳು ಸೀಟಿನ ಗಾಡಿ ಬೇಕಿತ್ತು, ಸಿಗುತ್ತಾ?",
           "mini_truck": "ನಮಸ್ಕಾರ ಅಣ್ಣ, {when} {pickup} ಇಂದ {drop} ಗೆ ಮನೆ ಸಾಮಾನು ಸಾಗಿಸಬೇಕು, ಚಿಕ್ಕ ಟೆಂಪೋ ಸಿಗುತ್ತಾ?",
           "ac_repair": "ನಮಸ್ಕಾರ, {pickup} ಅಲ್ಲಿ ಎಸಿ ತಣ್ಣಗೆ ಮಾಡ್ತಿಲ್ಲ, {when} ಯಾರಾದ್ರೂ ಬರ್ತಾರಾ?"},
}


def personas_for_providers(req: ServiceRequest, providers: list[dict], language: str) -> list[dict]:
    """Simulated behaviour for real map listings: each provider gets a different negotiation style."""
    out = []
    for i, p in enumerate(providers):
        name = str(p.get("name") or f"Provider {i + 1}")[:60]
        persona = {**GENERIC_STYLES[i % len(GENERIC_STYLES)], "name": name, "language": language}
        if req.category == "cab_outstation" and i == 2:
            persona["per_km_if_long"] = True
        out.append(persona)
    return out


GENERIC_STYLES = (
    {"style": "trap", "open_ratio_t": 1.0, "floor_ratio_t": 1.05, "concession": 0.5},
    {"style": "budget_first", "open_ratio_t": 1.15, "floor_ratio_t": 1.03, "concession": 0.6},
    {"style": "sharp", "open_ratio_c": 1.1, "floor_ratio_t": 1.1, "concession": 0.4},
    {"style": "budget_first", "open_ratio_t": 1.25, "floor_ratio_t": 1.08, "concession": 0.5},
    {"style": "trap", "open_ratio_t": 0.95, "floor_ratio_t": 1.12, "concession": 0.45},
)


def _fallback_parse(text: str) -> dict:
    t = text.lower()
    if re.search(r"\b(ac|air ?condition|cooling|technician|repair|gas)\b", t):
        category = "ac_repair"
    elif re.search(r"(tempo|truck|shift|packers|movers|bhk|ace)", t):
        category = "mini_truck"
    else:
        category = "cab_outstation"
    out: dict = {"category": category, "summary": text.strip()[:90]}
    m = re.search(r"(target|budget|around|approx)[^\d₹]{0,12}(₹?\s?[\d,\.]+\s?k?)", t)
    if m:
        out["target_price"] = parse_inr(m.group(2))
    m = re.search(r"(max|maximum|ceiling|walk.?away|not more than|upto|up to)[^\d₹]{0,12}(₹?\s?[\d,\.]+\s?k?)", t)
    if m:
        out["walkaway_ceiling"] = parse_inr(m.group(2))
    m = re.search(r"from ([a-z ]+?) to ([a-z ]+?)(?:[,.]| for| tomorrow| today| on |$)", t)
    if m:
        out["pickup"], out["drop"] = m.group(1).strip().title(), m.group(2).strip().title()
    m = re.search(r"(tomorrow|today|tonight|this \w+|saturday|sunday|monday|tuesday|wednesday|thursday|friday)"
                  r"[^,.]{0,12}?(\d{1,2}(:\d{2})?\s?(am|pm))?", t)
    if m:
        out["when"] = m.group(0).strip().capitalize()
    m = re.search(r"(\d+)\s*(day|din)", t)
    if m:
        out["days"] = int(m.group(1))
    return out


async def parse_brief(text: str) -> dict:
    """Free text / speech → request fields. Gemini when available, regex otherwise. Never invents prices."""
    data = await llm.parse_brief(text) or {}
    fallback = _fallback_parse(text)
    merged = {**fallback, **{k: v for k, v in data.items() if v not in (None, "", 0)}}
    if merged.get("category") not in CATEGORIES:
        merged["category"] = fallback["category"]
    base = next(p["request"] for p in PRESETS.values() if p["request"]["category"] == merged["category"])
    result = {**base, "summary": text.strip()[:120], "pickup": "", "drop": "", "when": ""}
    for key in ("summary", "pickup", "drop", "when", "language_hint"):
        if merged.get(key):
            result[key] = str(merged[key])
    for key in ("target_price", "walkaway_ceiling", "days"):
        value = parse_inr(merged.get(key)) if key != "days" else merged.get(key)
        if value:
            result[key] = int(value)
    try:
        km = float(merged.get("est_km") or 0)
        if km > 0:
            result["est_km"] = km
    except (TypeError, ValueError):
        pass
    if result["walkaway_ceiling"] < result["target_price"]:
        result["walkaway_ceiling"] = int(result["target_price"] * 1.2)
    result["parsed_by"] = "gemini" if data else "rules"
    return result


def vendors_for(category: str) -> list[dict]:
    return [dict(p) for p in VENDOR_PERSONAS.get(category, VENDOR_PERSONAS["cab_outstation"])]


def persona_by_name(category: str, name: str) -> Optional[dict]:
    return next((p for p in vendors_for(category) if p["name"] == name), None)
