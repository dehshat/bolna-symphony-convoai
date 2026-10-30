"""Single-origin FastAPI app: static war-room UI + REST + WebSocket + Bolna webhooks in one process.

Run:  uvicorn app.main:app --host 0.0.0.0 --port $PORT      (one worker: the auction state is in memory)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import os
import re
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import (bolna_client, briefs, config, intake, languages, llm, payments, places, ratelimit, scenarios, services,
                 state)
from app.auction_engine import GuardrailViolation
from app.pricing import ServiceRequest
from app.webhooks import router as webhook_router
from app.ws import broadcaster

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

log = logging.getLogger("main")

# Shown in ⚙︎ Setup so you can see from your phone whether Bolna provisioning worked
BOLNA_SYNC: dict = {"state": "not_configured", "detail": "Set BOLNA_API_KEY to enable hybrid/live calls"}


async def auto_provision_bolna() -> None:
    """On boot: point the Bolna agents' tools and webhook at this deployment (create them if missing)."""
    s = config.settings
    if not s.bolna_api_key or not s.auto_sync_bolna:
        return
    if not s.public_base_url:
        BOLNA_SYNC.update(state="waiting_for_domain",
                          detail="Generate a public domain (Railway → Settings → Networking) or set PUBLIC_BASE_URL")
        return
    BOLNA_SYNC.update(state="syncing", detail=f"Provisioning agents for {s.public_base_url}")
    try:
        result = await bolna_client.sync_agents(None, s.public_base_url)
        partial = result.get("errors")
        BOLNA_SYNC.update(state="ok" if not partial else "partial",
                          detail="Agents ready" if not partial else f"Some languages failed: {partial}",
                          agents=result["agents"])
        log.info("Bolna agents ready: %s (LLM %s%s)", result["agents"], result["llm"]["model"],
                 f"; {'; '.join(result['llm']['notes'])}" if result["llm"]["notes"] else "")
    except Exception as exc:  # never block the app from booting
        BOLNA_SYNC.update(state="error", detail=str(exc)[:600])
        log.warning("Bolna auto-provisioning failed: %s", exc)


SESSION_TTL = 6 * 3600        # finished sessions are dropped from memory after this (SQLite keeps the snapshot)
CONVERSATION_TTL = 3 * 3600


def prune(now: Optional[float] = None) -> dict:
    """Keep memory flat on a 512 MB free instance: forget old sessions and conversations."""
    now = now or time.time()
    engine = state.engine
    old = [sid for sid, s in engine.sessions.items() if now - s.created_at > SESSION_TTL]
    for sid in old:
        engine.sessions.pop(sid, None)
        engine._locks.pop(sid, None)
        state.orchestrator.cancel(sid)
    for execution_id in [e for e, (sid, _) in engine._execution_index.items() if sid in old]:
        engine._execution_index.pop(execution_id, None)
    stale = [cid for cid, c in intake.CONVERSATIONS.items() if now - c.touched > CONVERSATION_TTL]
    for cid in stale:
        intake.CONVERSATIONS.pop(cid, None)
    return {"sessions": len(old), "conversations": len(stale)}


async def housekeeping() -> None:
    """Every few minutes: prune memory and, if KEEP_AWAKE_MINUTES is set, ping our own URL so Render's free plan
    doesn't put the app to sleep in the middle of a demo."""
    import httpx
    last_ping = 0.0
    while True:
        await asyncio.sleep(60)
        prune()
        s = config.settings
        if s.keep_awake_minutes > 0 and s.public_base_url and time.time() - last_ping > s.keep_awake_minutes * 60:
            last_ping = time.time()
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.get(f"{s.public_base_url}/api/health")
            except httpx.HTTPError as exc:
                log.info("keep-awake ping failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    tasks = [asyncio.create_task(auto_provision_bolna()), asyncio.create_task(housekeeping())]
    yield
    for task in tasks:
        task.cancel()


app = FastAPI(title="Swarm Local Negotiator", version="2.0.0", lifespan=lifespan)
app.include_router(webhook_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.middleware("http")
async def revalidate_static(request: Request, call_next):
    """Static files are always revalidated (cheap 304s via ETag), so a stale copy is never used."""
    response = await call_next(request)
    if request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


# ----------------------------------------------------------------------------- models

class RequestFields(BaseModel):
    category: Optional[str] = None
    summary: Optional[str] = None
    pickup: Optional[str] = None
    drop: Optional[str] = None
    when: Optional[str] = None
    target_price: Optional[int] = Field(default=None, gt=0)
    walkaway_ceiling: Optional[int] = Field(default=None, gt=0)
    est_km: Optional[float] = None
    days: Optional[int] = None
    est_hours: Optional[float] = None
    min_km_per_day: Optional[float] = None
    language_hint: Optional[str] = None


class CreateSession(BaseModel):
    preset: Optional[str] = None
    request: Optional[RequestFields] = None
    mode: Optional[str] = None                 # simulation | hybrid | live
    live_phone: Optional[str] = None           # hybrid: the phone that plays Vendor A (your Android)
    live_language: Optional[str] = None
    vendor_phones: Optional[list[str]] = None  # live: up to 3 real vendor numbers
    vendor_names: Optional[list[str]] = None
    vendor_languages: Optional[list[str]] = None
    autostart: bool = True
    task: Optional[dict] = None                # from the chat intake (any service)
    providers: Optional[list[dict]] = None     # chosen from the map search (up to 5)
    language: Optional[str] = None             # call language for task sessions (one of the 11); default: the task's


class ChatBody(BaseModel):
    conversation_id: Optional[str] = None
    message: str = Field(default="", max_length=600)
    lat: Optional[float] = None
    lng: Optional[float] = None
    user_lang: Optional[str] = None            # the language the user talks to the app in
    call_language: Optional[str] = None        # set when the user picks the call language on screen


class SpeakBody(BaseModel):
    text: str = Field(min_length=1, max_length=600)


class ListenBody(BaseModel):
    audio: str = Field(min_length=10)          # base64 audio (the browser records 16 kHz mono WAV)
    mime: str = "audio/wav"
    language: Optional[str] = None


class ProviderSearch(BaseModel):
    conversation_id: Optional[str] = None
    task: Optional[dict] = None
    lat: Optional[float] = None
    lng: Optional[float] = None
    limit: int = Field(default=8, ge=1, le=15)


class BriefText(BaseModel):
    text: str = Field(min_length=3, max_length=1000)


class LockBody(BaseModel):
    leg_id: str


class SyncBody(BaseModel):
    dry_run: bool = False
    languages: Optional[list[str]] = None
    base_url: Optional[str] = None


E164 = re.compile(r"^\+?\d{10,15}$")


def _phone(raw: Optional[str]) -> str:
    phone = re.sub(r"[\s\-()]", "", raw or "")
    if phone and not phone.startswith("+"):
        phone = "+91" + phone[-10:] if len(phone) >= 10 else phone
    if phone and not E164.match(phone):
        raise HTTPException(422, f"invalid phone number: {raw}")
    return phone


def _base_url(request: Request) -> str:
    if config.settings.public_base_url:
        return config.settings.public_base_url
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    return f"{proto}://{host}"


def _require_admin(request: Request) -> None:
    token = config.settings.admin_token
    if token and request.headers.get("x-admin-token") != token:
        raise HTTPException(401, "admin token required (X-Admin-Token)")


# ----------------------------------------------------------------------------- pages + config

@app.get("/", include_in_schema=False)
async def index() -> Response:
    """The page, with its script and stylesheet URLs stamped with a content hash: after a deploy every browser (and
    Render's edge cache) fetches the new files instead of running an old app.js against the new page."""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for name in ("app.js", "styles.css"):
        digest = hashlib.sha1((STATIC / name).read_bytes()).hexdigest()[:10]
        html = html.replace(f"/static/{name}", f"/static/{name}?v={digest}")
    return Response(html, media_type="text/html", headers={"Cache-Control": "no-cache, must-revalidate"})


@app.get("/api/health")
async def health() -> dict:
    # Render and Railway expose the deployed commit; it tells you which version is live
    commit = os.getenv("RENDER_GIT_COMMIT") or os.getenv("RAILWAY_GIT_COMMIT_SHA") or os.getenv("GIT_COMMIT", "")
    return {"ok": True, "sessions": len(state.engine.sessions), "conversations": len(intake.CONVERSATIONS),
            "commit": commit[:7] or "unknown", "version": app.version}


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    return {
        **config.settings.public_summary(),
        "detected_base_url": _base_url(request),
        "presets": {k: {"title": v["title"], "brief": v["brief"], "request": v["request"]}
                    for k, v in briefs.PRESETS.items()},
        "default_preset": briefs.DEFAULT_PRESET,
        "bolna_sync": BOLNA_SYNC,
        "services": services.catalogue(),
        "gemini": llm.status(),
        "vendors": briefs.VENDOR_PERSONAS,
        "languages": languages.public_list(),
        "voice_commands": languages.voice_commands(),
    }


@app.get("/api/languages")
async def get_languages(location: str = "", state_name: str = "") -> dict:
    """The 11 call languages, and which one fits a place ("Kothrud, Pune" -> Marathi)."""
    code, reason = languages.detect(location, state_name)
    return {"languages": languages.public_list(), "detected": code, "reason": reason}


# ----------------------------------------------------------------------------- voice (Gemini, same API key)

_tts_cache: "OrderedDict[str, bytes]" = OrderedDict()


@app.post("/api/tts")
async def tts(body: SpeakBody, request: Request) -> Response:
    """Speak a line with a Gemini TTS model, for languages the browser has no voice for. Cached; 503 if unavailable."""
    key = hashlib.sha1(body.text.encode()).hexdigest()
    audio = _tts_cache.get(key)
    if audio is None:
        ratelimit.check(request, "voice", config.settings.rate_limit_voice)
        audio = await llm.speak(body.text)
        if not audio:
            raise HTTPException(503, "no voice model available right now")
        _tts_cache[key] = audio
        while len(_tts_cache) > 64:
            _tts_cache.popitem(last=False)
    _tts_cache.move_to_end(key)
    return Response(audio, media_type="audio/wav", headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/stt")
async def stt(body: ListenBody, request: Request) -> dict:
    """Speech to text with a Gemini model, for browsers without built-in speech recognition."""
    if len(body.audio) > 3_000_000:
        raise HTTPException(413, "recording too long; keep it under 30 seconds")
    try:
        audio = base64.b64decode(body.audio, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(422, "audio must be base64")
    if body.mime.split(";")[0] not in {"audio/wav", "audio/x-wav", "audio/ogg", "audio/webm", "audio/mp4",
                                       "audio/mpeg", "audio/aac", "audio/flac"}:
        raise HTTPException(415, "unsupported audio type")
    ratelimit.check(request, "voice", config.settings.rate_limit_voice)
    result = await llm.transcribe(audio, body.mime.split(";")[0], languages.normalize(body.language) or "")
    if result is None:
        raise HTTPException(503, "speech recognition isn't available right now; please type instead")
    return result


@app.post("/api/parse_brief")
async def parse_brief(body: BriefText) -> dict:
    return await briefs.parse_brief(body.text)


# ----------------------------------------------------------------------------- sessions

@app.post("/api/chat")
async def chat(body: ChatBody, request: Request) -> dict:
    ratelimit.check(request, "chat", config.settings.rate_limit_chat)
    return await intake.chat(body.conversation_id, body.message, body.lat, body.lng, body.user_lang,
                             body.call_language)


def _task_from(conversation_id: Optional[str], task: Optional[dict]) -> dict:
    if task:
        return task
    convo = intake.CONVERSATIONS.get(conversation_id or "")
    if not convo:
        raise HTTPException(404, "conversation not found; send a task")
    return convo.task


@app.post("/api/providers/search")
async def search_providers(body: ProviderSearch, request: Request) -> dict:
    ratelimit.check(request, "map search", config.settings.rate_limit_search)
    task = _task_from(body.conversation_id, body.task)
    if not task.get("service_key") and not task.get("service_label"):
        raise HTTPException(422, "tell me what service you need first")
    lat, lng = body.lat if body.lat is not None else task.get("lat"), body.lng if body.lng is not None else task.get("lng")
    return await places.search(task.get("service_key", "other"), task.get("service_label", ""),
                               task.get("location") or task.get("pickup") or "", lat, lng, body.limit)


async def _create_task_session(body: CreateSession, mode: str) -> dict:
    """Chat + map flow: legs come from the chosen providers; sim/live per mode; test phone unless real calls on."""
    s = config.settings
    try:
        req = intake.to_request(body.task or {})
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    providers = (body.providers or [])[:5]
    if not providers:
        raise HTTPException(422, "pick at least one provider")
    language = languages.normalize(body.language or body.live_language or (body.task or {}).get("call_language")) \
        or languages.DEFAULT
    personas = briefs.personas_for_providers(req, providers, language)
    test_phone = s.test_phone_override or s.live_test_phone
    vendors = []
    for i, (p, persona) in enumerate(zip(providers, personas)):
        live = mode == "live" or (mode == "hybrid" and i == 0)
        dial = (p.get("phone") if s.call_real_numbers and p.get("phone") else test_phone) if live else ""
        if live and not dial:
            raise HTTPException(422, "set LIVE_TEST_PHONE (or TEST_PHONE_OVERRIDE) to place test calls")
        provider_meta = {k: p.get(k) for k in ("rating", "reviews", "address", "distance_km", "maps_uri", "source",
                                               "phone", "open_now")}
        provider_meta["test_number"] = live and dial != p.get("phone")
        vendors.append({"name": p.get("name", f"Provider {i + 1}"), "language": language,
                        "mode": "live" if live else "sim", "phone": _phone(dial) if dial else "",
                        "provider": provider_meta, "persona": persona})
    session = await state.engine.create_session(req, vendors, mode)
    task = body.task or {}
    reason = task.get("call_language_reason") or ""
    await state.engine.trace(session.session_id, "concierge",
                             f"{req.service_label} · {req.location or req.pickup or ''} · {req.when or 'flexible'}"
                             + (f" · target {req.target_price}, max {req.walkaway_ceiling} (never told to the callers)"
                                if req.has_budget else " · appointment, no haggling"))
    await state.engine.trace(session.session_id, "linguist", f"calls in {languages.name(language)}"
                             + (f" — {reason}" if reason else ""))
    await state.engine.trace(session.session_id, "scout", "picked " + "; ".join(
        f"{p.get('name', '?')}" + (f" ★{p['rating']}" if p.get("rating") is not None else "") for p in providers))
    if body.autostart:
        await state.orchestrator.start(session.session_id)
    return session.public()


@app.post("/api/session")
async def create_session(body: CreateSession, request: Request) -> dict:
    s = config.settings
    mode = (body.mode or s.default_mode or "simulation").lower()
    if mode not in {"simulation", "hybrid", "live"}:
        raise HTTPException(422, "mode must be simulation, hybrid or live")
    if mode != "simulation":   # real phone calls cost Bolna credit
        ratelimit.check(request, "call", s.rate_limit_calls)
    if body.task:
        return await _create_task_session(body, mode)
    overrides = body.request.model_dump(exclude_none=True) if body.request else {}
    preset = body.preset or briefs.DEFAULT_PRESET
    if overrides.get("category") and overrides["category"] != briefs.PRESETS.get(preset, {}).get(
            "request", {}).get("category"):
        preset = next((k for k, v in briefs.PRESETS.items() if v["request"]["category"] == overrides["category"]),
                      preset)
    try:
        req: ServiceRequest = briefs.request_from_preset(preset, **overrides)
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, str(exc))

    vendors = briefs.vendors_for(req.category)
    for v in vendors:
        v["mode"] = "sim"
    if mode == "hybrid":
        phone = _phone(body.live_phone or s.live_test_phone)
        if not phone:
            raise HTTPException(422, "hybrid mode needs live_phone (or LIVE_TEST_PHONE)")
        vendors[0] = {"name": "You (live phone)", "language": body.live_language or "hi", "mode": "live",
                      "phone": phone}
    elif mode == "live":
        phones = [p for p in (body.vendor_phones or s.live_vendor_phones.split(",")) if p and p.strip()]
        if not phones:
            raise HTTPException(422, "live mode needs vendor_phones (or LIVE_VENDOR_PHONES)")
        names = body.vendor_names or []
        langs = body.vendor_languages or []
        for i, raw in enumerate(phones[:3]):
            vendors[i] = {"name": names[i] if i < len(names) and names[i] else f"Live Vendor {'ABC'[i]}",
                          "language": langs[i] if i < len(langs) and langs[i] else vendors[i]["language"],
                          "mode": "live", "phone": _phone(raw)}
    session = await state.engine.create_session(req, vendors, mode)
    if body.autostart:
        await state.orchestrator.start(session.session_id)
    return session.public()


@app.get("/api/session/{session_id}")
async def get_session(session_id: str) -> dict:
    if session_id in state.engine.sessions:
        return state.engine.get(session_id).public()
    snapshot = state.engine.store.load_snapshot(session_id) if state.engine.store else None
    if not snapshot:
        raise HTTPException(404, "session not found")
    return snapshot


@app.get("/api/session/{session_id}/events")
async def session_events(session_id: str) -> list:
    return state.engine.store.events(session_id) if state.engine.store else []


@app.post("/api/session/{session_id}/start")
async def start_session(session_id: str) -> dict:
    if session_id not in state.engine.sessions:
        raise HTTPException(404, "session not found")
    await state.orchestrator.start(session_id)
    return state.engine.get(session_id).public()


@app.post("/api/session/{session_id}/stop")
async def stop_session(session_id: str) -> dict:
    if session_id not in state.engine.sessions:
        raise HTTPException(404, "session not found")
    state.orchestrator.cancel(session_id)
    await state.engine.attach(session_id, status="CLOSED")
    return state.engine.get(session_id).public()


@app.post("/api/session/{session_id}/lock")
async def lock_winner(session_id: str, body: LockBody) -> dict:
    if session_id not in state.engine.sessions:
        raise HTTPException(404, "session not found")
    try:
        result = await state.engine.lock_winner(session_id, body.leg_id.upper())
    except GuardrailViolation as exc:
        raise HTTPException(409, str(exc))
    session = state.engine.get(session_id)
    payment = None
    if session.request.goal == "negotiate" and result["price"]:
        payment = await payments.create_payment_link(session_id, result["leg_id"], result["vendor_name"],
                                                     result["price"], session.request.summary)
    text = payments.deal_summary(session.request.to_dict(), result["vendor_name"], result["price"], payment,
                                 session.public().get("savings_vs_worst_first_quote"), result.get("slot", ""),
                                 result.get("excluded"), result.get("details"))
    whatsapp = await payments.whatsapp_summary(text)
    await state.engine.attach(session_id, payment=payment, whatsapp=whatsapp)
    await state.orchestrator.on_lock(session_id, result["leg_id"])
    # Finalize: phone the winner back to confirm (live legs). Runs in the background.
    asyncio.create_task(state.orchestrator.confirm_winner(session_id, result["leg_id"]))
    return {**result, "payment": payment, "whatsapp": whatsapp, "session": session.public()}


@app.post("/api/session/{session_id}/mock_pay")
async def mock_pay(session_id: str) -> dict:
    if session_id not in state.engine.sessions:
        raise HTTPException(404, "session not found")
    session = state.engine.get(session_id)
    if not session.payment:
        raise HTTPException(409, "lock a winner first")
    await state.engine.attach(session_id, payment={**session.payment, "status": "paid",
                                                   "payment_id": f"pay_mock_{session_id}"})
    return session.public()


# ----------------------------------------------------------------------------- admin (Bolna agent provisioning)

@app.get("/api/selftest")
async def selftest(format: str = "json"):
    """Replays every scripted call scenario through the live tool code on a private, in-memory engine.
    No calls are placed and nothing touches real sessions, so it is safe to run on a deployed instance."""
    report = await scenarios.run_all()
    sweep = await scenarios.language_sweep()
    if format == "text":
        langs = "  ".join(f"{v['name']} {v['passed']}/{v['total']}" for v in sweep["languages"].values())
        return PlainTextResponse(f"Every scenario in all {len(sweep['languages'])} call languages (no English words "
                                 f"allowed on non-English calls): {sweep['passed']}/{sweep['total']}\n{langs}\n\n"
                                 + scenarios.render(report))
    return {"passed": report["passed"], "total": report["total"],
            "failures": [{"name": r["name"], "problems": r["problems"]} for r in report["results"] if not r["ok"]],
            "scenarios": [{"name": r["name"], "about": r["about"], "ok": r["ok"]} for r in report["results"]],
            "languages": sweep}


@app.get("/api/admin/agent_payload")
async def agent_payload(request: Request, lang: str = "hi") -> dict:
    _require_admin(request)
    return bolna_client.build_agent_payload(lang, _base_url(request))


@app.get("/api/admin/bolna_check")
async def bolna_check(request: Request) -> dict:
    _require_admin(request)
    try:
        return {"ok": True, "account": await bolna_client.BolnaClient().me(), "sync": BOLNA_SYNC}
    except bolna_client.BolnaError as exc:
        return JSONResponse({"ok": False, "error": str(exc), "sync": BOLNA_SYNC}, status_code=502)


@app.post("/api/admin/sync_agent")
async def sync_agent(request: Request, body: SyncBody) -> dict:
    _require_admin(request)
    try:
        result = await bolna_client.sync_agents(body.languages, body.base_url or _base_url(request), body.dry_run)
        if not body.dry_run:
            BOLNA_SYNC.update(state="ok", detail="Agents ready (manual sync)", agents=result["agents"])
        return result
    except bolna_client.BolnaError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)


# ----------------------------------------------------------------------------- websocket

@app.websocket("/ws/session/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    await broadcaster.connect(session_id, ws)
    try:
        if session_id in state.engine.sessions:
            await ws.send_json({"event": {"type": "snapshot"}, "session": state.engine.get(session_id).public()})
        while True:
            msg = await ws.receive_text()  # keepalive pings from the client
            if msg == "ping":
                await ws.send_text("pong")
    except WebSocketDisconnect:
        pass
    finally:
        broadcaster.disconnect(session_id, ws)
