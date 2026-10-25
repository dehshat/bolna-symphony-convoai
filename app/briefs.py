"""Demo presets, buyer-brief parsing fallback, and the anti-hangup opening lines."""

from __future__ import annotations

import re
from typing import Optional

from app import llm
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
                        pickup="JP Nagar, Bengaluru", drop="Kempegowda International Airport", when="Tomorrow 6:00 AM",
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
         "concession": 0.5, "greeting": "Hello, Sri Sai Travels, heli sir."},
        {"name": "Namma Cabs & Tours", "language": "hi", "style": "budget_first", "open_ratio_t": 1.14,
         "floor_ratio_t": 1.04, "concession": 0.6, "greeting": "Haan ji, Namma Cabs, boliye."},
        {"name": "Metro Cab Service", "language": "hi", "style": "sharp", "open_ratio_c": 1.08, "per_km_if_long": True,
         "floor_ratio_t": 1.10, "concession": 0.4, "greeting": "Haan, Metro Cab Service. Kaun bol raha hai?"},
    ],
    "mini_truck": [
        {"name": "Namma Mini Trucks", "language": "hi", "style": "trap", "open_ratio_t": 0.97, "floor_ratio_t": 1.05,
         "concession": 0.5, "greeting": "Haan ji, Namma Mini Trucks."},
        {"name": "Sri Ganesh Packers", "language": "kn", "style": "budget_first", "open_ratio_t": 1.20,
         "floor_ratio_t": 1.03, "concession": 0.6, "greeting": "Hello, Ganesh Packers, heli."},
        {"name": "Quick Shift Tempo", "language": "ta", "style": "sharp", "open_ratio_c": 1.10, "floor_ratio_t": 1.12,
         "concession": 0.4, "greeting": "Hello, Quick Shift, sollunga."},
    ],
    "ac_repair": [
        {"name": "Cool Care AC Service", "language": "kn", "style": "trap", "open_ratio_t": 0.6, "floor_ratio_t": 1.08,
         "concession": 0.5, "greeting": "Hello, Cool Care AC Service."},
        {"name": "Sharma Refrigeration", "language": "hi", "style": "budget_first", "open_ratio_t": 1.25,
         "floor_ratio_t": 1.04, "concession": 0.6, "greeting": "Haan ji, Sharma Refrigeration."},
        {"name": "Arctic Fix Technicians", "language": "en", "style": "sharp", "open_ratio_c": 1.15,
         "floor_ratio_t": 1.12, "concession": 0.4, "greeting": "Hello, Arctic Fix, tell me."},
    ],
}


def request_from_preset(key: str, **overrides) -> ServiceRequest:
    preset = PRESETS.get(key) or PRESETS[DEFAULT_PRESET]
    data = {**preset["request"], **{k: v for k, v in overrides.items() if v not in (None, "")}}
    return ServiceRequest(**data)


WHEN_WORDS = {
    "tomorrow": {"kn": "naale", "hi": "kal", "ta": "naalaikku", "mr": "udya"},
    "today": {"kn": "ivattu", "hi": "aaj", "ta": "inniki", "mr": "aaj"},
    "tonight": {"kn": "ivattu raatri", "hi": "aaj raat", "ta": "inniki raathiri", "mr": "aaj raatri"},
    "asap": {"kn": "bega", "hi": "jaldi", "ta": "seekiram", "mr": "lavkar"},
    "saturday": {"kn": "shanivaara", "hi": "shanivaar", "ta": "sanikizhamai", "mr": "shanivaari"},
    "sunday": {"kn": "bhaanuvaara", "hi": "ravivaar", "ta": "nyaayitrukizhamai", "mr": "ravivaari"},
}


def localize_when(when: str, language: str) -> str:
    """'Tomorrow 6:00 AM' -> 'naale 6 AM' for a Kannada call. Unknown words are kept as spoken English."""
    text = (when or "").strip() or "tomorrow"
    if language == "en":
        return text
    text = re.sub(r"(\d{1,2}):00", r"\1", text)
    out = []
    for word in re.split(r"(\s+|,)", text):
        out.append(WHEN_WORDS.get(word.lower(), {}).get(language, word))
    return "".join(out).replace(",", "").strip()


def opening_line(req: ServiceRequest, language: str) -> str:
    """The first sentence after pickup: the job, the date and the place, no company name, no pitch."""
    pickup = req.pickup.split(",")[0] or "JP Nagar"
    drop = (req.drop.split(",")[0] or "").strip()
    when = localize_when(req.when, language)
    if req.category == "cab_outstation":
        return {
            "kn": f"Namaskara anna, {when} {pickup} inda {drop} ge 7-seater Innova beku, available idya?",
            "hi": f"Haan bhaiya namaste, {when} {pickup} se {drop} ke liye Innova 7-seater chahiye, milegi?",
            "ta": f"Vanakkam anna, {when} {pickup} la irundhu {drop} ku Innova 7-seater venum, kidaikuma?",
            "mr": f"Namaskar dada, {when} {pickup} pasun {drop} la Innova 7-seater pahije, milel ka?",
        }.get(language, f"Hello sir, I need an Innova 7-seater {when} from {pickup} to {drop}. Is one available?")
    if req.category == "mini_truck":
        return {
            "kn": f"Namaskara anna, {when} {pickup} inda {drop} ge 1BHK shifting ide, chikka tempo sigutta?",
            "hi": f"Haan bhaiya namaste, {when} {pickup} se {drop} 1BHK shifting hai, chhota tempo milega?",
            "ta": f"Vanakkam anna, {when} {pickup} la irundhu {drop} ku 1BHK shifting, chinna tempo kidaikuma?",
        }.get(language, f"Hello, I have a 1BHK shifting {when} from {pickup} to {drop}. Is a mini tempo available?")
    return {
        "kn": f"Namaskara, {pickup} nalli split AC cooling aagtilla, {when.lower()} technician bartara?",
        "hi": f"Namaste ji, {pickup} mein split AC cooling nahi kar raha, {when.lower()} koi aa sakta hai?",
        "ta": f"Vanakkam, {pickup} la split AC cooling aagala, {when.lower()} technician vara mudiyuma?",
    }.get(language, f"Hello, my split AC in {pickup} is not cooling. Can a technician come {when.lower()}?")


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
