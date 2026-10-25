"""Chat intake: talk to the user until we know exactly what to arrange.

Each user message is understood by Gemini when it's available (one request per message), with a rule-based
extractor as the always-on fallback. The questions themselves are deterministic, so the conversation is
predictable even when the LLM quota is used up.

Order of questions: service → where → when → details → budget (negotiated services only) → ready.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app import llm, services
from app.pricing import ServiceRequest, format_inr, parse_inr

BENGALURU_AREAS = (
    "JP Nagar", "Jayanagar", "Koramangala", "HSR Layout", "Indiranagar", "Whitefield", "BTM Layout", "Marathahalli",
    "Electronic City", "Banashankari", "Malleshwaram", "Hebbal", "Yelahanka", "Bellandur", "Sarjapur", "Rajajinagar",
    "Basavanagudi", "MG Road", "Bannerghatta Road", "Kalyan Nagar", "HBR Layout", "Banaswadi", "RT Nagar",
    "Vijayanagar", "Kengeri", "Hennur", "Domlur", "Ulsoor", "Frazer Town", "Sadashivanagar", "Yeshwanthpur",
    "Peenya", "Mahadevapura", "KR Puram", "Brookefield", "Bommanahalli", "Hosur Road", "Sahakar Nagar",
    "Kammanahalli", "Basaveshwaranagar", "Wilson Garden", "Richmond Town", "Shivajinagar", "Majestic",
    "Kempegowda Airport", "Airport",
)
CITIES = ("Bengaluru", "Bangalore", "Mumbai", "Delhi", "Pune", "Hyderabad", "Chennai", "Kolkata", "Mysuru", "Mysore",
          "Gurgaon", "Gurugram", "Noida", "Ahmedabad", "Jaipur", "Kochi", "Coorg", "Madikeri")

WHEN_RE = re.compile(
    r"\b(right now|now|asap|urgent(?:ly)?|today|tonight|tomorrow|day after tomorrow|this (?:week(?:end)?|evening|"
    r"morning|afternoon|saturday|sunday|monday|tuesday|wednesday|thursday|friday)|next (?:week|month|monday|"
    r"tuesday|wednesday|thursday|friday|saturday|sunday)|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"aaj|kal|parso|ivattu|naale)\b(?:[^.,;]{0,25}?\b\d{1,2}(?::\d{2})?\s*(?:am|pm|baje)\b)?"
    r"|\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|"
    r"nov|dec)\w*", re.I)
NOT_SURE_RE = re.compile(r"\b(no idea|not sure|don'?t know|dont know|suggest|any|whatever|pata nahi|gottilla)\b", re.I)
SKIP_RE = re.compile(r"^\s*(no|nope|nothing|none|skip|na|that'?s it|nahi|illa)\s*[.!]?\s*$", re.I)
BUDGET_HINT_RE = re.compile(r"(₹|rs\.?|rupees?|budget|max|maximum|under|upto|up to|within|around|approx|k\b|\d{3,})",
                            re.I)

FIELDS = ("service", "location", "when", "details", "budget")


@dataclass
class Intake:
    id: str
    messages: list = field(default_factory=list)
    task: dict = field(default_factory=dict)
    asked: list = field(default_factory=list)
    last_question: str = ""

    def public(self) -> dict:
        missing = missing_fields(self.task, self.asked)
        return {"conversation_id": self.id, "messages": self.messages[-30:], "task": self.task,
                "ready": not missing, "missing": missing}


CONVERSATIONS: dict[str, Intake] = {}


# ---------------------------------------------------------------------------- extraction

def _find_location(text: str) -> str:
    for area in sorted(BENGALURU_AREAS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(area)}\b", text, re.I):
            city = next((c for c in CITIES if re.search(rf"\b{c}\b", text, re.I)), "Bengaluru")
            return f"{area}, {city}"
    m = re.search(r"\b(?:in|at|near|around|nearby)\s+([A-Za-z][\w .'-]{2,40}?)(?=[,.!?]|\s+(?:for|by|on|at|tomorrow|"
                  r"today|tonight|this|next|around|under|with|budget)\b|$)", text, re.I)
    if m and not WHEN_RE.fullmatch(m.group(1).strip()):
        return _proper(m.group(1).strip())
    city = next((c for c in CITIES if re.search(rf"\b{c}\b", text, re.I)), "")
    return city


def _proper(place: str) -> str:
    """Known area names keep their real spelling ('JP Nagar', not 'Jp Nagar')."""
    for area in BENGALURU_AREAS + CITIES:
        if place.lower() == area.lower():
            return area
    return place.title()


def _find_route(text: str) -> tuple[str, str]:
    m = re.search(r"\bfrom\s+([A-Za-z][\w .'-]{2,40}?)\s+to\s+([A-Za-z][\w .'-]{2,40}?)(?=[,.!?]|\s+(?:on|at|for|"
                  r"tomorrow|today|tonight|this|next|by|around|under|budget)\b|$)", text, re.I)
    return (_proper(m.group(1).strip()), _proper(m.group(2).strip())) if m else ("", "")


def _find_budget(text: str) -> tuple[int, int]:
    if not BUDGET_HINT_RE.search(text):
        return 0, 0
    amounts = []
    for chunk in re.findall(r"(?:₹|rs\.?\s*)?\d[\d,]*(?:\.\d+)?\s*(?:k|lakh|thousand|hazaar)?", text, re.I):
        value = parse_inr(chunk)
        if value and value >= 100:
            amounts.append(value)
    if not amounts:
        return 0, 0
    max_m = re.search(r"(max|maximum|not more than|upto|up to|under|within|hard max|ceiling)[^\d₹]{0,12}(₹?\s?[\d,.]+\s?k?)",
                      text, re.I)
    ceiling = parse_inr(max_m.group(2)) if max_m else 0
    if len(amounts) >= 2:
        low, high = min(amounts), max(amounts)
        return low, ceiling or high
    only = amounts[0]
    if ceiling:
        return int(round(only * 0.85 / 50) * 50), only
    return only, int(round(only * 1.2 / 50) * 50)


FILLER_RE = re.compile(r"\b(i|we|need|want|would like|looking for|please|book|an?|the|some(one|body)|get|find|"
                       r"appointment|in|at|near|for|me|my area|to|urgently|asap)\b", re.I)


def _residual(text: str, svc) -> str:
    """What's left of a message after removing the service words, place, time and filler: the actual details."""
    rest = text
    for kw in sorted(svc.keywords, key=len, reverse=True):
        rest = re.sub(re.escape(kw.strip()), " ", rest, flags=re.I)
    loc = _find_location(text)
    if loc:
        rest = re.sub(re.escape(loc.split(",")[0]), " ", rest, flags=re.I)
    rest = WHEN_RE.sub(" ", rest)
    rest = FILLER_RE.sub(" ", rest)
    return re.sub(r"[\s,.;!?]+", " ", rest).strip()


def extract_rules(text: str, task: dict, asking: str) -> dict:
    """Pull whatever we can out of one user message. `asking` = the field we just asked about."""
    out: dict = {}
    svc = services.detect(text)
    if svc and not task.get("service_key"):
        out.update(service_key=svc.key, service_label=svc.label, goal=svc.goal)
        residual = _residual(text, svc)
        if not svc.transport and len(residual.split()) >= 3 and not task.get("details"):
            out["details"] = residual[:240]  # "my kitchen tap is leaking…" already says what's wrong
    elif asking == "service" and not task.get("service_key"):
        label = re.sub(r"^(i need|i want|need|want|looking for|get me|book|find|please)\s+", "", text.strip(),
                       flags=re.I)
        label = re.sub(r"^(an?|some(one|body)|a person|people)\s+(to|for|who can|who will)?\s*", "", label,
                       flags=re.I).strip(" .!?")[:60]
        if label:
            out.update(service_key="other", service_label=label, goal="negotiate", details=text.strip()[:240])
    pickup, drop = _find_route(text)
    if pickup:
        out.update(pickup=pickup, drop=drop, location=task.get("location") or pickup)
    loc = _find_location(text)
    if loc and not pickup:
        out["location"] = loc
    elif asking == "location" and not loc and not pickup and not SKIP_RE.match(text):
        out["location"] = text.strip(" .!?")[:80]
    when = WHEN_RE.search(text)
    if when:
        out["when"] = when.group(0).strip()
    elif asking == "when" and not SKIP_RE.match(text):
        out["when"] = text.strip(" .!?")[:60]
    if asking == "budget" and NOT_SURE_RE.search(text):
        out["budget_suggested"] = True
    else:
        target, ceiling = _find_budget(text)
        if target:
            out.update(target_price=target, walkaway_ceiling=max(ceiling, target))
    if asking == "details":
        out["details"] = "" if SKIP_RE.match(text) else text.strip()[:240]
        out["details_done"] = True
    return out


async def extract_llm(intake: Intake, text: str) -> dict:
    """One Gemini call per message; anything it can't fill stays empty."""
    keys = ", ".join(s.key for s in services.SERVICES)
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in intake.messages[-8:])
    prompt = (f"You help a user in India arrange a local service. Conversation so far:\n{convo}\nuser: {text}\n\n"
              f"Current draft (JSON): {json.dumps(intake.task)}\n\n"
              "Update the draft from the conversation. Return ONLY JSON with these keys (use \"\" or 0 when unknown):\n"
              f'service_key: one of [{keys}, other]; service_label: short name of what they need (e.g. "home cook"); '
              'goal: "negotiate" (price-based job) or "appointment" (clinic/doctor/salon/lawyer slot booking); '
              "location: area and city; pickup and drop (only for cabs/movers/courier); when: date/time as said; "
              "details: concrete requirements in one short line; target_price and walkaway_ceiling: integers in rupees "
              "(only if the user stated a budget); est_km: total km for trips (0 if not a trip).")
    raw = await llm.generate(prompt, max_tokens=300, temperature=0.1, json_mode=True, timeout=6)
    if not raw:
        return {}
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", raw.strip()))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    out = {}
    for key in ("service_key", "service_label", "goal", "location", "pickup", "drop", "when", "details"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:240]
    if out.get("service_key") and out["service_key"] not in services.BY_KEY:
        out["service_key"] = "other"
    for key in ("target_price", "walkaway_ceiling"):
        value = parse_inr(data.get(key))
        if value and value >= 100:
            out[key] = value
    try:
        if float(data.get("est_km") or 0) > 0:
            out["est_km"] = float(data["est_km"])
    except (TypeError, ValueError):
        pass
    return out


# ---------------------------------------------------------------------------- dialogue

def missing_fields(task: dict, asked: list) -> list[str]:
    missing = []
    if not task.get("service_key"):
        missing.append("service")
    if not task.get("location"):
        missing.append("location")
    if not task.get("when"):
        missing.append("when")
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    if not task.get("details") and not task.get("details_done") and "details" not in asked:
        missing.append("details")
    if task.get("goal", svc.goal) == "negotiate" and not task.get("walkaway_ceiling"):
        missing.append("budget")
    return missing


def question(field_name: str, task: dict) -> tuple[str, list[str]]:
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    label = task.get("service_label") or svc.label
    generic = svc.key == "other"
    if field_name == "service":
        return ("Hi! Tell me what you need and I'll find nearby providers, call them, and fix the best deal or "
                "appointment. A cook, plumber, doctor's visit, movers, a cab: anything.",
                ["Home cook for my family", "Dentist appointment", "Plumber for a leak", "Cab to the airport"])
    if field_name == "location":
        if svc.transport:
            return f"Got it: {label}. Where from, and where to?", ["From JP Nagar to Kempegowda Airport"]
        return ("Got it. Which area should I search in?" if generic else
                f"Got it: {label}. Which area should I search in?"), ["JP Nagar, Bengaluru", "Koramangala, Bengaluru"]
    if field_name == "when":
        prompt = ("When would you like the appointment?" if svc.goal == "appointment" else
                  "When do you need this done?" if generic else f"When do you need the {label}?")
        return prompt, ["Today", "Tomorrow morning", "This weekend"]
    if field_name == "details":
        return (svc.detail_question or "Anything specific they should know?") + " (or say 'skip')", ["Skip"]
    if field_name == "budget":
        target, high = services.suggested_budget(svc)
        unit = services.UNIT_WORDS.get(svc.unit, "")
        return (f"What's your budget? Give a target and the most you'd pay{(' ' + unit) if unit else ''}, e.g. "
                f"'around {format_inr(target)}, max {format_inr(high)}'. Not sure? Say 'suggest'.",
                [f"Around {format_inr(target)}, max {format_inr(high)}", "Suggest a budget"])
    return "", []


def summary(task: dict) -> str:
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    label = task.get("service_label") or svc.label
    where = f"from {task['pickup']} to {task['drop']}" if task.get("pickup") and task.get("drop") else \
        f"near {task.get('location')}"
    parts = [f"**{label}** {where}, {task.get('when')}"]
    if task.get("details"):
        parts.append(task["details"])
    if task.get("goal") == "appointment":
        parts.append("I'll book the earliest suitable slot and get the fee")
    else:
        parts.append(f"target {format_inr(task.get('target_price'))}, never above {format_inr(task.get('walkaway_ceiling'))}")
    return " · ".join(parts)


async def chat(conversation_id: Optional[str], message: str, lat: Optional[float] = None,
               lng: Optional[float] = None) -> dict:
    intake = CONVERSATIONS.get(conversation_id or "") or Intake(id=uuid.uuid4().hex[:10])
    CONVERSATIONS[intake.id] = intake
    text = (message or "").strip()[:600]
    if lat is not None and lng is not None:
        intake.task.update(lat=lat, lng=lng)
    if text:
        asking = intake.last_question
        updates = extract_rules(text, intake.task, asking)
        llm_updates = await extract_llm(intake, text)
        if updates.get("service_key") == "other" and llm_updates.get("service_key") not in (None, "other"):
            updates.pop("service_key")
        # LLM fills gaps; explicit rule hits on the field we just asked about win
        merged = {**llm_updates, **{k: v for k, v in updates.items() if v not in (None, "")}}
        if merged.get("service_key") and intake.task.get("service_key") and asking != "service":
            merged.pop("service_key", None)  # don't silently switch service mid-conversation
            merged.pop("service_label", None)
        intake.task.update(merged)
        intake.messages.append({"role": "user", "text": text})
    task = intake.task
    if task.get("service_key"):
        svc = services.get(task["service_key"], task.get("service_label", ""))
        task.setdefault("service_label", svc.label)
        task["goal"] = task.get("goal") if task.get("goal") in {"negotiate", "appointment"} else svc.goal
        if task.pop("budget_suggested", False) or (task["goal"] == "negotiate" and "budget" in intake.asked
                                                   and not task.get("walkaway_ceiling") and NOT_SURE_RE.search(text)):
            task["target_price"], task["walkaway_ceiling"] = services.suggested_budget(svc)
    missing = missing_fields(task, intake.asked)
    if missing:
        nxt = missing[0]
        if nxt == "details" and "details" in intake.asked:
            task["details_done"] = True
            missing = missing_fields(task, intake.asked)
            nxt = missing[0] if missing else ""
    if missing:
        reply, suggestions = question(nxt, task)
        if nxt not in intake.asked:
            intake.asked.append(nxt)
        intake.last_question = nxt
    else:
        intake.last_question = "confirm"
        reply = f"Perfect. {summary(task)}. Shall I find the best-rated providers nearby?"
        suggestions = ["Find providers"]
    intake.messages.append({"role": "assistant", "text": reply})
    return {**intake.public(), "reply": reply, "suggestions": suggestions}


def to_request(task: dict) -> ServiceRequest:
    """Chat draft → ServiceRequest used by the auction engine."""
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    goal = task.get("goal") if task.get("goal") in {"negotiate", "appointment"} else svc.goal
    target, ceiling = int(task.get("target_price") or 0), int(task.get("walkaway_ceiling") or 0)
    if goal == "negotiate" and not ceiling:
        target, ceiling = services.suggested_budget(svc)
    if ceiling and target > ceiling:
        target = ceiling
    details = (task.get("details") or "").strip()
    label = task.get("service_label") or svc.label
    outstation = bool(re.search(r"outstation|round trip|\d+\s*day", f"{details} {task.get('drop', '')}", re.I))
    return ServiceRequest(
        category=svc.key if svc.key in services.BY_KEY else "other", service_label=label, goal=goal,
        summary=f"{label}{': ' + details if details else ''}"[:140], details=details,
        location=task.get("location", ""), pickup=task.get("pickup") or task.get("location", ""),
        drop=task.get("drop", ""), when=task.get("when", ""), target_price=target, walkaway_ceiling=ceiling,
        est_km=float(task.get("est_km") or (40 if svc.key == "cab_outstation" else 10)),
        min_km_per_day=250 if outstation else 0, lat=task.get("lat"), lng=task.get("lng"),
        language_hint=task.get("language", "hi"))
