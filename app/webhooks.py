"""Bolna-facing HTTP endpoints: mid-call custom tools and the execution webhook.

Bolna POSTs the tool's `param` template (filled with the LLM's arguments) as JSON and reads the response
body back into the LLM as the tool result, so every response here is short, explicit and self-contained.
"""

from __future__ import annotations

import hmac
import json
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request

from app import config, state
from app.auction_engine import LegStatus
from app.bolna_client import parse_execution
from app.orchestrator import apply_execution_update

log = logging.getLogger("webhooks")
router = APIRouter()

# Our own bug must never leave the voice LLM improvising prices: it gets a safe, non-committal line instead
SAFE_FALLBACK = {
    "ok": False, "action": "SAFE_FALLBACK", "say_this": "",
    "instruction": ("Something went wrong noting this down. Do not agree to or suggest any price. Say you have noted "
                    "it and will WhatsApp them in five minutes, thank them and end the call politely."),
}

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


def _normalize_args(data: Any) -> dict:
    """Unwrap what an LLM tool call can look like: {"arguments": {...}}, {"arguments": "<json string>"},
    {"params": ...}; tidy ids ("{abc}", "a" -> "A") and drop template placeholders the LLM left unfilled."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            data = {}
    data = dict(data) if isinstance(data, dict) else {}
    for key in ("arguments", "args", "params", "parameters", "data", "input"):
        inner = data.get(key)
        if isinstance(inner, str) and inner.strip().startswith("{"):
            try:
                inner = json.loads(inner)
            except ValueError:
                inner = None
        if isinstance(inner, dict) and not data.get("session_id"):
            data = {**data, **inner}
    for key, value in list(data.items()):
        if isinstance(value, str):
            value = value.strip()
            if value.startswith(("%(", "{$", "{{")) and key not in ("session_id", "leg_id"):
                value = "unknown"   # Bolna left the template marker in: the LLM didn't fill this field
            data[key] = value
    for key in ("session_id", "leg_id"):
        data[key] = str(data.get(key) or "").strip().strip("{}$ ").replace("$", "")
    data["leg_id"] = data["leg_id"].upper()
    return data


async def _payload(request: Request) -> dict:
    """Accept JSON, form or query params, and unwrap common nesting."""
    data: Any = {}
    try:
        data = await request.json()
    except Exception:
        try:
            data = dict(await request.form())
        except Exception:
            data = {}
    data = data if isinstance(data, (dict, str)) else {}
    data = _normalize_args(data)
    for key, value in request.query_params.items():
        if key != "secret":
            data.setdefault(key, value)
    return data


def _resolve(args: dict, engine=None) -> Optional[tuple[str, str]]:
    """Find (session_id, leg_id). Falls back gracefully if the LLM mangled the ids."""
    engine = engine or state.engine
    session_id = str(args.get("session_id") or "").strip().strip("{}$ ")
    leg_id = str(args.get("leg_id") or "").strip().strip("{}$ ").upper()
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
    try:
        return await state.engine.submit_and_counter(session_id, leg_id, args)
    except Exception:
        log.exception("submit_and_counter failed for %s/%s with %s", session_id, leg_id, args)
        return SAFE_FALLBACK


@router.post("/api/tools/check_auction_status")
async def check_auction_status(request: Request) -> dict:
    if not _authorized(request):
        raise HTTPException(401, "bad tool secret")
    args = await _payload(request)
    resolved = _resolve(args)
    if not resolved:
        return UNRESOLVED
    try:
        return await state.engine.check_auction_status(*resolved)
    except Exception:
        log.exception("check_auction_status failed for %s", resolved)
        return SAFE_FALLBACK


@router.post("/api/webhooks/bolna_execution")
async def bolna_execution(request: Request) -> dict:
    if not _authorized(request):
        raise HTTPException(401, "bad webhook secret")
    payload = await _payload(request)
    parsed = parse_execution(payload)
    result = await apply_execution_update(state.engine, parsed)
    return {"ok": True, "matched": bool(result), **(result or {})}
