"""Gemini (Google AI Studio): one API key, a different model for each job.

Roles and their model ladders (config.GEMINI_*_MODELS):
  * chat      the voice concierge that talks with the user and works out what they need
  * parse     brief parsing and Google-Maps-grounded search (GEMINI_MODEL)
  * localize  the linguist: the user's job details in the call language
  * stt       speech-to-text for browsers without built-in speech recognition
  * tts       a natural voice for languages the browser can't speak

The free tier's daily quota is per model, so separate models keep the jobs from starving each other. Every caller has a
deterministic fallback: if GEMINI_API_KEY is unset, a model is rate-limited or the network is slow, everything works.
"""

from __future__ import annotations

import base64
import copy
import json
import logging
import re
import struct
import time
from typing import Optional

import httpx

from app import config

log = logging.getLogger("llm")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-3.5-flash-lite")
_dead_models: set[str] = set()
# model -> unix time until which it is rate-limited (the free tier allows ~20 requests/day/model)
_cooldown: dict[str, float] = {}
_last_used: dict[str, str] = {}   # role -> model that answered last


def _retry_seconds(response: httpx.Response, default: float) -> float:
    try:
        for detail in response.json()["error"].get("details", []):
            delay = str(detail.get("retryDelay", ""))
            if delay.endswith("s"):
                return min(float(delay[:-1]), 24 * 3600)
    except (ValueError, KeyError, TypeError):
        pass
    return default


def ladder(role: str) -> list[str]:
    s = config.settings
    lists = {"chat": s.gemini_chat_models, "localize": s.gemini_localize_models, "stt": s.gemini_stt_models,
             "tts": s.gemini_tts_models}
    if role in lists:
        models = [m.strip() for m in lists[role].split(",") if m.strip()]
    else:
        models = [s.gemini_model, *FALLBACK_MODELS]
    return list(dict.fromkeys(models))


def status() -> dict:
    now = time.time()
    cooling = {m: int(t - now) for m, t in _cooldown.items() if t > now}
    roles = {role: next((m for m in ladder(role) if m not in _dead_models and _cooldown.get(m, 0) <= now), None)
             for role in ("chat", "parse", "localize", "stt", "tts")}
    return {"enabled": enabled(), "rate_limited_models": cooling, "unavailable_models": sorted(_dead_models),
            "models": roles, "last_used": dict(_last_used)}


def enabled() -> bool:
    s = config.settings
    return bool(s.gemini_api_key and s.llm_enabled)


def _adapt(model: str, body: dict) -> dict:
    """The same request, shaped for what each model family accepts."""
    b = copy.deepcopy(body)
    gc = b.setdefault("generationConfig", {})
    if model.startswith("gemma"):
        # Gemma takes no system instruction, JSON mode or thinking settings: fold the system text into the prompt
        system = b.pop("systemInstruction", None)
        if system:
            b["contents"][0]["parts"].insert(0, {"text": system["parts"][0]["text"] + "\n\n"})
        gc.pop("responseMimeType", None)
        gc.pop("thinkingConfig", None)
    elif "tts" in model or not model.startswith("gemini-2.5"):
        gc.pop("thinkingConfig", None)        # 2.0 doesn't think; 3.x uses thinking levels; TTS models don't
    elif "responseModalities" not in gc:
        gc.setdefault("thinkingConfig", {"thinkingBudget": 0})   # no thinking pause: answers are short
    return b


async def _call(body: dict, timeout: float, role: str = "parse") -> Optional[dict]:
    """POST to the first usable model for the role; returns the first candidate (or None)."""
    if not enabled():
        return None
    s = config.settings
    now = time.time()
    models = [m for m in ladder(role) if m not in _dead_models and _cooldown.get(m, 0) <= now]
    if not models:
        return None  # every model is rate-limited: answer instantly so callers use their fallback
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models:
            request = _adapt(model, body)
            for attempt in range(2):
                try:
                    r = await client.post(API.format(model=model), json=request,
                                          headers={"x-goog-api-key": s.gemini_api_key})
                except httpx.HTTPError as exc:
                    log.info("gemini %s request failed: %s", model, exc)
                    return None  # network/timeout: do not burn the clock on retries
                if r.status_code == 400 and attempt == 0 and re.search(r"thinking|responseMimeType|system", r.text,
                                                                        re.I):
                    # a setting this model doesn't take: drop it and try the same model once more
                    gc = request.get("generationConfig", {})
                    gc.pop("thinkingConfig", None)
                    if "responseMimeType" in r.text:
                        gc.pop("responseMimeType", None)
                    continue
                break
            if r.status_code == 404:
                _dead_models.add(model)
                continue
            if r.status_code >= 400:
                log.info("gemini %s -> %s %s", model, r.status_code, r.text[:200])
                if r.status_code in (429, 503, 500):
                    _cooldown[model] = time.time() + _retry_seconds(r, 60 if r.status_code == 429 else 20)
                    continue
                if r.status_code in (400, 403):
                    continue   # this model refuses this request (e.g. no audio support): try the next one
                return None
            try:
                candidate = r.json()["candidates"][0]
            except (KeyError, IndexError, ValueError):
                continue
            _last_used[role] = model
            return candidate
    return None


def _text(candidate: Optional[dict]) -> Optional[str]:
    try:
        parts = candidate["content"]["parts"]
    except (TypeError, KeyError):
        return None
    return "".join(p.get("text", "") for p in parts).strip() or None


def parse_json(raw: Optional[str]) -> Optional[dict]:
    """The first JSON object in a reply (models without JSON mode wrap it in prose or ``` fences)."""
    if not raw:
        return None
    text = re.sub(r"^```(?:json)?|```$", "", raw.strip()).strip()
    for candidate in (text, (re.search(r"\{.*\}", text, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            continue
    return None


async def generate(prompt: str, *, system: str = "", max_tokens: int = 160, temperature: float = 0.8,
                   json_mode: bool = False, timeout: float = 4.0, role: str = "parse") -> Optional[str]:
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    return _text(await _call(body, timeout, role))


# ------------------------------------------------------------------------------------------------ voice

def wav(pcm: bytes, rate: int = 24000, channels: int = 1, width: int = 2) -> bytes:
    """Raw 16-bit PCM (what Gemini TTS returns) -> a playable WAV file."""
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * channels * width, channels * width, width * 8)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


async def speak(text: str) -> Optional[bytes]:
    """Text -> WAV with a Gemini TTS model (for languages the user's browser has no voice for)."""
    body = {"contents": [{"role": "user", "parts": [{"text": text[:600]}]}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                "prebuiltVoiceConfig": {"voiceName": config.settings.gemini_tts_voice}}}}}
    candidate = await _call(body, timeout=25, role="tts")
    try:
        part = next(p for p in candidate["content"]["parts"] if p.get("inlineData"))
        pcm = base64.b64decode(part["inlineData"]["data"])
        rate = int((re.search(r"rate=(\d+)", part["inlineData"].get("mimeType", "")) or [0, 24000])[1])
    except (TypeError, KeyError, StopIteration, ValueError):
        return None
    return wav(pcm, rate) if pcm else None


async def transcribe(audio: bytes, mime: str, language_hint: str = "") -> Optional[dict]:
    """Speech -> {text, language} for browsers without speech recognition (Firefox, some iPhones)."""
    from app import languages
    hint = languages.get(language_hint).name if language_hint else "an Indian language or English"
    prompt = (f"Transcribe this short voice message exactly as spoken. The speaker is in India, most likely speaking "
              f"{hint}. Write each language in its own script (Hindi and Marathi in Devanagari, English in Latin "
              f"letters, and so on); keep English words the speaker used in English. Return ONLY JSON: "
              f'{{"text": "<transcript>", "language": "<ISO 639-1 code>"}}. If there is no speech, text is "".')
    body = {"contents": [{"role": "user", "parts": [
        {"inlineData": {"mimeType": mime, "data": base64.b64encode(audio).decode()}}, {"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 400, "temperature": 0, "responseMimeType": "application/json"}}
    data = parse_json(_text(await _call(body, timeout=20, role="stt")))
    if not data or not isinstance(data.get("text"), str):
        return None
    return {"text": data["text"].strip()[:600], "language": languages.normalize(data.get("language")) or ""}


async def generate_with_maps(prompt: str, lat: Optional[float] = None,
                             lng: Optional[float] = None) -> Optional[tuple[str, list[dict]]]:
    """Gemini with Grounding with Google Maps. Returns (text, [{title, uri}]) or None."""
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "tools": [{"googleMaps": {}}],
        "generationConfig": {"maxOutputTokens": 1200, "temperature": 0.1},
    }
    if lat is not None and lng is not None:
        body["toolConfig"] = {"retrievalConfig": {"latLng": {"latitude": lat, "longitude": lng}}}
    candidate = await _call(body, timeout=20)
    text = _text(candidate)
    if not text:
        return None
    chunks = [c.get("maps", {}) for c in (candidate.get("groundingMetadata") or {}).get("groundingChunks", [])
              if isinstance(c, dict) and c.get("maps")]
    return text, chunks


def style(language: str) -> str:
    """How a rewrite must be written: colloquial, in the language's own script, no English words."""
    from app import languages
    if language == "en":
        return "simple Indian English"
    lang = languages.get(language)
    return f"colloquial spoken {lang.name} in {lang.script} script, with no English words and no Latin letters"


def _pure(text: str, language: str) -> bool:
    from app import languages
    return language == "en" or not (languages.latin_words(text) or languages.foreign_letters(text, language))


def _amounts_ok(text: str, must_include: list[int]) -> bool:
    """Every required amount survives, and no new 3+ digit number (an invented price) appears."""
    numbers = set(re.findall(r"\d+", text.replace("₹", "").replace(",", "")))
    required = {str(a) for a in must_include}
    return required <= numbers and not {n for n in numbers - required if len(n) >= 3}


def _history_block(history: Optional[list[dict]]) -> str:
    if not history:
        return ""
    who = {"agent": "Booking assistant", "vendor": "Vendor"}
    lines = [f"{who.get(h.get('role'), 'Note')}: {h.get('text', '')}" for h in history[-4:] if h.get("text")]
    return "Conversation so far:\n" + "\n".join(lines) + "\n"


def _clean(text: Optional[str]) -> str:
    return (text or "").strip().strip('"').splitlines()[0][:260] if text else ""


async def vendor_line(persona: str, language: str, intent: str, fallback: str,
                      must_include: Optional[list[int]] = None, history: Optional[list[dict]] = None) -> str:
    """Rephrase a simulated vendor's reply so it follows the conversation. Numbers are fixed by code: the rewrite
    is rejected unless every required amount survives and no new price appears."""
    if not enabled():
        return fallback
    amounts = list(must_include or [])
    prompt = (f"You are {persona}, a busy small local business owner in India, answering a customer's phone call.\n"
              f"{_history_block(history)}"
              f"Say your next reply in {style(language)}: one or two short, natural, slightly "
              f"informal spoken sentences that respond to what was just said. No emojis, no quotes, no narration.\n"
              f"What your reply must mean: {intent}\nReference wording: {fallback}\n"
              + (f"Use exactly these rupee amounts as digits: {', '.join(map(str, amounts))}. No other amounts.\n"
                 if amounts else "Do not mention any rupee amount.\n"))
    text = _clean(await generate(prompt, max_tokens=90, temperature=0.8, timeout=3.5, role="chat"))
    return text if text and _amounts_ok(text, amounts) and _pure(text, language) else fallback


async def agent_line(language: str, fallback: str, history: Optional[list[dict]] = None) -> str:
    """Natural wording for the simulated negotiator's line, mirroring what the live Bolna LLM does with say_this.
    Every amount in the deterministic line must survive, and nothing new may be added."""
    if not enabled():
        return fallback
    amounts = [int(n) for n in re.findall(r"\d{3,}", fallback.replace(",", ""))]
    prompt = (f"You are a friendly booking assistant on a phone call with a local vendor in India.\n"
              f"{_history_block(history)}"
              f"Say this next line naturally in {style(language)}, as one short spoken turn "
              f"that flows from the conversation: \"{fallback}\"\n"
              f"Keep the exact meaning and every rupee amount (write amounts as ₹ with digits). Do not add any other "
              f"price, promise, question or detail. No quotes, no narration.")
    text = _clean(await generate(prompt, max_tokens=110, temperature=0.6, timeout=3.0, role="chat"))
    return text if text and _amounts_ok(text, amounts) and _pure(text, language) else fallback


BRIEF_SCHEMA_HINT = """Return JSON with keys:
category: one of "cab_outstation", "mini_truck", "ac_repair"
summary: short English summary (max 12 words)
pickup, drop, when: strings ("" if unknown)
target_price, walkaway_ceiling: integers in rupees (0 if not said)
est_km: estimated total billable km for the whole job as a number (0 if local/unknown)
days: integer number of days (1 if unknown)
language_hint: "kn" | "hi" | "ta" | "mr" | "en" (vendor language most likely in that area)"""


async def parse_brief(text: str) -> Optional[dict]:
    raw = await generate(f"Buyer request (may be Hindi, Hinglish, Kannada or English):\n{text}\n\n{BRIEF_SCHEMA_HINT}",
                         system="You convert Indian local-service requests into structured JSON. Be precise.",
                         max_tokens=300, temperature=0.1, json_mode=True, timeout=6)
    return parse_json(raw)
