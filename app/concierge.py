"""Concierge agent: the voice the user talks to. It understands what they need and asks for what's missing.

Hybrid by design. A deterministic state machine (app.intake) decides WHAT is still missing and when the request is
ready; a Gemini chat model (its own model, same API key) supplies the understanding and the natural wording, in
whatever language the user speaks. Its reply is used only if it asks about the field the state machine says comes
next, mentions no price that wasn't given or suggested, and is short enough to be spoken. Otherwise the built-in
question is used, so the conversation never stalls when the model is slow or its quota is used up.
"""

from __future__ import annotations

import json
import re

from app import languages, llm, services
from app.pricing import format_inr

FIELDS = ("service", "location", "when", "details", "budget")
ASKING = set(FIELDS) | {"confirm", "none"}
INTENTS = {"inform", "yes", "no", "change", "restart", "question", "smalltalk"}

SYSTEM = """You are Swarm, a warm and quick voice concierge in India. You help the user get a local service done:
you find out exactly what they need; then the app finds the best-rated providers nearby and phones them in the local
language to get the best all-inclusive price or an appointment. The user's phone number is never shared.

Your words are spoken aloud: one or two short, natural sentences. No lists, no markdown, no emojis.
Reply in the language the user speaks (if they mix Hindi and English, mix the same way).

Collect, in this order, skipping anything already known:
service -> location (area and city; for cabs, movers and courier: from where to where) -> when -> details (one line of
specifics, the user may skip) -> budget (priced jobs only, never for appointments: a target and the most they'd pay;
if they're unsure, suggest the typical range you are given).
Ask about exactly ONE missing thing per turn. When nothing is missing, summarise the request in one sentence and ask
whether to start calling the providers, in the call language you are given.
Never invent facts or prices. Only suggest the typical range given. Never promise a price.
If they ask something else (how it works, privacy, which language the calls use), answer in a sentence, then continue.
"""

SCHEMA = """Return ONLY JSON:
{"updates": {only fields the user just gave or changed: "service_key", "service_label", "goal" ("negotiate" or
  "appointment"), "location", "pickup", "drop", "when", "details", "target_price", "walkaway_ceiling" (integers, rupees,
  only if the user said a budget), "est_km", "call_language" (code, only if the user asked for one)},
 "asking": "service|location|when|details|budget|confirm|none",
 "reply": "what you say next",
 "reply_language": "ISO 639-1 code of your reply",
 "intent": "inform|yes|no|change|restart|question|smalltalk"}"""


def _typical(task: dict) -> str:
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    if svc.key == "other" or svc.goal == "appointment":
        return ""
    low, high = svc.typical
    unit = services.UNIT_WORDS.get(svc.unit, "")
    return f"Typical price for {svc.label}: {format_inr(low)}–{format_inr(high)}{(' ' + unit) if unit else ''}."


def allowed_amounts(task: dict) -> set[int]:
    """Rupee amounts the concierge may say: what the user gave, and the typical range / suggestion for the service."""
    svc = services.get(task.get("service_key"), task.get("service_label", ""))
    amounts = {int(task.get(k) or 0) for k in ("target_price", "walkaway_ceiling")}
    amounts |= set(svc.typical) | set(services.suggested_budget(svc))
    return {a for a in amounts if a}


def reply_ok(reply: str, task: dict) -> bool:
    """Short enough to speak, and no rupee amount nobody gave or suggested."""
    if not reply or len(reply) > 340:
        return False
    allowed = allowed_amounts(task)
    for raw in re.findall(r"\d[\d,]*(?:\.\d+)?\s*k?\b", reply.replace("₹", " ")):
        value = raw.replace(",", "").strip().lower()
        number = float(value[:-1]) * 1000 if value.endswith("k") else float(value)
        if number >= 100 and int(number) not in allowed:
            return False
    return True


async def think(history: list[dict], text: str, task: dict, missing: list[str], call_language: str,
                language_reason: str, user_language: str) -> dict:
    """One Gemini turn. Returns {} when the model isn't available, so the caller falls back to rules."""
    if not llm.enabled():
        return {}
    catalogue = ", ".join(f"{s.key}: {s.label}" for s in services.SERVICES)
    convo = "\n".join(f"{m['role']}: {m['text']}" for m in history[-10:])
    lang = languages.get(call_language)
    prompt = (f"Known services (service_key: label), or \"other\" with the user's own words: {catalogue}\n"
              f"{_typical(task)}\n"
              f"Current draft: {json.dumps({k: v for k, v in task.items() if k not in ('lat', 'lng')}, ensure_ascii=False)}\n"
              f"Still missing before this message, in order: {', '.join(missing) or 'nothing'}\n"
              f"Calls will be made in {lang.name} ({lang.native}) — {language_reason or 'default'}.\n"
              f"User's language setting: {languages.get(user_language).name}.\n"
              f"Conversation so far:\n{convo}\nuser: {text}\n\n{SCHEMA}")
    raw = await llm.generate(prompt, system=SYSTEM, max_tokens=420, temperature=0.4, json_mode=True, timeout=7,
                             role="chat")
    data = llm.parse_json(raw) or {}
    out: dict = {}
    updates = data.get("updates") if isinstance(data.get("updates"), dict) else {}
    clean: dict = {}
    for key in ("service_key", "service_label", "goal", "location", "pickup", "drop", "when", "details"):
        value = updates.get(key)
        if isinstance(value, str) and value.strip():
            clean[key] = value.strip()[:240]
    if clean.get("service_key") and clean["service_key"] not in services.BY_KEY:
        clean["service_key"] = "other"
    if clean.get("goal") not in (None, "negotiate", "appointment"):
        clean.pop("goal")
    for key in ("target_price", "walkaway_ceiling"):
        try:
            value = int(float(str(updates.get(key) or 0).replace(",", "")))
        except ValueError:
            value = 0
        if value >= 100:
            clean[key] = value
    try:
        if float(updates.get("est_km") or 0) > 0:
            clean["est_km"] = float(updates["est_km"])
    except (TypeError, ValueError):
        pass
    code = languages.normalize(updates.get("call_language"))
    if code:
        clean["call_language"] = code
    out["updates"] = clean
    if isinstance(data.get("reply"), str):
        out["reply"] = re.sub(r"[*_#`]", "", data["reply"]).strip()
    out["asking"] = data.get("asking") if data.get("asking") in ASKING else ""
    out["intent"] = data.get("intent") if data.get("intent") in INTENTS else ""
    out["reply_language"] = languages.normalize(data.get("reply_language")) or ""
    return out


# ------------------------------------------------------------------------------------------------ fallback wording
# The built-in questions, in English and Hindi (the two languages most users talk to the app in). Any other language
# uses English when the chat model is unavailable.

HINDI = {
    "greet": "नमस्ते! बताइए क्या चाहिए। मैं पास के सबसे अच्छे लोगों को ढूँढकर, उनसे बात करके, सबसे अच्छा दाम या समय पक्का कर दूँगा।",
    "location_route": "ठीक है: {label}। कहाँ से कहाँ जाना है?",
    "location": "ठीक है: {label}। किस इलाके में चाहिए?",
    "when_appt": "कब का समय चाहिए?",
    "when": "{label} कब चाहिए?",
    "details": "कोई ख़ास बात जो उन्हें पता होनी चाहिए? (या 'छोड़ो' बोलिए)",
    "budget": "आपका बजट क्या है? अंदाज़ा और ज़्यादा से ज़्यादा कितना, जैसे 'लगभग {target}, ज़्यादा से ज़्यादा {high}'। पता नहीं तो 'सुझाओ' बोलिए।",
    "confirm": "बढ़िया। {summary}। मैं पास के {n} सबसे अच्छे लोगों को {language} में फ़ोन करूँ?",
    "go": "ठीक है, ढूँढ रहा हूँ और फ़ोन लगा रहा हूँ।",
    "change": "ठीक है, क्या बदलना है?",
}
