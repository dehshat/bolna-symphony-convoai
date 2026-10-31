"""Intake: talk to the user (by voice or text) until we know exactly what to arrange, then hand over to the calls.

The dialogue state is deterministic: order of questions service → where → when → details → budget (negotiated
services only) → confirm. Each message is understood by rules and, when available, by the concierge agent (a Gemini
chat model, see app.concierge), which also words the replies in the user's own language. The call language follows
the place ("Kothrud, Pune" → Marathi) unless the user picks one.

The user can talk in any of the 11 languages. The first message that shows its language (from what speech recognition
heard, or the script of the text) fixes it for the whole conversation: every reply, the summary and the confirmation
come back in it (`app.userlex`), and it never flips mid-conversation. Picking a language in the settings fixes it too.

The call language is the local language of the place the service is needed in: from the area/city table, the state of
a GPS position, or (for places the table doesn't know) the state OpenStreetMap puts the place in. It changes only
when the user gives another place, or names a language for the calls; once named, it stays.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from app import concierge, languages, lexicon, llm, places, services, userlex
from app.pricing import ServiceRequest, parse_inr

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
# "Not sure / suggest one" in the other languages (substrings: \b doesn't work between Indic letters)
NOT_SURE_NATIVE = ("पता नहीं", "सुझाओ", "सुझाइए", "माहीत नाही", "सुचवा", "ખબર નથી", "સૂચવો", "জানি না", "প্রস্তাব",
                   "ਪਤਾ ਨਹੀਂ", "ਸੁਝਾਓ", "ଜଣା ନାହିଁ", "ପରାମର୍ଶ", "ಗೊತ್ತಿಲ್ಲ", "ಸೂಚಿಸಿ", "தெரியாது", "தெரியவில்லை",
                   "பரிந்துரை", "తెలియదు", "సూచించండి", "അറിയില്ല", "നിർദ്ദേശിക്കൂ")
SKIP_RE = re.compile(r"^\s*(no|nope|nothing|none|skip|na|that'?s it|nahi|illa)\s*[.!]?\s*$", re.I)
BUDGET_HINT_RE = re.compile(r"(₹|rs\.?|rupees?|budget|max|maximum|under|upto|up to|within|around|approx|k\b|\d{3,})",
                            re.I)

FIELDS = ("service", "location", "when", "details", "budget")


def _not_sure(text: str) -> bool:
    return bool(NOT_SURE_RE.search(text)) or any(w in text for w in NOT_SURE_NATIVE)


def _skip(text: str) -> bool:
    """'skip' / 'no' / 'छोड़ो' / 'ಬಿಡಿ': nothing more to add."""
    return bool(SKIP_RE.match(text)) or userlex.matches_chip(text, "chip_skip") or \
        (languages.is_no(text) and len(text.split()) <= 2)


GREETINGS = {"hi", "hii", "hello", "hey", "namaste", "namaskar", "namaskaram", "vanakkam", "sat sri akal",
             "good morning", "good afternoon", "good evening", "नमस्ते", "नमस्कार", "प्रणाम", "নমস্কার", "નમસ્તે",
             "ਸਤ ਸ੍ਰੀ ਅਕਾਲ", "ନମସ୍କାର", "ನಮಸ್ಕಾರ", "வணக்கம்", "నమస్కారం", "നമസ്കാരം"}


def _greeting(text: str) -> bool:
    """'Hello' / 'नमस्ते जी': a greeting, nothing to act on (and nothing misunderstood)."""
    t = re.sub(r"[\s.,!?।]+", " ", text.lower()).strip()
    return re.sub(r"\s+(ji|sir|madam|there)$", "", t) in GREETINGS


def _not_a_service(text: str) -> bool:
    """A greeting, a bare yes/no, or only a place or time: none of these names the service."""
    return _greeting(text) or _only_place_or_time(text) or \
        (len(text.split()) <= 2 and (languages.is_yes(text) or languages.is_no(text)))


@dataclass
class Intake:
    id: str
    messages: list = field(default_factory=list)
    task: dict = field(default_factory=dict)
    last_question: str = ""         # what the last reply asked: a field, "confirm", "change" or "go"
    misses: dict = field(default_factory=dict)   # field -> answers to it that gave nothing usable
    user_lang: str = "en"           # the language the user talks to the app in (detected, or chosen)
    lang_auto: bool = True          # detect it (off once the user picks one in the settings)
    lang_locked: bool = False       # fixed for the rest of the conversation (detected once, or picked)
    trace: list = field(default_factory=list)
    touched: float = field(default_factory=time.time)

    def public(self) -> dict:
        missing = missing_fields(self.task)
        code = self.task.get("call_language") or languages.DEFAULT
        return {"conversation_id": self.id, "messages": self.messages[-30:], "task": self.task,
                "ready": not missing, "missing": missing, "user_lang": self.user_lang,
                "user_lang_locked": self.lang_locked,
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
    area = re.search(rf"\b([A-Z][A-Za-z.'-]{{2,30}}(?: [A-Z][A-Za-z.'-]{{1,30}})?),\s*{re.escape(city)}\b", text) \
        if city else None
    if area and area.group(1).lower() not in _CITY_SET:
        return f"{_proper(area.group(1))}, {city}"   # "मला Kothrud, Pune मध्ये plumber पाहिजे"
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


# "I need / want / in / for" in the other languages: not details (whole words; \b doesn't work between Indic letters)
NATIVE_FILLER = set("""मुझे हमें चाहिए है हैं में के की का लिए एक कोई मला आम्हाला पाहिजे हवा हवी हवं आहे मध्ये साठी इथे
मने અમને જોઈએ છે માં માટે আমার আমাদের চাই লাগবে একজন জন্য ਮੈਨੂੰ ਸਾਨੂੰ ਚਾਹੀਦਾ ਚਾਹੀਦੀ ਹੈ ਵਿੱਚ ਲਈ ମୋତେ ଆମକୁ ଦରକାର ରେ
ପାଇଁ ನನಗೆ ನಮಗೆ ಬೇಕು ಒಬ್ಬ ಅಲ್ಲಿ ಗೆ எனக்கு எங்களுக்கு வேண்டும் ஒரு இல் నాకు మాకు కావాలి ఒక లో కోసం എനിക്ക്
ഞങ്ങൾക്ക് വേണം ഒരു ഇൽ""".split())


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
    rest = " ".join(w for w in rest.split() if w.strip(",.;!?।") not in NATIVE_FILLER and
                    not lexicon.english_when(w))   # "कल / ನಾಳೆ / நாளை" is the time, not a detail
    return re.sub(r"[\s,.;!?]+", " ", rest).strip()


def _only_place_or_time(text: str) -> bool:
    """'Koramangala, Bengaluru' or 'tomorrow' says where or when, not what: never take it for the service."""
    rest = WHEN_RE.sub(" ", text)
    for part in _find_location(text).split(","):
        if part.strip():
            rest = re.sub(re.escape(part.strip()), " ", rest, flags=re.I)
    rest = re.sub(r"\b(in|at|near|around|from|to|on|by)\b", " ", rest, flags=re.I)
    return not [w for w in re.split(r"[\s,.;!?।]+", rest) if w and not lexicon.english_when(w)]


def extract_rules(text: str, task: dict, asking: str) -> dict:
    """Pull whatever we can out of one user message. `asking` = the field we just asked about."""
    out: dict = {}
    svc = services.detect(text)
    if svc and not task.get("service_key"):
        out.update(service_key=svc.key, service_label=svc.label, goal=svc.goal)
        residual = _residual(text, svc)
        if not svc.transport and len(residual.split()) >= 3 and not task.get("details"):
            out["details"] = residual[:240]  # "my kitchen tap is leaking…" already says what's wrong
    elif asking == "service" and not task.get("service_key") and not _not_a_service(text):
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
    elif asking == "location" and not loc and not pickup and not _skip(text):
        out["location"] = text.strip(" .!?")[:80]
    when = WHEN_RE.search(text)
    native_when = lexicon.english_when(text)   # "उद्या सकाळी" -> "tomorrow morning", so every call can say it
    if when:
        out["when"] = when.group(0).strip()
    elif native_when and (asking == "when" or re.search(r"today|tomorrow|tonight|now|day", native_when)):
        out["when"] = native_when
    elif asking == "when" and not _skip(text):
        out["when"] = text.strip(" .!?")[:60]
    if asking == "budget" and _not_sure(text):
        out["budget_suggested"] = True
    else:
        target, ceiling = _find_budget(text)
        if target:
            out.update(target_price=target, walkaway_ceiling=max(ceiling, target))
    if asking == "details":
        out["details"] = "" if _skip(text) else text.strip()[:240]
        out["details_done"] = True
    return out


# ---------------------------------------------------------------------------- dialogue

def missing_fields(task: dict) -> list[str]:
    """What we still need to know, in the order it is asked."""
    missing = []
    if not task.get("service_key"):
        missing.append("service")
    if not task.get("location"):
        missing.append("location")
    if not task.get("when"):
        missing.append("when")
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    if not task.get("details") and not task.get("details_done"):
        missing.append("details")
    if task.get("goal", svc.goal) == "negotiate" and not task.get("walkaway_ceiling"):
        missing.append("budget")
    return missing


def service_for_user(task: dict, user_lang: str) -> str:
    """What to call the service when talking to the user: their own words if they're in their language, else the
    language's name for it ('plumber' -> 'ಪ್ಲಂಬರ್' for a Kannada speaker)."""
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    label = task.get("service_label") or svc.label
    lang = userlex.code(user_lang)
    if lang == "en" or languages.writes_in(label, lang) or svc.key not in lexicon.SERVICE_NAMES or svc.key == "other":
        return label
    return lexicon.service_name(svc.key, label, lang)


def question(field_name: str, task: dict, user_lang: str = "en", opening: str = "greet") -> tuple[str, list[str]]:
    """The built-in question for a field (used when the concierge model is unavailable), in the user's language.
    `opening` words the service question: 'greet' (first visit), 'greet_back' (returning) or 'service_q' (mid-chat)."""
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    lang = userlex.code(user_lang)
    label = service_for_user(task, lang)
    generic = svc.key == "other"
    say = lambda key, **kw: userlex.line(lang, key, **kw)  # noqa: E731
    if field_name == "service":
        return say(opening), userlex.chips(lang, "chip_examples")
    if field_name == "location":
        if svc.transport:
            return say("location_route", service=label), (["From JP Nagar to Kempegowda Airport"] if lang == "en" else [])
        return (say("location_generic" if generic else "location", service=label),
                [say("chip_location"), "Koramangala, Bengaluru", "Kothrud, Pune"])
    if field_name == "when":
        prompt = say("when_appt") if svc.goal == "appointment" else \
            say("when_generic") if generic else say("when", service=label)
        return prompt, [say("chip_today"), say("chip_tomorrow"), say("chip_weekend")]
    if field_name == "details":
        prompt = (svc.detail_question or "Anything specific they should know?") + " (or say 'skip')" \
            if lang == "en" else say("details")
        return prompt, [say("chip_skip")]
    if field_name == "budget":
        target, high = services.suggested_budget(svc)
        return (say("budget", unit=userlex.unit_phrase(lang, svc.unit), target=target, high=high),
                [say("chip_budget", target=target, high=high), say("chip_suggest")])
    return "", []


def summary(task: dict, user_lang: str = "en") -> str:
    """The request in one line, in the user's language: what, where, when, details, budget."""
    lang = userlex.code(user_lang)
    label = service_for_user(task, lang)
    when = lexicon.localize_when(task.get("when") or "", lang) if task.get("when") else userlex.line(lang, "soon")
    if task.get("pickup") and task.get("drop"):
        parts = [userlex.line(lang, "sum_route", service=label, pickup=task["pickup"], drop=task["drop"], when=when)]
    else:
        parts = [userlex.line(lang, "sum_near", service=label, place=task.get("location"), when=when)]
    if task.get("details"):
        parts.append(task["details"])
    if task.get("goal") == "appointment":
        parts.append(userlex.line(lang, "sum_appt"))
    else:
        parts.append(userlex.line(lang, "sum_budget", target=int(task.get("target_price") or 0),
                                  ceiling=int(task.get("walkaway_ceiling") or 0)))
    return " · ".join(parts)


def _confirm_line(task: dict, user_lang: str) -> str:
    call_language = languages.get(task.get("call_language")).code
    return userlex.line(user_lang, "confirm", summary=summary(task, user_lang), n=auto_call_count(),
                        language=userlex.language_name(user_lang, call_language))


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


def _set_user_language(intake: "Intake", setting: Optional[str], text: str, heard: Optional[str]) -> None:
    """The language to answer in: the one picked in the settings, else the one the user first spoke in.

    It is decided once and then fixed for the conversation: a Tamil speaker who later says "okay", reads out an
    English address or mixes in English words still gets Tamil. Messages that don't show a language ("2000",
    "Koramangala, Bengaluru") decide nothing; until one does, Swarm talks in the browser's language."""
    picked = languages.normalize(setting) if (setting or "").strip().lower() != "auto" else None
    if picked:
        if picked != intake.user_lang or not intake.lang_locked:
            intake.trace.append({"agent": "linguist", "text": f"talking to you in {languages.name(picked)} "
                                                              "(picked in the settings)"})
        intake.lang_auto, intake.lang_locked, intake.user_lang = False, True, picked
        return
    if (setting or "").strip().lower() == "auto" and not intake.lang_auto:
        intake.lang_auto, intake.lang_locked = True, False   # the user went back to "any language"
    if intake.lang_locked:
        return
    if not text:
        # Before the first word: greet in the browser's language, if it's one of ours (not fixed yet)
        if not intake.messages and languages.normalize(heard):
            intake.user_lang = languages.normalize(heard)
        return
    detected = languages.confident_language(text, heard, intake.user_lang)
    if detected:
        intake.user_lang, intake.lang_locked = detected, True
        intake.trace.append({"agent": "linguist", "text": f"you're speaking {languages.name(detected)}: the whole "
                                                          f"conversation stays in {languages.name(detected)}"})


async def _place_state(task: dict) -> None:
    """For a place the language table doesn't know, look up its state, which picks the local call language."""
    place = task.get("location") or task.get("pickup") or ""
    if not place or task.get("state") or places.is_near_me(place) or languages.detect(place)[0]:
        return
    state = await places.state_of(place)
    if state:
        task["state"] = state


# ---------------------------------------------------------------------------- one turn: understand → decide → reply

@dataclass
class Turn:
    """What one user message did: the facts the decision tree looks at."""
    text: str = ""
    repeat: bool = False        # "what? / फिर से": they want the last question again
    go: bool = False            # a plain yes to the summary
    change: bool = False        # "no / change something" to the summary
    understood: bool = False    # the message gave or changed something, asked a question, or just said hello
    defaulted: bool = False     # a safe default was just filled in for a question they couldn't answer
    thought: dict = field(default_factory=dict)   # the concierge model's reading ({} without Gemini)


MAX_MISSES = 2   # answers that give nothing before a question gets a safe default (service and place: never)
DEFAULTS = {"when": "as soon as possible", "details": "no details", "budget": "the usual price range"}


async def _understand(intake: Intake, turn: Turn) -> None:
    """Read one message with the rules and (if available) the concierge model, and apply what it gives."""
    text, asking, task = turn.text, intake.last_question, intake.task
    confirming = asking in {"confirm", "change"} and not missing_fields(task)
    rules = extract_rules(text, task, asking)
    # A bare "yes / haan / ಹೌದು" starts the calls without asking the model; "yes but call in Hindi" changes first
    if confirming and languages.is_yes(text) and not rules and not languages.requested_language(text):
        turn.go = turn.understood = True
        return
    thought = turn.thought = await concierge.think(
        intake.messages, text, task, missing_fields(task), task.get("call_language") or languages.DEFAULT,
        task.get("call_language_reason", ""), intake.user_lang)
    llm_updates = thought.get("updates", {})
    if rules.get("service_key") == "other" and llm_updates.get("service_key") not in (None, "other"):
        rules.pop("service_key")    # the model recognised a known service in the user's own words
    # The model fills gaps; explicit rule hits on the field we just asked about win
    merged = {**llm_updates, **{k: v for k, v in rules.items() if v not in (None, "")}}
    if merged.get("service_key") and task.get("service_key") and asking != "service":
        merged.pop("service_key", None)  # don't silently switch service mid-conversation
        merged.pop("service_label", None)
    # The call language follows the place; it changes only when the user names a language for the calls
    # (never on the model's guess, e.g. their own language when they didn't ask for it)
    llm_language = merged.pop("call_language", None)
    requested = languages.requested_language(text) or (llm_language if languages.mentions_language(text) else None)
    moved = any(merged.get(k) and merged[k] != task.get(k) for k in ("location", "pickup"))
    if moved and not places.is_near_me(merged.get("location", "")):
        task.pop("state", None)   # a GPS state belongs to the old place, not the one just typed
    task.update(merged)
    await _place_state(task)
    _set_call_language(intake, requested, "you asked")
    intake.trace.append({"agent": "concierge", "text": ("understood with " + llm.status()["last_used"].get(
        "chat", "Gemini")) if thought.get("updates") or thought.get("reply") else "understood with rules"})
    turn.understood = bool(merged or requested) or _greeting(text) or \
        thought.get("intent") in {"question", "smalltalk"}
    if confirming and not merged and not requested:
        if thought.get("intent") == "yes":
            turn.go = True
        elif languages.is_no(text) or thought.get("intent") == "no" or userlex.matches_chip(text, "chip_change"):
            turn.change = True


def _use_default(intake: Intake, field_name: str) -> bool:
    """Fill a safe value for a question the user couldn't answer twice, so nobody gets stuck on it."""
    task = intake.task
    if field_name == "when":
        task["when"] = DEFAULTS["when"]
    elif field_name == "details":
        task["details_done"] = True
    elif field_name == "budget":
        svc = services.get(task.get("service_key"), task.get("service_label", ""))
        task["target_price"], task["walkaway_ceiling"] = services.suggested_budget(svc)
    else:
        return False     # what they need and where are never guessed
    intake.trace.append({"agent": "concierge", "text": f"no clear answer about {field_name} twice: using "
                                                       f"{DEFAULTS[field_name]} (you can change it before the calls)"})
    return True


def decide(intake: Intake, turn: Turn) -> tuple[str, str]:
    """The concierge's decision tree: what to do after this message. Returns (step, field).

        they want the last line again ("what?", "फिर से")  → repeat   the last question, plainly; nothing changes
        a plain yes to the summary                         → go       find providers nearby and call them
        something still missing, in this order: service,   → ask      the first one, one thing at a time ("sorry,
          place, when, details, budget (priced jobs only)              I didn't catch that" first if their answer
                                                                       gave nothing)
            └ answered twice with nothing usable           → default  when: as soon as possible · details: none ·
                                                                       budget: the usual range; then carry on
        "no / change something" to the summary             → change   ask what to change
        everything known                                   → confirm  one-line summary: "shall I call them?"
    """
    if turn.repeat:
        return "repeat", intake.last_question
    if turn.go:
        return "go", ""
    missing = missing_fields(intake.task)
    while missing and intake.misses.get(missing[0], 0) >= MAX_MISSES and _use_default(intake, missing[0]):
        turn.defaulted = True
        missing = missing_fields(intake.task)
    if missing:
        return "ask", missing[0]
    return ("change" if turn.change else "confirm"), ""


def _reply(intake: Intake, step: str, nxt: str, turn: Turn, returning: bool) -> tuple[str, list[str]]:
    """Word the decision in the user's language. The model's wording is used only if it's about the right thing,
    invents no price and is in their language; otherwise the built-in line (`app.userlex`)."""
    task, lang = intake.task, intake.user_lang
    llm_reply, asking = turn.thought.get("reply", ""), turn.thought.get("asking")
    usable = bool(llm_reply) and not turn.defaulted and concierge.reply_ok(llm_reply, task) and \
        languages.writes_in(llm_reply, lang)
    if step == "repeat":     # said again, the plain built-in way
        step, usable = ("ask" if nxt in FIELDS else nxt), False
    if step == "go":
        return userlex.line(lang, "go", service=service_for_user(task, lang)), []
    if step == "ask":
        opening = "service_q" if turn.text else "greet_back" if returning or intake.messages else "greet"
        reply, suggestions = question(nxt, task, lang, opening)
        if usable and asking == nxt:
            reply = llm_reply
        elif turn.text and not turn.understood and not turn.repeat and intake.last_question == nxt:
            reply = f"{userlex.line(lang, 'again')} {reply}"
        return reply, suggestions
    if step == "change":
        return (llm_reply if usable else userlex.line(lang, "change"),
                [userlex.line(lang, k) for k in ("chip_time", "chip_money", "chip_language")])
    reply = llm_reply if usable and asking in {"confirm", "none"} else _confirm_line(task, lang)
    return reply, [userlex.line(lang, "chip_yes"), userlex.line(lang, "chip_change")]


def _complete(task: dict) -> None:
    """What follows from the service: its label, its goal, and the suggested budget if the user asked for one."""
    if not task.get("service_key"):
        return
    svc = services.get(task["service_key"], task.get("service_label", ""))
    task.setdefault("service_label", svc.label)
    task["goal"] = task.get("goal") if task.get("goal") in {"negotiate", "appointment"} else svc.goal
    if task.pop("budget_suggested", False):
        task["target_price"], task["walkaway_ceiling"] = services.suggested_budget(svc)


async def chat(conversation_id: Optional[str], message: str, lat: Optional[float] = None,
               lng: Optional[float] = None, user_lang: Optional[str] = None,
               call_language: Optional[str] = None, heard_lang: Optional[str] = None,
               returning: bool = False) -> dict:
    """One turn: understand the message, decide what to do next (`decide`), say it in the user's language.

    `user_lang` is the language picked in the settings ('auto' or None: detect it); `heard_lang` is the language
    speech recognition heard (or, before the first message, the browser's language); `returning` means this browser
    has used the app before, so the greeting is short."""
    intake = CONVERSATIONS.get(conversation_id or "") or Intake(id=uuid.uuid4().hex[:10])
    CONVERSATIONS[intake.id] = intake
    intake.touched = time.time()
    text = (message or "").strip()[:600]
    _set_user_language(intake, user_lang, text, heard_lang)
    if _RESTART_RE.search(text):
        intake.task, intake.misses, intake.last_question = {}, {}, ""
        text = ""
    if call_language:
        _set_call_language(intake, languages.normalize(call_language), "picked on screen")
    if lat is not None and lng is not None:
        intake.task.update(lat=lat, lng=lng)
        if places.is_near_me(intake.task.get("location", "")) and not intake.task.get("pickup"):
            # live location: name the area so the user can see (and correct) where we'll search
            intake.task["location"] = await places.reverse_geocode(lat, lng) or "your current location"
            intake.task["state"] = places.state_for(lat, lng)
    turn = Turn(text=text)
    if text:
        if intake.last_question and languages.is_repeat(text):
            turn.repeat = True
        else:
            await _understand(intake, turn)
            if not turn.understood and intake.last_question in FIELDS:
                intake.misses[intake.last_question] = intake.misses.get(intake.last_question, 0) + 1
    task = intake.task
    _complete(task)
    _set_call_language(intake, None, "")
    step, nxt = decide(intake, turn)
    lang = intake.user_lang
    reply, suggestions = _reply(intake, step, nxt, turn, returning)
    if step != "repeat":
        intake.last_question = nxt if step == "ask" else step
    task["user_lang"] = lang      # travels with the task, so the calls are narrated and summed up in it too
    if text:
        intake.messages.append({"role": "user", "text": text})
    intake.messages.append({"role": "assistant", "text": reply})
    return {**intake.public(), "reply": reply, "reply_language": lang, "suggestions": suggestions,
            "action": "go" if step == "go" else "", "decision": step,
            "service_name": service_for_user(task, lang) if task.get("service_key") else ""}


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
        language_hint=task.get("call_language") or task.get("language") or languages.DEFAULT,
        user_language=userlex.code(task.get("user_lang") or "en"),
        user_summary=summary({**task, "target_price": target, "walkaway_ceiling": ceiling, "goal": goal},
                             task.get("user_lang") or "en").replace("**", ""))
