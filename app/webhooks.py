"""Bolna-facing HTTP endpoints: mid-call custom tools and the execution webhook.

Bolna POSTs the tool's `param` template (filled with the LLM's arguments) as JSON and reads the response
body back into the LLM as the tool result, so every response here is short, explicit and self-contained.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from app import config, state
from app.auction_engine import LegStatus
from app.bolna_client import parse_execution
from app.orchestrator import apply_execution_update

log = logging.getLogger("webhooks")
router = APIRouter()

UNRESOLVED = {
    "ok": False, "action": "UNKNOWN_CALL", "say_this": "",
    "instruction": ("The auction for this call could not be found. Ask the vendor for their best all-inclusive "
                    "price, do not agree to anything, and say you will WhatsApp them in 5 minutes."),
}


def _authorized(request: Request) -> bool:
    secret = config.settings.tool_shared_secret
    if not secret:
        return True
    supplied = request.headers.get("authorization", "")
    supplied = supplied[7:] if supplied.lower().startswith("bearer ") else supplied
    supplied = supplied or request.headers.get("x-tool-secret", "") or request.query_params.get("secret", "")
    return hmac.compare_digest(supplied.encode(), secret.encode())


async def _payload(request: Request) -> dict:
    """Accept JSON, form or query params, and unwrap common nesting ({"arguments": {...}}, {"params": ...})."""
    data: dict[str, Any] = {}
    try:
        body = await request.json()
        if isinstance(body, dict):
            data = body
    except Exception:
        try:
            data = dict(await request.form())
        except Exception:
            data = {}
    for key in ("arguments", "args", "params", "parameters", "data"):
        if isinstance(data.get(key), dict) and not data.get("session_id"):
            data = {**data, **data[key]}
    for key, value in request.query_params.items():
        data.setdefault(key, value)
    return data


def _resolve(args: dict) -> Optional[tuple[str, str]]:
    """Find (session_id, leg_id). Falls back gracefully if the LLM mangled the ids."""
    engine = state.engine
    session_id = str(args.get("session_id") or "").strip().strip("{}")
    leg_id = str(args.get("leg_id") or "").strip().strip("{}").upper()
    if session_id in engine.sessions and leg_id in engine.get(session_id).legs:
        return session_id, leg_id
    execution_id = args.get("execution_id") or args.get("call_id")
    if execution_id and engine.find_by_execution(str(execution_id)):
        return engine.find_by_execution(str(execution_id))
    # Fallback: the single active live leg (or the given leg letter) in the newest running session
    for session in sorted(engine.sessions.values(), key=lambda s: s.created_at, reverse=True):
        if session.status not in {"RUNNING", "LOCKED"}:
            continue
        if session_id == session.session_id or not session_id or session_id not in engine.sessions:
            live = [l for l in session.legs.values() if l.mode == "live" and l.status not in LegStatus.TERMINAL]
            if leg_id in session.legs and session.legs[leg_id].mode == "live":
                return session.session_id, leg_id
            if len(live) == 1:
                return session.session_id, live[0].leg_id
    return None


@router.post("/api/tools/submit_and_counter")
async def submit_and_counter(request: Request) -> dict:
    if not _authorized(request):
        raise HTTPException(401, "bad tool secret")
    args = await _payload(request)
    resolved = _resolve(args)
    if not resolved:
        log.warning("unresolved tool call: %s", {k: args.get(k) for k in ("session_id", "leg_id")})
        return UNRESOLVED
    session_id, leg_id = resolved
    return await state.engine.submit_and_counter(session_id, leg_id, args)


@router.post("/api/tools/check_auction_status")
async def check_auction_status(request: Request) -> dict:
    if not _authorized(request):
        raise HTTPException(401, "bad tool secret")
    args = await _payload(request)
    resolved = _resolve(args)
    if not resolved:
        return UNRESOLVED
    return await state.engine.check_auction_status(*resolved)


@router.post("/api/webhooks/bolna_execution")
async def bolna_execution(request: Request) -> dict:
    if not _authorized(request):
        raise HTTPException(401, "bad webhook secret")
    payload = await _payload(request)
    parsed = parse_execution(payload)
    result = await apply_execution_update(state.engine, parsed)
    return {"ok": True, "matched": bool(result), **(result or {})}
