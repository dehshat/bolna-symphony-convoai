"""Intake: talk to the user (by voice or text) until we know exactly what to arrange, then hand over to the calls.

The dialogue state is deterministic: order of questions service → where → when → details → budget (negotiated
services only) → confirm. Each message is understood by rules and, when available, by the concierge agent (a Gemini
chat model, see app.concierge), which also words the replies in the user's own language. The call language follows
the place ("Kothrud, Pune" → Marathi) unless the user picks one.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app import concierge, languages, llm, places, services
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
CITIES = ("Bengaluru", "Bangalore", "Mumbai", "Delhi", "New Delhi", "Pune", "Hyderabad", "Secunderabad", "Chennai",
          "Kolkata", "Mysuru", "Mysore", "Gurgaon", "Gurugram", "Noida", "Ghaziabad", "Faridabad", "Ahmedabad", "Surat",
          "Vadodara", "Rajkot", "Jaipur", "Kochi", "Ernakulam", "Thiruvananthapuram", "Trivandrum", "Kozhikode",
          "Thrissur", "Coorg", "Madikeri", "Mangaluru", "Mangalore", "Hubli", "Nagpur", "Nashik", "Thane",
          "Navi Mumbai", "Kolhapur", "Coimbatore", "Madurai", "Tiruchirappalli", "Visakhapatnam", "Vizag",
          "Vijayawada", "Warangal", "Howrah", "Siliguri", "Amritsar", "Ludhiana", "Jalandhar", "Chandigarh", "Mohali",
          "Patiala", "Bhubaneswar", "Cuttack", "Lucknow", "Kanpur", "Patna", "Bhopal", "Indore", "Varanasi", "Agra",
          "Dehradun", "Ranchi", "Raipur", "Guwahati")
_CITY_SET = {c.lower() for c in CITIES}

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
    user_lang: str = "en"           # the language the user talks to the app in
    trace: list = field(default_factory=list)
    touched: float = field(default_factory=time.time)

    def public(self) -> dict:
        missing = missing_fields(self.task, self.asked)
        code = self.task.get("call_language") or languages.DEFAULT
        return {"conversation_id": self.id, "messages": self.messages[-30:], "task": self.task,
                "ready": not missing, "missing": missing, "user_lang": self.user_lang,
                "call_language": {**languages.get(code).public(),
                                  "reason": self.task.get("call_language_reason", ""),
                                  "locked": bool(self.task.get("call_language_locked"))},
                "trace": self.trace[-20:]}


CONVERSATIONS: dict[str, Intake] = {}


# ---------------------------------------------------------------------------- extraction

def _find_location(text: str) -> str:
    for area in sorted(BENGALURU_AREAS, key=len, reverse=True):
        if re.search(rf"\b{re.escape(area)}\b", text, re.I):
            city = next((c for c in CITIES if re.search(rf"\b{c}\b", text, re.I)), "Bengaluru")
            return f"{area}, {city}"
    m = re.search(r"\b(?:in|at|near|around|nearby)\s+([A-Za-z][\w .'-]{2,40}?)(?=[,.!?]|\s+(?:for|by|on|at|tomorrow|"
                  r"today|tonight|this|next|around|under|with|budget|from)\b|$)", text, re.I)
    if m and not WHEN_RE.fullmatch(m.group(1).strip()) and \
            m.group(1).strip().split()[0].lower() not in languages.NAME_TO_CODE:   # "call them in Hindi" isn't a place
        place = _proper(m.group(1).strip())
        # "in Kothrud, Pune": keep the city too (it decides the call language)
        tail = re.match(r"\s*,\s*([A-Za-z][A-Za-z .'-]{2,30}?)(?=[,.!?]|\s+\w|$)", text[m.end():])
        if tail and tail.group(1).strip().lower() in _CITY_SET and tail.group(1).strip().lower() != place.lower():
            place += ", " + _proper(tail.group(1).strip())
        return place
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
    if asking == "location" and not pickup and "," in text and len(text.split()) <= 6 and not re.search(r"\d", text):
        loc = ", ".join(_proper(p.strip()) for p in text.strip(" .!?").split(",") if p.strip())  # "Kothrud, Pune"
    if loc and not pickup:
        out["location"] = loc
    elif not pickup and re.search(r"\bnear\s*(?:me|by)\b|\bnearby\b|\b(?:my|current|live) location\b", text, re.I):
        out["location"] = "near me"         # the browser's live position fills this in
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


def question(field_name: str, task: dict, user_lang: str = "en") -> tuple[str, list[str]]:
    """The built-in question for a field (used when the concierge model is unavailable). English, or Hindi."""
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    label = task.get("service_label") or svc.label
    generic = svc.key == "other"
    hi = user_lang == "hi"
    if field_name == "service":
        return (concierge.HINDI["greet"] if hi else
                "Hi! Tell me what you need, and I'll find the best-rated people nearby, call them in the local "
                "language, and fix the best deal or appointment.",
                ["Home cook for my family", "Dentist appointment", "Plumber for a leak", "Cab to the airport"])
    if field_name == "location":
        if svc.transport:
            return (concierge.HINDI["location_route"].format(label=label) if hi else
                    f"Got it: {label}. Where from, and where to?"), ["From JP Nagar to Kempegowda Airport"]
        return ((concierge.HINDI["location"].format(label=label) if hi else
                 "Got it. Which area should I search in?" if generic else f"Got it: {label}. Which area should I search in?"),
                ["📍 Use my current location", "Koramangala, Bengaluru", "Kothrud, Pune"])
    if field_name == "when":
        if hi:
            prompt = concierge.HINDI["when_appt"] if svc.goal == "appointment" else \
                concierge.HINDI["when"].format(label=label)
        else:
            prompt = ("When would you like the appointment?" if svc.goal == "appointment" else
                      "When do you need this done?" if generic else f"When do you need the {label}?")
        return prompt, ["Today", "Tomorrow morning", "This weekend"]
    if field_name == "details":
        return (concierge.HINDI["details"] if hi else
                (svc.detail_question or "Anything specific they should know?") + " (or say 'skip')"), ["Skip"]
    if field_name == "budget":
        target, high = services.suggested_budget(svc)
        unit = services.UNIT_WORDS.get(svc.unit, "")
        if hi:
            return (concierge.HINDI["budget"].format(target=format_inr(target), high=format_inr(high)),
                    [f"Around {format_inr(target)}, max {format_inr(high)}", "Suggest a budget"])
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


def _confirm_line(task: dict, user_lang: str) -> str:
    n = auto_call_count()
    lang = languages.get(task.get("call_language"))
    if user_lang == "hi":
        return concierge.HINDI["confirm"].format(summary=summary(task).replace("**", ""), n=n,
                                                 language=languages.HINDI_NAMES[lang.code])
    return (f"Perfect. {summary(task)}. Shall I call the {n} best-rated nearby, in {lang.name}? "
            f"(Say yes, or tell me what to change.)")


def auto_call_count() -> int:
    from app import config
    return config.settings.auto_call_count


_RESTART_RE = re.compile(r"\b(start over|start again|new request|reset|restart|cancel everything)\b", re.I)


def _set_call_language(intake: "Intake", requested: Optional[str], source: str) -> None:
    """The call language: what the user asked for (sticky), else the language of the place."""
    task = intake.task
    if requested:
        if requested != task.get("call_language") or not task.get("call_language_locked"):
            task.update(call_language=requested, call_language_locked=True,
                        call_language_reason=f"you chose {languages.name(requested)}")
            intake.trace.append({"agent": "linguist", "text": f"calls in {languages.name(requested)} ({source})"})
        return
    if task.get("call_language_locked"):
        return
    code, reason = languages.detect(task.get("location") or task.get("pickup") or "", task.get("state", ""))
    if code and code != task.get("call_language"):
        task.update(call_language=code, call_language_reason=reason)
        intake.trace.append({"agent": "linguist", "text": f"calls in {languages.name(code)}: {reason}"})
    elif not task.get("call_language"):
        task.update(call_language=languages.DEFAULT,
                    call_language_reason="Hindi, the most widely understood (tell me the city to pick its language)")


async def chat(conversation_id: Optional[str], message: str, lat: Optional[float] = None,
               lng: Optional[float] = None, user_lang: Optional[str] = None,
               call_language: Optional[str] = None) -> dict:
    intake = CONVERSATIONS.get(conversation_id or "") or Intake(id=uuid.uuid4().hex[:10])
    CONVERSATIONS[intake.id] = intake
    intake.touched = time.time()
    intake.user_lang = languages.normalize(user_lang) or intake.user_lang
    text = (message or "").strip()[:600]
    if _RESTART_RE.search(text):
        intake.task, intake.asked, intake.last_question = {}, [], ""
        text = ""
    if call_language:
        _set_call_language(intake, languages.normalize(call_language), "picked on screen")
    if lat is not None and lng is not None:
        intake.task.update(lat=lat, lng=lng)
        if places.is_near_me(intake.task.get("location", "")) and not intake.task.get("pickup"):
            # live location: name the area so the user can see (and correct) where we'll search
            intake.task["location"] = await places.reverse_geocode(lat, lng) or "your current location"
            intake.task["state"] = places.state_for(lat, lng)
    action, thought, wants_change = "", {}, False
    if text:
        asking = intake.last_question
        confirming = asking in {"confirm", "change"} and not missing_fields(intake.task, intake.asked)
        # A bare "yes / haan / ಹೌದು" starts the calls; "yes but call them in Hindi" changes something first
        if confirming and languages.is_yes(text) and not languages.requested_language(text) and \
                not extract_rules(text, intake.task, asking):
            action = "go"
        else:
            updates = extract_rules(text, intake.task, asking)
            thought = await concierge.think(intake.messages, text, intake.task,
                                            missing_fields(intake.task, intake.asked),
                                            intake.task.get("call_language") or languages.DEFAULT,
                                            intake.task.get("call_language_reason", ""), intake.user_lang)
            llm_updates = thought.get("updates", {})
            if thought.get("intent") == "yes" and asking in {"confirm", "change"} and not llm_updates and \
                    not missing_fields(intake.task, intake.asked):
                action = "go"
            if updates.get("service_key") == "other" and llm_updates.get("service_key") not in (None, "other"):
                updates.pop("service_key")
            # The model fills gaps; explicit rule hits on the field we just asked about win
            merged = {**llm_updates, **{k: v for k, v in updates.items() if v not in (None, "")}}
            if merged.get("service_key") and intake.task.get("service_key") and asking != "service":
                merged.pop("service_key", None)  # don't silently switch service mid-conversation
                merged.pop("service_label", None)
            requested = languages.requested_language(text) or merged.pop("call_language", None)
            merged.pop("call_language", None)
            intake.task.update(merged)
            _set_call_language(intake, requested, "you asked")
            intake.trace.append({"agent": "concierge", "text": ("understood with " + llm.status()["last_used"].get(
                "chat", "Gemini")) if thought.get("updates") or thought.get("reply") else "understood with rules"})
            if confirming and (languages.is_no(text) or thought.get("intent") == "no") and not merged \
                    and not requested:
                wants_change = True
        intake.messages.append({"role": "user", "text": text})
    task = intake.task
    if task.get("service_key"):
        svc = services.get(task["service_key"], task.get("service_label", ""))
        task.setdefault("service_label", svc.label)
        task["goal"] = task.get("goal") if task.get("goal") in {"negotiate", "appointment"} else svc.goal
        if task.pop("budget_suggested", False) or (task["goal"] == "negotiate" and "budget" in intake.asked
                                                   and not task.get("walkaway_ceiling") and NOT_SURE_RE.search(text)):
            task["target_price"], task["walkaway_ceiling"] = services.suggested_budget(svc)
    _set_call_language(intake, None, "")
    missing = missing_fields(task, intake.asked)
    if missing:
        nxt = missing[0]
        if nxt == "details" and "details" in intake.asked:
            task["details_done"] = True
            missing = missing_fields(task, intake.asked)
            nxt = missing[0] if missing else ""
    reply_lang = "hi" if intake.user_lang == "hi" else "en"
    llm_reply = thought.get("reply", "")
    if action == "go":
        intake.last_question = "go"
        reply = concierge.HINDI["go"] if intake.user_lang == "hi" else \
            f"On it. Finding the best-rated {task.get('service_label', 'providers')} near you and calling them now."
        suggestions = []
    elif missing:
        reply, suggestions = question(nxt, task, intake.user_lang)
        if llm_reply and thought.get("asking") == nxt and concierge.reply_ok(llm_reply, task):
            reply, reply_lang = llm_reply, thought.get("reply_language") or intake.user_lang
        if nxt not in intake.asked:
            intake.asked.append(nxt)
        intake.last_question = nxt
    elif wants_change:
        intake.last_question = "change"
        reply = concierge.HINDI["change"] if intake.user_lang == "hi" else "Sure, what should I change?"
        if llm_reply and concierge.reply_ok(llm_reply, task):
            reply, reply_lang = llm_reply, thought.get("reply_language") or intake.user_lang
        suggestions = ["Change the time", "Change the budget", "Call in another language"]
    else:
        intake.last_question = "confirm"
        reply = _confirm_line(task, intake.user_lang)
        if llm_reply and thought.get("asking") in {"confirm", "none"} and concierge.reply_ok(llm_reply, task):
            reply, reply_lang = llm_reply, thought.get("reply_language") or intake.user_lang
        suggestions = ["Yes, call them", "Change something"]
    intake.messages.append({"role": "assistant", "text": reply})
    return {**intake.public(), "reply": reply, "reply_language": reply_lang, "suggestions": suggestions,
            "action": action}


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
        language_hint=task.get("call_language") or task.get("language") or languages.DEFAULT)
