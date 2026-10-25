"""Optional Gemini (Google AI Studio) text model.

Used for two things only, both with deterministic fallbacks:
  1. giving simulated vendors natural, varied wording (the numbers are fixed by code and verified), and
  2. turning a free-text or spoken buyer brief into a structured ServiceRequest.
If GEMINI_API_KEY is unset or the API is slow, everything still works.
"""

from __future__ import annotations

import json
import logging
import re
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


def _retry_seconds(response: httpx.Response, default: float) -> float:
    try:
        for detail in response.json()["error"].get("details", []):
            delay = str(detail.get("retryDelay", ""))
            if delay.endswith("s"):
                return min(float(delay[:-1]), 24 * 3600)
    except (ValueError, KeyError, TypeError):
        pass
    return default


def status() -> dict:
    now = time.time()
    cooling = {m: int(t - now) for m, t in _cooldown.items() if t > now}
    return {"enabled": enabled(), "rate_limited_models": cooling}


def enabled() -> bool:
    s = config.settings
    return bool(s.gemini_api_key and s.llm_enabled)


async def generate(prompt: str, *, system: str = "", max_tokens: int = 160, temperature: float = 0.8,
                   json_mode: bool = False, timeout: float = 4.0) -> Optional[str]:
    if not enabled():
        return None
    s = config.settings
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    now = time.time()
    models = [m for m in [s.gemini_model] + [m for m in FALLBACK_MODELS if m != s.gemini_model]
              if m not in _dead_models and _cooldown.get(m, 0) <= now]
    if not models:
        return None  # every model is rate-limited: answer instantly so callers use their fallback
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models:
            try:
                r = await client.post(API.format(model=model), json=body,
                                      headers={"x-goog-api-key": s.gemini_api_key})
            except httpx.HTTPError as exc:
                log.info("gemini %s request failed: %s", model, exc)
                return None  # network/timeout: do not burn the demo clock on retries
            if r.status_code == 404:
                _dead_models.add(model)
                continue
            if r.status_code >= 400:
                log.info("gemini %s -> %s %s", model, r.status_code, r.text[:200])
                if r.status_code in (429, 503):
                    _cooldown[model] = time.time() + _retry_seconds(r, 60 if r.status_code == 429 else 20)
                    continue
                return None
            try:
                parts = r.json()["candidates"][0]["content"]["parts"]
                return "".join(p.get("text", "") for p in parts).strip() or None
            except (KeyError, IndexError, ValueError):
                return None
    return None


def _digits(amount: int) -> set[str]:
    return {str(amount), f"{amount:,}", f"{amount:,}".replace(",", "")}


ROMAN_STYLE = {
    "kn": "Kannada-English mix written in Roman script (Bengaluru style)",
    "hi": "Hinglish (Hindi-English mix) in Roman script",
    "en": "Indian English",
    "ta": "Tamil-English mix in Roman script",
    "mr": "Marathi-English mix in Roman script",
}


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
    prompt = (f"You are {persona}, a busy small local business owner in Bengaluru, answering a customer's phone call.\n"
              f"{_history_block(history)}"
              f"Say your next reply in {ROMAN_STYLE.get(language, 'Hinglish')}: one or two short, natural, slightly "
              f"informal spoken sentences that respond to what was just said. No emojis, no quotes, no narration.\n"
              f"What your reply must mean: {intent}\nReference wording: {fallback}\n"
              + (f"Use exactly these rupee amounts as digits: {', '.join(map(str, amounts))}. No other amounts.\n"
                 if amounts else "Do not mention any rupee amount.\n"))
    text = _clean(await generate(prompt, max_tokens=90, temperature=0.8, timeout=3.5))
    return text if text and _amounts_ok(text, amounts) else fallback


async def agent_line(language: str, fallback: str, history: Optional[list[dict]] = None) -> str:
    """Natural wording for the simulated negotiator's line, mirroring what the live Bolna LLM does with say_this.
    Every amount in the deterministic line must survive, and nothing new may be added."""
    if not enabled():
        return fallback
    amounts = [int(n) for n in re.findall(r"\d{3,}", fallback.replace(",", ""))]
    prompt = (f"You are a friendly booking assistant on a phone call with a local vendor in Bengaluru.\n"
              f"{_history_block(history)}"
              f"Say this next line naturally in {ROMAN_STYLE.get(language, 'Hinglish')}, as one short spoken turn "
              f"that flows from the conversation: \"{fallback}\"\n"
              f"Keep the exact meaning and every rupee amount (write amounts as ₹ with digits). Do not add any other "
              f"price, promise, question or detail. No quotes, no narration.")
    text = _clean(await generate(prompt, max_tokens=110, temperature=0.6, timeout=3.0))
    return text if text and _amounts_ok(text, amounts) else fallback


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
    if not raw:
        return None
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", raw.strip()))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
