"""Single-origin FastAPI app: static war-room UI + REST + WebSocket + Bolna webhooks in one process.

Run:  uvicorn app.main:app --host 0.0.0.0 --port $PORT      (one worker: the auction state is in memory)
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import bolna_client, briefs, config, payments, state
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
        BOLNA_SYNC.update(state="ok", detail="Agents ready", agents=result["agents"])
        log.info("Bolna agents ready: %s", result["agents"])
    except Exception as exc:  # never block the app from booting
        BOLNA_SYNC.update(state="error", detail=str(exc)[:600])
        log.warning("Bolna auto-provisioning failed: %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    task = asyncio.create_task(auto_provision_bolna())
    yield
    task.cancel()


app = FastAPI(title="Swarm Local Negotiator", version="1.0.0", lifespan=lifespan)
app.include_router(webhook_router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


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
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "sessions": len(state.engine.sessions)}


@app.get("/api/config")
async def get_config(request: Request) -> dict:
    return {
        **config.settings.public_summary(),
        "detected_base_url": _base_url(request),
        "presets": {k: {"title": v["title"], "brief": v["brief"], "request": v["request"]}
                    for k, v in briefs.PRESETS.items()},
        "default_preset": briefs.DEFAULT_PRESET,
        "bolna_sync": BOLNA_SYNC,
        "vendors": briefs.VENDOR_PERSONAS,
    }


@app.post("/api/parse_brief")
async def parse_brief(body: BriefText) -> dict:
    return await briefs.parse_brief(body.text)


# ----------------------------------------------------------------------------- sessions

@app.post("/api/session")
async def create_session(body: CreateSession) -> dict:
    s = config.settings
    mode = (body.mode or s.default_mode or "simulation").lower()
    if mode not in {"simulation", "hybrid", "live"}:
        raise HTTPException(422, "mode must be simulation, hybrid or live")
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
    payment = await payments.create_payment_link(session_id, result["leg_id"], result["vendor_name"],
                                                 result["price"], session.request.summary)
    text = payments.deal_summary(session.request.to_dict(), result["vendor_name"], result["price"], payment,
                                 session.public().get("savings_vs_worst_first_quote"))
    whatsapp = await payments.whatsapp_summary(text)
    await state.engine.attach(session_id, payment=payment, whatsapp=whatsapp)
    await state.orchestrator.on_lock(session_id, result["leg_id"])
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
