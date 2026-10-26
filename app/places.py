"""Find nearby service providers and rank them by rating and popularity.

Sources, tried in order until one returns results:
  1. Google Places API (New) Text Search      needs GOOGLE_MAPS_API_KEY (Maps Platform key, billing enabled)
  2. Gemini + "Grounding with Google Maps"    uses GEMINI_API_KEY (free tier is ~20 requests/day/model)
  3. OpenStreetMap (Nominatim + Overpass)     free, no key, no ratings
  4. Demo listings                            always works; clearly labelled, for testing the call flow

Safety: unless CALL_REAL_NUMBERS=true, every provider is dialled on the test phone (TEST_PHONE_OVERRIDE, else
LIVE_TEST_PHONE). The real listed number is still shown so you can see what would be called.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
from dataclasses import asdict, dataclass, field
from typing import Optional

import httpx

from app import config, llm, services
from app.services import ServiceType

log = logging.getLogger("places")

PRIOR_RATING = 4.0     # Bayesian prior: a place with few reviews is pulled towards this
PRIOR_WEIGHT = 25      # ...as if it had this many extra reviews


@dataclass
class Provider:
    id: str
    name: str
    phone: str = ""                 # number as listed (may be empty)
    phone_local: str = ""           # same number in local format, e.g. "099720 19812"
    dial_phone: str = ""            # number that will actually be called
    website: str = ""
    hours: list = field(default_factory=list)   # ["Monday: 9:00 AM – 8:00 PM", ...]
    category: str = ""
    rating: Optional[float] = None
    reviews: int = 0
    address: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    distance_km: Optional[float] = None
    maps_uri: str = ""
    open_now: Optional[bool] = None
    source: str = ""
    score: float = 0.0
    test_number: bool = False
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------- ranking + phones

def popularity_score(rating: Optional[float], reviews: int) -> float:
    """Bayesian average: ★4.9 from 8 reviews ranks below ★4.6 from 2,000."""
    if rating is None:
        return 0.0
    v = max(reviews or 0, 0)
    return (v / (v + PRIOR_WEIGHT)) * rating + (PRIOR_WEIGHT / (v + PRIOR_WEIGHT)) * PRIOR_RATING


def rank(providers: list[Provider]) -> list[Provider]:
    for p in providers:
        p.score = round(popularity_score(p.rating, p.reviews), 3)
    return sorted(providers, key=lambda p: (-p.score, -(p.reviews or 0),
                                            p.distance_km if p.distance_km is not None else 999))


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw or "")
    if not digits:
        return ""
    if digits.startswith("+"):
        return digits
    digits = digits.lstrip("0")
    if len(digits) == 10:
        return "+91" + digits
    if len(digits) == 12 and digits.startswith("91"):
        return "+" + digits
    return "+91" + digits if len(digits) >= 8 else ""


def assign_dial_numbers(providers: list[Provider]) -> list[Provider]:
    s = config.settings
    test_phone = s.test_phone_override or s.live_test_phone
    for p in providers:
        p.phone = normalize_phone(p.phone)
        if s.call_real_numbers and p.phone:
            p.dial_phone, p.test_number = p.phone, False
        else:
            p.dial_phone, p.test_number = test_phone, True
    return providers


def haversine_km(lat1, lng1, lat2, lng2) -> Optional[float]:
    if None in (lat1, lng1, lat2, lng2):
        return None
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * r * math.asin(math.sqrt(a)), 1)


NEAR_ME_RE = re.compile(r"^\W*(?:near(?:by| me| you)?|around me|here|(?:my|your) (?:current |live )?location|"
                        r"current location|use my (?:current |live )?location)\W*$", re.I)


def is_near_me(location: str) -> bool:
    """True when the location means "wherever I am right now" rather than a named area."""
    return not (location or "").strip() or bool(NEAR_ME_RE.match(location.strip()))


async def reverse_geocode(lat: float, lng: float) -> str:
    """Live GPS position -> "Koramangala, Bengaluru" (OpenStreetMap Nominatim; no Google Geocoding API needed)."""
    try:
        async with httpx.AsyncClient(timeout=6, headers=UA) as client:
            r = await client.get("https://nominatim.openstreetmap.org/reverse",
                                 params={"lat": lat, "lon": lng, "format": "json", "zoom": 16, "addressdetails": 1})
        addr = (r.json() if r.status_code < 400 else {}).get("address") or {}
    except (httpx.HTTPError, ValueError):
        return ""
    area = next((addr[k] for k in ("suburb", "neighbourhood", "quarter", "city_district", "village", "town")
                 if addr.get(k)), "")
    city = next((addr[k] for k in ("city", "town", "state_district", "county") if addr.get(k)), "")
    if city == "Bangalore":
        city = "Bengaluru"
    return ", ".join(dict.fromkeys(x for x in (area, city) if x))


def _pid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


# ---------------------------------------------------------------------------- 1. Google Places (New)

async def google_places(svc: ServiceType, location: str, lat=None, lng=None, limit: int = 8) -> list[Provider]:
    key = config.settings.google_maps_api_key
    if not key:
        return []
    what = svc.maps_query or svc.label
    gps = lat is not None and lng is not None
    near_me = gps and is_near_me(location)
    body: dict = {"textQuery": what if near_me else f"{what} in {location}", "maxResultCount": min(limit * 2, 20),
                  "languageCode": "en", "regionCode": "IN"}
    if gps:
        # "near me": stay close to the live position; a named area: just prefer results near the user
        body["locationBias"] = {"circle": {"center": {"latitude": lat, "longitude": lng},
                                           "radius": 5000.0 if near_me else 8000.0}}
    # Every contact field Google has. All of these are in the same (Enterprise) billing tier as rating + phone.
    fields = ",".join(f"places.{f}" for f in ("id", "displayName", "formattedAddress", "rating", "userRatingCount",
                                               "nationalPhoneNumber", "internationalPhoneNumber", "location",
                                               "googleMapsUri", "websiteUri", "currentOpeningHours.openNow",
                                               "regularOpeningHours.weekdayDescriptions", "primaryTypeDisplayName",
                                               "businessStatus"))
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post("https://places.googleapis.com/v1/places:searchText", json=body,
                                  headers={"X-Goog-Api-Key": key, "X-Goog-FieldMask": fields})
        if r.status_code >= 400:
            log.warning("google places %s: %s", r.status_code, r.text[:300])
            return []
        places = r.json().get("places", [])
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("google places failed: %s", exc)
        return []
    out = []
    for p in places:
        if p.get("businessStatus") not in (None, "OPERATIONAL"):
            continue
        loc = p.get("location") or {}
        out.append(Provider(
            id=p.get("id") or _pid(p.get("displayName")), name=(p.get("displayName") or {}).get("text", "Unknown"),
            phone=p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber") or "",
            phone_local=p.get("nationalPhoneNumber", ""), website=p.get("websiteUri", ""),
            hours=list((p.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []),
            category=(p.get("primaryTypeDisplayName") or {}).get("text", ""),
            rating=p.get("rating"), reviews=int(p.get("userRatingCount") or 0), address=p.get("formattedAddress", ""),
            lat=loc.get("latitude"), lng=loc.get("longitude"), maps_uri=p.get("googleMapsUri", ""),
            open_now=(p.get("currentOpeningHours") or {}).get("openNow"), source="Google Maps"))
    return out


# ---------------------------------------------------------------------------- 2. Gemini + Google Maps grounding

async def gemini_maps(svc: ServiceType, location: str, lat=None, lng=None, limit: int = 8) -> list[Provider]:
    if not llm.enabled():
        return []
    where = "my current location" if is_near_me(location) else f"{location}, India"
    prompt = (f"Using Google Maps, list up to {limit} real {svc.maps_query or svc.label} businesses in or near "
              f"{where}. Return ONLY a JSON array, each item: "
              '{"name": str, "address": str, "rating": number or null, "reviews": integer or null, '
              '"phone": str or null}. Use only data from Google Maps; use null when unknown.')
    raw = await llm.generate_with_maps(prompt, lat, lng)
    if not raw:
        return []
    text, chunks = raw
    match = re.search(r"\[.*\]", text, re.S)
    try:
        items = json.loads(match.group(0)) if match else []
    except json.JSONDecodeError:
        items = []
    uris = {c.get("title", "").lower(): c.get("uri", "") for c in chunks}
    out = []
    for it in items[:limit]:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        name = str(it["name"])[:80]
        try:
            rating = float(it["rating"]) if it.get("rating") is not None else None
        except (TypeError, ValueError):
            rating = None
        try:
            reviews = int(it.get("reviews") or 0)
        except (TypeError, ValueError):
            reviews = 0
        out.append(Provider(id=_pid(name, it.get("address")), name=name, phone=str(it.get("phone") or ""),
                            rating=rating, reviews=reviews, address=str(it.get("address") or "")[:160],
                            maps_uri=uris.get(name.lower(), ""), source="Google Maps (via Gemini)"))
    return out


# ---------------------------------------------------------------------------- 3. OpenStreetMap

UA = {"User-Agent": "SwarmNegotiator/1.0 (hackathon prototype)"}


async def geocode_osm(location: str) -> Optional[tuple[float, float]]:
    try:
        async with httpx.AsyncClient(timeout=8, headers=UA) as client:
            r = await client.get("https://nominatim.openstreetmap.org/search",
                                 params={"q": location if "india" in location.lower() else f"{location}, India",
                                         "format": "json", "limit": 1})
        data = r.json() if r.status_code < 400 else []
        if data:
            return float(data[0]["lat"]), float(data[0]["lon"])
    except (httpx.HTTPError, ValueError, KeyError):
        pass
    return None


async def openstreetmap(svc: ServiceType, location: str, lat=None, lng=None, limit: int = 8) -> list[Provider]:
    if lat is None or lng is None:
        point = await geocode_osm(location)
        if not point:
            return []
        lat, lng = point
    filters = "".join(f'nwr["{k}"="{v}"](around:7000,{lat},{lng});' for k, v in svc.osm_tags)
    word = re.escape((svc.maps_query or svc.label).split()[0])
    filters += f'nwr["name"~"{word}",i](around:7000,{lat},{lng});'
    query = f"[out:json][timeout:12];({filters});out center 60;"
    try:
        async with httpx.AsyncClient(timeout=15, headers=UA) as client:
            r = await client.post("https://overpass-api.de/api/interpreter", data={"data": query})
        elements = r.json().get("elements", []) if r.status_code < 400 else []
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("overpass failed: %s", exc)
        return []
    out, seen = [], set()
    for e in elements:
        tags = e.get("tags", {})
        name = tags.get("name")
        if not name or name in seen:
            continue
        seen.add(name)
        plat = e.get("lat") or (e.get("center") or {}).get("lat")
        plng = e.get("lon") or (e.get("center") or {}).get("lon")
        addr = ", ".join(x for x in (tags.get("addr:housenumber"), tags.get("addr:street"), tags.get("addr:suburb"),
                                     tags.get("addr:city")) if x)
        out.append(Provider(id=f"osm-{e.get('type')}-{e.get('id')}", name=name,
                            phone=tags.get("phone") or tags.get("contact:phone") or "", address=addr,
                            lat=plat, lng=plng, distance_km=haversine_km(lat, lng, plat, plng),
                            source="OpenStreetMap (no ratings)"))
    out.sort(key=lambda p: (0 if p.phone else 1, p.distance_km or 99))
    return out[:limit]


# ---------------------------------------------------------------------------- 4. Demo listings

DEMO_PREFIX = ("Sri Lakshmi", "Namma", "Royal", "Green Leaf", "City", "Sai Krupa", "Annapoorna", "Metro", "Prime",
               "Ganesh", "Sunrise", "Classic")


async def demo(svc: ServiceType, location: str, lat=None, lng=None, limit: int = 6) -> list[Provider]:
    rng = random.Random(f"{svc.key}:{svc.label}:{location}".lower())
    area = ("Bengaluru" if is_near_me(location) else location).split(",")[0].strip() or "Bengaluru"
    area = area if any(c.isupper() for c in area) else area.title()
    noun = svc.label.split("/")[0].strip().title()
    names = rng.sample(DEMO_PREFIX, k=min(limit, len(DEMO_PREFIX)))
    out = []
    for i, prefix in enumerate(names):
        rating = round(rng.uniform(3.7, 4.9), 1)
        reviews = int(rng.choice([12, 38, 85, 160, 240, 410, 760, 1250, 2300]) * rng.uniform(0.8, 1.2))
        out.append(Provider(id=_pid("demo", svc.key, prefix, area), name=f"{prefix} {noun}",
                            phone=f"+9180{rng.randint(20000000, 99999999)}", rating=rating, reviews=reviews,
                            address=f"{rng.randint(1, 40)}th Cross, {area}, Bengaluru",
                            distance_km=round(rng.uniform(0.4, 6.5), 1), open_now=rng.random() > 0.2,
                            source="Demo listing (add GOOGLE_MAPS_API_KEY for real data)"))
    return out


# ---------------------------------------------------------------------------- orchestration

BACKENDS = (("google_places", google_places), ("gemini_maps", gemini_maps), ("openstreetmap", openstreetmap),
            ("demo", demo))


async def search(service_key: str, service_label: str, location: str, lat: Optional[float] = None,
                 lng: Optional[float] = None, limit: int = 8) -> dict:
    svc = services.get(service_key, service_label)
    if service_label and svc.key == "other":
        svc = services.get("other", service_label)
    if is_near_me(location) and (lat is None or lng is None):
        location = "Bengaluru"      # "near me" but the browser gave no position
    tried = []
    for name, backend in BACKENDS:
        try:
            found = await backend(svc, location, lat, lng, limit)
        except Exception as exc:  # one broken source must never break the search
            log.warning("provider backend %s crashed: %s", name, exc)
            found = []
        tried.append({"source": name, "results": len(found)})
        if found:
            for p in found:
                if p.distance_km is None:
                    p.distance_km = haversine_km(lat, lng, p.lat, p.lng)
            ranked = assign_dial_numbers(rank(found))[:limit]
            return {"providers": [p.to_dict() for p in ranked], "source": name, "tried": tried,
                    "service": {"key": svc.key, "label": svc.label, "goal": svc.goal},
                    "test_mode": not config.settings.call_real_numbers,
                    "test_phone": config.settings.test_phone_override or config.settings.live_test_phone}
    return {"providers": [], "source": None, "tried": tried, "service": {"key": svc.key, "label": svc.label}}
