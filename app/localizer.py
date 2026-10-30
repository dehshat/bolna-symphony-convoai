"""Linguist agent: puts the user's own words (the job details, the area) into the call's language before dialling.

The catalogue already has native names for every service and the lexicon covers every negotiation line, so calls work
without this. What only an LLM can do is translate free text ("kitchen tap leaking under the sink") and transliterate
place names ("Koramangala" -> "ಕೋರಮಂಗಲ") so the opening line is entirely in the provider's language. One Gemini request
per job and language; anything that comes back with Latin letters or in the wrong script is dropped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

from app import languages, lexicon, llm
from app.pricing import ServiceRequest

log = logging.getLogger("localizer")

FIELDS = ("service", "details", "place", "pickup", "drop", "when")
_cache: dict[str, dict] = {}


def _key(req: ServiceRequest, language: str) -> str:
    raw = "|".join([language, req.service_label, req.details, req.location, req.pickup, req.drop, req.when])
    return hashlib.sha1(raw.encode()).hexdigest()


def _clean(value: object, language: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .,;।")
    if not text or len(text) > limit or languages.latin_words(text) or languages.foreign_letters(text, language):
        return ""
    return text


async def localize_job(req: ServiceRequest, language: str) -> dict:
    """{service, details, place, pickup, drop, when} in the call language, only the fields that passed the checks."""
    if language not in lexicon.NATIVE or not llm.enabled():
        return {}
    key = _key(req, language)
    if key in _cache:
        return _cache[key]
    lang = languages.get(language)
    source = {"service": req.service_label, "details": req.details[:200], "place": (req.location or "").split(",")[0],
              "pickup": (req.pickup or "").split(",")[0] if req.drop else "", "drop": (req.drop or "").split(",")[0],
              "when": req.when}
    prompt = (f"Translate these fields into natural, spoken {lang.name} ({lang.script} script) for a phone call to a "
              f"local service provider in India. Return ONLY JSON with the same keys.\n"
              f"Rules: no English words and no Latin letters at all; transliterate place names into {lang.script} "
              f"script; keep numbers as digits; 'service' is what you would call the person or shop (2-4 words); "
              f"'details' is at most 12 words; 'when' is how you would say the time; an empty input stays \"\".\n"
              f"{json.dumps(source, ensure_ascii=False)}")
    raw = await llm.generate(prompt, max_tokens=300, temperature=0.2, json_mode=True, timeout=7, role="localize")
    out: dict = {}
    try:
        data = json.loads(re.sub(r"^```(json)?|```$", "", (raw or "").strip())) if raw else {}
    except json.JSONDecodeError:
        data = {}
    if isinstance(data, dict):
        for field in FIELDS:
            if source.get(field):
                value = _clean(data.get(field), language, 120 if field == "details" else 60)
                if value:
                    out[field] = value
    if out:
        _cache[key] = out
    log.info("localized job into %s: %s", lang.name, sorted(out))
    return out
