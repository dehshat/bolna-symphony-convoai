"""Shared-memory auction store: every concurrent call leg reads and writes the same session state.

Concurrency model: one asyncio event loop, one `asyncio.Lock` per session. Tool calls from different
legs (Bolna webhooks or simulated vendors) are serialized per session, so each counter-offer is
computed against a consistent snapshot. Every mutation bumps `state_version`, so the order is auditable.
SQLite stores snapshots and an event log for replay and debugging; the live state is in memory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from app.negotiation import Competitor, Decision, LegNegotiationState, decide
from app.pricing import NormalizedQuote, ServiceRequest, format_inr, merge_charges, normalize_quote, parse_inr

log = logging.getLogger("auction")


class LegStatus:
    PENDING = "PENDING"
    DIALING = "DIALING"
    CONNECTED = "CONNECTED"
    QUOTED = "QUOTED"
    COUNTERED = "COUNTERED"
    HOLD = "HOLD"          # vendor agreed to a price; soft-hold, awaiting buyer lock
    WON = "WON"
    RELEASED = "RELEASED"
    ENDED = "ENDED"        # call over without a hold (walked away / no deal)
    FAILED = "FAILED"      # no-answer / busy / error

    TERMINAL = {WON, RELEASED, ENDED, FAILED}


ACTION_TO_STATUS = {
    "COUNTER": LegStatus.COUNTERED,
    "ACCEPT_HOLD": LegStatus.HOLD,
    "FINAL_HOLD": LegStatus.HOLD,
    "WALK_AWAY": LegStatus.ENDED,
    "RELEASE": LegStatus.RELEASED,
    "WON": LegStatus.WON,
    "NOT_AVAILABLE": LegStatus.ENDED,
}


class GuardrailViolation(Exception):
    """Raised when an action would commit the buyer above the walk-away ceiling or to an invalid quote."""


@dataclass
class Leg:
    leg_id: str
    vendor_name: str
    language: str
    mode: str = "sim"                 # sim | live
    vendor_phone: str = ""
    status: str = LegStatus.PENDING
    execution_id: Optional[str] = None
    neg: LegNegotiationState = field(default_factory=LegNegotiationState)
    charges: dict = field(default_factory=dict)
    last_quote: Optional[NormalizedQuote] = None
    last_stated: Optional[Any] = None
    last_pricing_model: str = "flat"
    best_verified: Optional[int] = None   # lowest all-in quote with every add-on resolved (safe to cite)
    first_stated: Optional[int] = None    # first number the vendor said
    first_all_in: Optional[int] = None    # what that first number really cost
    first_model: str = "flat"
    first_hidden: int = 0                 # add-ons the vendor left out of the first number
    quotes: list = field(default_factory=list)
    transcript: list = field(default_factory=list)
    recording_url: Optional[str] = None
    last_action: Optional[str] = None
    last_counter_line: str = ""
    error: str = ""

    def public(self) -> dict:
        phone = self.vendor_phone
        masked = f"{phone[:3]}••••{phone[-3:]}" if len(phone) > 6 else phone
        return {
            "leg_id": self.leg_id,
            "vendor_name": self.vendor_name,
            "language": self.language,
            "mode": self.mode,
            "vendor_phone": masked,
            "status": self.status,
            "execution_id": self.execution_id,
            "first_stated": self.first_stated,
            "first_all_in": self.first_all_in,
            "first_model": self.first_model,
            "first_hidden": self.first_hidden,
            "best_all_in": self.neg.best_all_in,
            "best_verified": self.best_verified,
            "last_counter": self.neg.last_counter,
            "rounds": self.neg.rounds,
            "last_quote": self.last_quote.to_dict() if self.last_quote else None,
            "quotes": self.quotes[-8:],
            "transcript": self.transcript[-40:],
            "recording_url": self.recording_url,
            "last_action": self.last_action,
            "error": self.error,
        }


@dataclass
class Session:
    session_id: str
    request: ServiceRequest
    legs: dict
    mode: str = "simulation"
    status: str = "CREATED"            # CREATED | RUNNING | LOCKED | CLOSED
    locked_leg_id: Optional[str] = None
    state_version: int = 0
    created_at: float = field(default_factory=time.time)
    arbitrage_log: list = field(default_factory=list)
    payment: Optional[dict] = None
    whatsapp: Optional[dict] = None

    def t(self) -> float:
        return round(time.time() - self.created_at, 1)

    def current_best(self, exclude: Optional[str] = None, verified_only: bool = True) -> Optional[Competitor]:
        best = None
        for leg in self.legs.values():
            if leg.leg_id == exclude or leg.status == LegStatus.FAILED:
                continue
            price = leg.best_verified if verified_only else leg.neg.best_all_in
            if price is not None and (best is None or price < best.price):
                best = Competitor(leg.leg_id, leg.vendor_name, price)
        return best

    def public(self) -> dict:
        best = self.current_best(verified_only=False)
        first_quotes = [l.first_all_in for l in self.legs.values() if l.first_all_in]
        savings = None
        if best and first_quotes:
            savings = max(first_quotes) - best.price
        return {
            "session_id": self.session_id,
            "request": self.request.to_dict(),
            "mode": self.mode,
            "status": self.status,
            "locked_leg_id": self.locked_leg_id,
            "state_version": self.state_version,
            "t": self.t(),
            "current_best_quote": asdict(best) if best else None,
            "savings_vs_worst_first_quote": savings,
            "legs": [leg.public() for leg in self.legs.values()],
            "arbitrage_log": self.arbitrage_log[-30:],
            "payment": self.payment,
            "whatsapp": self.whatsapp,
        }


Publisher = Callable[[str, dict], Awaitable[None]]


class Store:
    """Tiny SQLite persistence: latest snapshot per session + append-only event log."""

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL") if path != ":memory:" else None
        self._conn.execute("CREATE TABLE IF NOT EXISTS sessions (session_id TEXT PRIMARY KEY, snapshot TEXT, "
                           "updated_at REAL)")
        self._conn.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                           "session_id TEXT, type TEXT, payload TEXT, ts REAL)")
        self._conn.commit()
        self._write_lock = asyncio.Lock()

    def _save(self, session_id: str, snapshot: dict, event: Optional[dict]) -> None:
        now = time.time()
        self._conn.execute("INSERT INTO sessions VALUES (?, ?, ?) ON CONFLICT(session_id) DO UPDATE SET "
                           "snapshot=excluded.snapshot, updated_at=excluded.updated_at",
                           (session_id, json.dumps(snapshot, default=str), now))
        if event:
            self._conn.execute("INSERT INTO events (session_id, type, payload, ts) VALUES (?, ?, ?, ?)",
                               (session_id, event.get("type", ""), json.dumps(event, default=str), now))
        self._conn.commit()

    async def save(self, session_id: str, snapshot: dict, event: Optional[dict] = None) -> None:
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._save, session_id, snapshot, event)
            except sqlite3.Error as exc:  # persistence must never break a live call
                log.warning("sqlite save failed: %s", exc)

    def load_snapshot(self, session_id: str) -> Optional[dict]:
        row = self._conn.execute("SELECT snapshot FROM sessions WHERE session_id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def events(self, session_id: str) -> list:
        rows = self._conn.execute("SELECT payload FROM events WHERE session_id=? ORDER BY id", (session_id,))
        return [json.loads(r[0]) for r in rows.fetchall()]


class AuctionEngine:
    def __init__(self, store: Optional[Store] = None, publisher: Optional[Publisher] = None):
        self.sessions: dict[str, Session] = {}
        self.store = store
        self.publisher = publisher
        self._locks: dict[str, asyncio.Lock] = {}
        self._execution_index: dict[str, tuple[str, str]] = {}

    # ------------------------------------------------------------------ helpers
    def lock_for(self, session_id: str) -> asyncio.Lock:
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    def get(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if not session:
            raise KeyError(f"unknown session {session_id}")
        return session

    def find_by_execution(self, execution_id: str) -> Optional[tuple[str, str]]:
        return self._execution_index.get(execution_id)

    async def _emit(self, session: Session, event: dict) -> None:
        event = {**event, "session_id": session.session_id, "t": session.t(), "state_version": session.state_version}
        snapshot = session.public()
        if self.store:
            await self.store.save(session.session_id, snapshot, event)
        if self.publisher:
            try:
                await self.publisher(session.session_id, {"event": event, "session": snapshot})
            except Exception as exc:  # a dead websocket must never break a tool call
                log.warning("publish failed: %s", exc)

    @staticmethod
    def _say(session: Session, leg: Leg, role: str, text: str) -> None:
        if text:
            leg.transcript.append({"role": role, "text": text, "t": session.t()})

    # ------------------------------------------------------------------ lifecycle
    async def create_session(self, request: ServiceRequest, vendors: list[dict], mode: str = "simulation") -> Session:
        legs = {}
        for i, v in enumerate(vendors[:3]):
            leg_id = v.get("leg_id") or "ABC"[i]
            legs[leg_id] = Leg(leg_id=leg_id, vendor_name=v.get("name", f"Vendor {leg_id}"),
                               language=v.get("language", "hi"), mode=v.get("mode", "sim"),
                               vendor_phone=v.get("phone", ""))
        session = Session(session_id=uuid.uuid4().hex[:10], request=request, legs=legs, mode=mode)
        self.sessions[session.session_id] = session
        await self._emit(session, {"type": "session_created"})
        return session

    async def set_leg_status(self, session_id: str, leg_id: str, status: str, **fields) -> None:
        session = self.get(session_id)
        async with self.lock_for(session_id):
            leg = session.legs[leg_id]
            if leg.status in LegStatus.TERMINAL and status not in LegStatus.TERMINAL:
                return  # never resurrect a finished leg from a late/duplicate webhook
            if leg.status in {LegStatus.HOLD, LegStatus.WON, LegStatus.RELEASED} and \
                    status in {LegStatus.ENDED, LegStatus.FAILED, LegStatus.CONNECTED, LegStatus.DIALING}:
                status = leg.status  # keep the negotiated outcome when the call hangs up / late webhooks arrive
            leg.status = status
            for key, value in fields.items():
                setattr(leg, key, value)
            if fields.get("execution_id"):
                self._execution_index[fields["execution_id"]] = (session_id, leg_id)
            if session.status == "CREATED" and status != LegStatus.PENDING:
                session.status = "RUNNING"
            session.state_version += 1
        await self._emit(session, {"type": "leg_status", "leg_id": leg_id, "status": status})

    async def add_transcript(self, session_id: str, leg_id: str, role: str, text: str) -> None:
        session = self.get(session_id)
        async with self.lock_for(session_id):
            self._say(session, session.legs[leg_id], role, text)
            session.state_version += 1
        await self._emit(session, {"type": "transcript", "leg_id": leg_id, "role": role, "text": text})

    async def replace_transcript(self, session_id: str, leg_id: str, lines: list[dict]) -> None:
        session = self.get(session_id)
        async with self.lock_for(session_id):
            if lines:
                session.legs[leg_id].transcript = lines
            session.state_version += 1
        await self._emit(session, {"type": "transcript_final", "leg_id": leg_id})

    # ------------------------------------------------------------------ the core tool
    async def submit_and_counter(self, session_id: str, leg_id: str, args: dict) -> dict:
        """Mid-call tool: record what the vendor said, then return the deterministic next line."""
        session = self.get(session_id)
        event: dict = {"type": "quote"}
        async with self.lock_for(session_id):
            leg = session.legs.get(leg_id)
            if leg is None:
                raise KeyError(f"unknown leg {leg_id}")
            req = session.request
            said = str(args.get("vendor_said") or "").strip()
            self._say(session, leg, "vendor", said)
            if leg.status in {LegStatus.PENDING, LegStatus.DIALING}:
                leg.status = LegStatus.CONNECTED
            if session.status == "CREATED":
                session.status = "RUNNING"

            new_charges = {k: args.get(k) for k in ("toll", "driver_bata", "other_charges") if k in args}
            leg.charges = merge_charges(leg.charges, new_charges)
            price = args.get("price")
            model = args.get("pricing_model") or "flat"
            quote: Optional[NormalizedQuote] = None
            if parse_inr(price):
                quote = normalize_quote(req, price, model, leg.charges, said)
                leg.last_stated, leg.last_pricing_model = price, quote.pricing_model
            elif leg.last_stated is not None:
                # Vendor answered an inclusion question without repeating the price
                quote = normalize_quote(req, leg.last_stated, leg.last_pricing_model, leg.charges, said)

            previous_best, raised = leg.neg.best_all_in, False
            if quote is not None and quote.status != "CLARIFY" and quote.all_in is not None:
                prev_base = leg.quotes[-1]["base_price"] if leg.quotes else None
                if previous_best is not None and prev_base is not None and quote.base_price > prev_base \
                        and quote.all_in > previous_best:
                    raised = True  # quotes are binding: keep the lower earlier price
                else:
                    leg.neg.best_all_in = quote.all_in
                if quote.status == "VALID" and not raised:
                    leg.best_verified = leg.neg.best_all_in
                if leg.first_stated is None:
                    leg.first_stated, leg.first_model = quote.stated_price, quote.pricing_model
                if quote.status == "VALID" and leg.first_all_in is None:
                    leg.first_all_in, leg.first_hidden = quote.all_in, quote.hidden_charges
                leg.quotes.append({"t": session.t(), "stated": quote.stated_price, "base_price": quote.base_price,
                                   "all_in": quote.all_in, "model": quote.pricing_model, "status": quote.status,
                                   "hidden": quote.hidden_charges})
                if leg.status == LegStatus.CONNECTED:
                    leg.status = LegStatus.QUOTED
            leg.last_quote = quote if quote is not None else leg.last_quote

            competitor = session.current_best(exclude=leg_id)
            available = str(args.get("availability") or "").strip().lower() not in {"no", "false", "nahi", "illa"}
            decision = decide(req, leg.neg, quote, competitor, leg.language, session.locked_leg_id, leg_id,
                              previous_best=previous_best, raised_price=raised, available=available,
                              explain_unit=bool(parse_inr(price)) and quote is not None)
            self._apply_decision(session, leg, decision)
            event.update(self._decision_event(session, leg, decision, quote))
            response = self._tool_response(session, leg, decision, quote, competitor)  # snapshot under the lock
        await self._emit(session, event)
        return response

    async def check_auction_status(self, session_id: str, leg_id: str) -> dict:
        """Mid-call tool: has another vendor beaten this leg, or has the buyer locked someone?"""
        session = self.get(session_id)
        async with self.lock_for(session_id):
            leg = session.legs[leg_id]
            competitor = session.current_best(exclude=leg_id)
            if session.locked_leg_id is None and leg.last_quote is None and competitor is None:
                decision = Decision(action="NO_UPDATE", say="",
                                    instruction="No competing quotes yet. Ask the vendor for their best "
                                                "all-inclusive price and call submit_and_counter when they say it.")
            elif session.locked_leg_id is None and leg.neg.best_all_in is not None and \
                    (competitor is None or competitor.price >= leg.neg.best_all_in) and leg.last_action == "COUNTER":
                decision = Decision(action="NO_UPDATE", say="",
                                    instruction="Nothing new. Wait for the vendor to answer your last offer, then "
                                                "call submit_and_counter with what they say.")
            else:
                decision = decide(session.request, leg.neg, leg.last_quote, competitor, leg.language,
                                  session.locked_leg_id, leg_id)
            event = {"type": "status_check", **self._decision_event(session, leg, decision, leg.last_quote)}
            if decision.action != "NO_UPDATE":
                self._apply_decision(session, leg, decision)
            else:
                session.state_version += 1
            response = self._tool_response(session, leg, decision, leg.last_quote, competitor)
        await self._emit(session, event)
        return response

    def _apply_decision(self, session: Session, leg: Leg, decision: Decision) -> None:
        leg.last_action = decision.action
        new_status = ACTION_TO_STATUS.get(decision.action)
        if new_status and leg.status not in {LegStatus.WON, LegStatus.RELEASED}:
            leg.status = new_status
        self._say(session, leg, "agent", decision.say)
        if decision.leverage:
            session.arbitrage_log.append({
                "t": session.t(), "from_leg": decision.leverage["leg_id"],
                "from_vendor": decision.leverage["vendor_name"], "competitor_price": decision.leverage["price"],
                "to_leg": leg.leg_id, "to_vendor": leg.vendor_name, "counter": decision.counter_price,
                "action": decision.action, "state_version": session.state_version + 1,
            })
        session.state_version += 1

    @staticmethod
    def _decision_event(session: Session, leg: Leg, decision: Decision, quote: Optional[NormalizedQuote]) -> dict:
        return {"leg_id": leg.leg_id, "action": decision.action, "counter": decision.counter_price,
                "leverage": decision.leverage, "all_in": quote.all_in if quote else None,
                "hidden": quote.hidden_charges if quote else 0, "say": decision.say}

    @staticmethod
    def _tool_response(session: Session, leg: Leg, decision: Decision, quote: Optional[NormalizedQuote],
                       competitor: Optional[Competitor]) -> dict:
        # NOTE: the walk-away ceiling is deliberately never included in anything the voice LLM sees.
        return {
            "ok": True,
            "action": decision.action,
            "say_this": decision.say,
            "counter_price_inr": decision.counter_price,
            "agreed_price_inr": decision.binding_price,
            "end_call": decision.end_call,
            "instruction": decision.instruction,
            "vendor_all_in_estimate_inr": quote.all_in if quote else None,
            "best_competing_quote_inr": competitor.price if competitor else None,
            "state_version": session.state_version,
        }

    # ------------------------------------------------------------------ buyer actions
    async def lock_winner(self, session_id: str, leg_id: str) -> dict:
        session = self.get(session_id)
        async with self.lock_for(session_id):
            if session.locked_leg_id:
                raise GuardrailViolation(f"session already locked to leg {session.locked_leg_id}")
            leg = session.legs.get(leg_id)
            if leg is None:
                raise GuardrailViolation("unknown leg")
            price = leg.neg.best_all_in
            if price is None:
                raise GuardrailViolation("this vendor has not given a usable quote yet")
            if price > session.request.walkaway_ceiling:
                raise GuardrailViolation(f"{format_inr(price)} is above your walk-away ceiling "
                                         f"{format_inr(session.request.walkaway_ceiling)}")
            if leg.status == LegStatus.FAILED:
                raise GuardrailViolation("this call failed")
            session.locked_leg_id = leg_id
            session.status = "LOCKED"
            leg.status = LegStatus.WON
            released = []
            for other in session.legs.values():
                if other.leg_id != leg_id and other.status != LegStatus.FAILED:
                    other.status = LegStatus.RELEASED
                    released.append(other.leg_id)
            session.state_version += 1
            result = {"leg_id": leg_id, "vendor_name": leg.vendor_name, "price": price, "released": released,
                      "unresolved_addons": list(leg.last_quote.unknown_items) if leg.last_quote else []}
        await self._emit(session, {"type": "locked", **result})
        return result

    async def attach(self, session_id: str, **fields) -> None:
        session = self.get(session_id)
        async with self.lock_for(session_id):
            for key, value in fields.items():
                setattr(session, key, value)
            session.state_version += 1
        await self._emit(session, {"type": "session_update", "fields": list(fields)})
