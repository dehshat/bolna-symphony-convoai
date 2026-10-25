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
from typing import Optional

import httpx

from app import config

log = logging.getLogger("llm")

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
FALLBACK_MODELS = ("gemini-2.5-flash", "gemini-3.5-flash-lite", "gemini-flash-latest")
_dead_models: set[str] = set()


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
    models = [s.gemini_model] + [m for m in FALLBACK_MODELS if m != s.gemini_model]
    async with httpx.AsyncClient(timeout=timeout) as client:
        for model in models:
            if model in _dead_models:
                continue
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


async def vendor_line(persona: str, language: str, intent: str, fallback: str,
                      must_include: Optional[list[int]] = None) -> str:
    """Rephrase a simulated vendor's line. The rewrite is rejected unless every required amount survives."""
    if not enabled():
        return fallback
    lang = {"kn": "Kannada-English mix written in Roman script (Bengaluru style)",
            "hi": "Hinglish (Hindi-English mix) in Roman script", "en": "Indian English",
            "ta": "Tamil-English mix in Roman script", "mr": "Marathi-English mix in Roman script"}.get(language, "Hinglish")
    amounts = ", ".join(str(a) for a in (must_include or []))
    prompt = (f"You are {persona}, a small local business owner in Bengaluru answering a phone call.\n"
              f"Rewrite this reply so it sounds like a real, busy, slightly informal vendor on the phone, in {lang}. "
              f"One or two short sentences, no emojis, no quotes.\n"
              f"Meaning to keep: {intent}\nOriginal: {fallback}\n"
              + (f"You MUST keep these rupee amounts as digits exactly: {amounts}.\n" if amounts else
                 "Do not mention any rupee amount.\n"))
    text = await generate(prompt, max_tokens=90, temperature=0.9, timeout=3.5)
    if not text:
        return fallback
    text = text.strip().strip('"').splitlines()[0][:240]
    normalized = text.replace("₹", "").replace(",", "")
    for amount in must_include or []:
        if str(amount) not in normalized:
            return fallback
    if not must_include and re.search(r"\d{3,}", normalized):
        return fallback
    return text


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
