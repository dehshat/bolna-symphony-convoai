"""Session orchestration: staggered wave dialing across simulated and live Bolna legs."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from app import config
from app.auction_engine import AuctionEngine, LegStatus
from app.bolna_client import BolnaClient, BolnaError, parse_execution
from app.briefs import opening_line, persona_by_name
from app.simulator import SimulatedLeg, release_message

log = logging.getLogger("orchestrator")


class Orchestrator:
    def __init__(self, engine: AuctionEngine):
        self.engine = engine
        self.tasks: dict[str, list[asyncio.Task]] = {}

    def plan(self, session_id: str) -> list[tuple[str, float]]:
        """Dial schedule. Waves are staggered so the first quote exists before later legs need leverage.
        In hybrid mode the simulated legs go first, so the live phone call hears real competing quotes."""
        s = config.settings
        session = self.engine.get(session_id)
        stagger = s.wave_stagger_seconds
        sims = [l for l in session.legs.values() if l.mode == "sim"]
        lives = [l for l in session.legs.values() if l.mode == "live"]
        if session.mode == "hybrid":
            schedule = [(l.leg_id, i * stagger / 2) for i, l in enumerate(sims)]
            schedule += [(l.leg_id, s.hybrid_live_delay_seconds + i * stagger) for i, l in enumerate(lives)]
        else:
            schedule = [(l.leg_id, i * stagger) for i, l in enumerate(session.legs.values())]
        return schedule

    def sequential(self, session_id: str) -> bool:
        """Live calls go one after another when they'd ring the same phone (test mode) or exceed the
        trial concurrency of 2. Simulated legs always run in parallel."""
        s = config.settings
        lives = [l for l in self.engine.get(session_id).legs.values() if l.mode == "live"]
        if s.dial_strategy == "sequential":
            return True
        if s.dial_strategy == "parallel" or len(lives) < 2:
            return False
        phones = [l.vendor_phone for l in lives]
        return len(set(phones)) < len(phones) or len(lives) > 2

    async def start(self, session_id: str) -> None:
        session = self.engine.get(session_id)
        session.status = "RUNNING"
        tasks = []
        plan = self.plan(session_id)
        if self.sequential(session_id):
            live_ids = [leg_id for leg_id, _ in plan if session.legs[leg_id].mode == "live"]
            first_delay = min((d for leg_id, d in plan if leg_id in live_ids), default=0)
            plan = [(leg_id, d) for leg_id, d in plan if leg_id not in live_ids]
            tasks.append(asyncio.create_task(self._run_live_chain(session_id, live_ids, first_delay)))
            for leg_id in live_ids[1:]:
                await self.engine.add_transcript(session_id, leg_id, "system",
                                                 "Queued: calls the same phone after the previous call ends")
        for leg_id, delay in plan:
            tasks.append(asyncio.create_task(self._run_leg(session_id, leg_id, delay)))
        self.tasks[session_id] = tasks

    async def _run_live_chain(self, session_id: str, leg_ids: list[str], delay: float) -> None:
        await asyncio.sleep(delay)
        for i, leg_id in enumerate(leg_ids):
            if self.engine.get(session_id).locked_leg_id:
                break  # the buyer already chose: don't ring anyone else
            await self._dial_live(session_id, leg_id)
            await self._wait_call_over(session_id, leg_id)
            if i < len(leg_ids) - 1:
                await asyncio.sleep(4)  # let the phone line clear before the next call

    async def _wait_call_over(self, session_id: str, leg_id: str, timeout: float = 300) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            leg = self.engine.get(session_id).legs[leg_id]
            if leg.call_ended or leg.status == LegStatus.FAILED:
                return
            await asyncio.sleep(2)

    async def _run_leg(self, session_id: str, leg_id: str, delay: float) -> None:
        leg = self.engine.get(session_id).legs[leg_id]
        speed = max(config.settings.sim_speed, 0.05)
        await asyncio.sleep(delay / speed if leg.mode == "sim" else delay)
        if leg.mode == "sim":
            session = self.engine.get(session_id)
            persona = leg.persona or persona_by_name(session.request.category, leg.vendor_name) or \
                {"name": leg.vendor_name, "language": leg.language, "style": "budget_first"}
            await SimulatedLeg(self.engine, session_id, leg_id, persona).run()
        else:
            await self._dial_live(session_id, leg_id)

    async def _dial_live(self, session_id: str, leg_id: str) -> None:
        s = config.settings
        session = self.engine.get(session_id)
        leg = session.legs[leg_id]
        agent_id = s.agent_id_for(leg.language)
        if not (s.bolna_api_key and agent_id):
            await self.engine.set_leg_status(session_id, leg_id, LegStatus.FAILED,
                                             error="Bolna not configured: set BOLNA_API_KEY and sync the agent")
            return
        if not leg.vendor_phone:
            await self.engine.set_leg_status(session_id, leg_id, LegStatus.FAILED, error="No phone number for leg")
            return
        req = session.request
        user_data = call_user_data(session_id, leg_id, req, leg.vendor_name, leg.language)
        await self.engine.set_leg_status(session_id, leg_id, LegStatus.DIALING)
        await self.engine.add_transcript(session_id, leg_id, "system", f"Dialing {leg.vendor_phone[:3]}•••• via Bolna")
        try:
            resp = await BolnaClient().place_call(agent_id, leg.vendor_phone, user_data, s.bolna_from_number or None)
        except BolnaError as exc:
            log.warning("place_call failed: %s", exc)
            await self.engine.set_leg_status(session_id, leg_id, LegStatus.FAILED,
                                             error=explain_call_error(str(exc), leg.vendor_phone))
            return
        execution_id = resp.get("execution_id") or resp.get("id") or resp.get("call_id")
        await self.engine.set_leg_status(session_id, leg_id, LegStatus.DIALING, execution_id=execution_id)
        if execution_id:
            # Background safety net: the dial chain must not wait on polling (it only waits for the call to end)
            task = asyncio.create_task(self._poll_execution(session_id, leg_id, execution_id))
            self.tasks.setdefault(session_id, []).append(task)

    async def _poll_execution(self, session_id: str, leg_id: str, execution_id: str) -> None:
        """Safety net for when the execution webhook can't reach us (wrong PUBLIC_BASE_URL, etc.)."""
        client = BolnaClient()
        deadline = time.time() + 6 * 60
        while time.time() < deadline:
            await asyncio.sleep(6)
            try:
                data = await client.get_execution(execution_id)
            except BolnaError:
                continue
            parsed = parse_execution(data)
            parsed["session_id"], parsed["leg_id"] = session_id, leg_id
            await apply_execution_update(self.engine, parsed)
            if parsed["status"] in {"completed", "failed", "no-answer", "busy", "error", "canceled", "stopped"} and \
                    (parsed["recording_url"] or parsed["status"] != "completed"):
                return

    async def on_lock(self, session_id: str, winner: str) -> None:
        session = self.engine.get(session_id)
        for leg in session.legs.values():
            if leg.status == LegStatus.FAILED:
                continue
            if leg.mode == "sim":
                asyncio.create_task(release_message(self.engine, session_id, leg.leg_id, leg.leg_id == winner))
            elif leg.leg_id != winner and leg.execution_id:
                # Best effort: Bolna's stop endpoint covers queued/scheduled calls. An in-progress call
                # hears RELEASE on its next tool call (check_auction_status / submit_and_counter).
                try:
                    await BolnaClient().stop_call(leg.execution_id)
                except BolnaError as exc:
                    log.info("stop_call %s: %s", leg.execution_id, exc)

    async def confirm_winner(self, session_id: str, leg_id: str) -> Optional[str]:
        """Finalize: phone the winning provider back and confirm the deal/appointment (live legs only)."""
        s = config.settings
        session = self.engine.get(session_id)
        leg = session.legs[leg_id]
        agent_id = s.agent_id_for(leg.language)
        if leg.mode != "live" or not (s.bolna_api_key and agent_id and leg.vendor_phone):
            return None
        req = session.request
        deal = deal_text(req, leg.neg.best_all_in, leg.neg.slot)
        user_data = call_user_data(session_id, leg_id, req, leg.vendor_name, leg.language, confirm_deal=deal)
        if self.sequential(session_id):
            await self._wait_call_over(session_id, leg_id, timeout=90)
            for other in session.legs.values():
                if other.mode == "live" and other.leg_id != leg_id and other.execution_id:
                    await self._wait_call_over(session_id, other.leg_id, timeout=90)
        try:
            resp = await BolnaClient().place_call(agent_id, leg.vendor_phone, user_data, s.bolna_from_number or None)
        except BolnaError as exc:
            await self.engine.add_transcript(session_id, leg_id, "system",
                                             "Confirmation call failed: " + explain_call_error(str(exc), leg.vendor_phone))
            return None
        execution_id = resp.get("execution_id") or resp.get("id") or resp.get("call_id")
        if execution_id:
            leg.confirm_execution_id = execution_id
            self.engine._execution_index[execution_id] = (session_id, leg_id)
        await self.engine.add_transcript(session_id, leg_id, "system", f"📞 Confirmation call placed: {deal}")
        return execution_id

    def cancel(self, session_id: str) -> None:
        for task in self.tasks.pop(session_id, []):
            task.cancel()


NEGOTIATE_GOAL = ("Find out if they can do the job, get their best all-inclusive price, then bargain using only the "
                  "lines the tools give you. Customer's preferred time: {when}.")
APPOINTMENT_GOAL = ("Book an appointment. Ask for a slot at the customer's preferred time ({when}) or the earliest "
                    "available, and the fee. Report the slot (slot field) and fee (price field) with "
                    "submit_and_counter. Never bargain on fees.")
CONFIRM_GOAL = ("This is a CONFIRMATION call: the customer has chosen this provider ({deal}). Say so clearly, ask them "
                "to note it, answer simple questions from THE JOB, say the details will come on WhatsApp, then thank "
                "them and end the call. Do not renegotiate or change anything; no tool call is needed.")
CONFIRM_OPENING = {
    "hi": "नमस्ते जी, मैं {service} के बारे में दोबारा call कर रहा हूँ। Customer ने आपको confirm कर दिया है: {deal}।",
    "kn": "ನಮಸ್ಕಾರ, {service} ಬಗ್ಗೆ ಮತ್ತೆ call ಮಾಡ್ತಿದ್ದೀನಿ. Customer ನಿಮ್ಮನ್ನ confirm ಮಾಡಿದ್ದಾರೆ: {deal}.",
    "en": "Hello, calling back about the {service}. The customer has confirmed you: {deal}.",
}


def deal_text(req, price: Optional[int], slot: str) -> str:
    from app.pricing import format_inr
    parts = []
    if slot:
        parts.append(slot)
    elif req.when:
        parts.append(req.when)
    if price:
        parts.append(f"{format_inr(price)} all-inclusive" if req.goal == "negotiate" else f"fee {format_inr(price)}")
    return ", ".join(parts) or req.service_label


def call_user_data(session_id: str, leg_id: str, req, vendor_name: str, language: str,
                   confirm_deal: str = "") -> dict:
    """Variables substituted into the Bolna agent's prompt and welcome message for one call."""
    where = f"from {req.pickup} to {req.drop}" if req.drop else (req.location or req.pickup)
    brief = f"{req.service_label}" + (f" ({req.details})" if req.details else "") + \
        f". Where: {where}. When: {req.when or 'flexible'}."
    if confirm_deal:
        goal = CONFIRM_GOAL.format(deal=confirm_deal)
        opening = CONFIRM_OPENING.get(language, CONFIRM_OPENING["en"]).format(service=req.service_label,
                                                                              deal=confirm_deal)
    else:
        goal = (APPOINTMENT_GOAL if req.goal == "appointment" else NEGOTIATE_GOAL).format(when=req.when or "flexible")
        opening = opening_line(req, language, native=True)
    return {"session_id": session_id, "leg_id": leg_id, "vendor_name": vendor_name, "service_brief": brief,
            "opening_line": opening, "call_goal": goal, "language": language}


def explain_call_error(message: str, phone: str) -> str:
    """Turn known Bolna call errors into a next step the user can act on from their phone."""
    lower = message.lower()
    if "verified phone number" in lower:
        return (f"Bolna trial accounts can only call verified numbers. In the Bolna dashboard open your profile "
                f"(bottom-left) → Verified numbers → add {phone} and confirm the OTP, then launch again.")
    if "balance" in lower or "wallet" in lower:
        return "Bolna wallet balance is too low for a call. Top up in the Bolna dashboard, then launch again."
    if "concurren" in lower:
        return "Bolna concurrency limit reached (trial: 2 calls). Wait for the other call to end, then launch again."
    return message[:300]


CALL_OVER = {"completed", "call-disconnected", "no-answer", "busy", "failed", "error", "canceled", "cancelled",
             "stopped", "balance-low"}


async def apply_execution_update(engine: AuctionEngine, parsed: dict) -> Optional[dict]:
    """Shared by the Bolna execution webhook and the polling fallback."""
    session_id, leg_id = parsed.get("session_id"), parsed.get("leg_id")
    if (not session_id or not leg_id) and parsed.get("execution_id"):
        found = engine.find_by_execution(parsed["execution_id"])
        if found:
            session_id, leg_id = found
    if not session_id or session_id not in engine.sessions or leg_id not in engine.get(session_id).legs:
        return None
    fields = {}
    if parsed.get("recording_url"):
        fields["recording_url"] = parsed["recording_url"]
    if parsed.get("execution_id"):
        fields["execution_id"] = parsed["execution_id"]
    leg = engine.get(session_id).legs[leg_id]
    if leg.confirm_execution_id and parsed.get("execution_id") == leg.confirm_execution_id:
        # The follow-up confirmation call: keep the negotiation transcript, append this one
        if parsed.get("transcript") and parsed.get("status") == "completed":
            lines = [{"role": "system", "text": "Confirmation call", "t": None}] + parsed["transcript"]
            await engine.replace_transcript(session_id, leg_id, leg.transcript + lines)
        return {"session_id": session_id, "leg_id": leg_id, "status": leg.status, "confirmation": True}
    if leg.execution_id and leg.execution_id != parsed.get("execution_id"):
        fields.pop("execution_id", None)
    if parsed.get("status") in CALL_OVER:
        fields["call_ended"] = True
    status = parsed.get("leg_status") or leg.status
    await engine.set_leg_status(session_id, leg_id, status, **fields)
    if parsed.get("transcript"):
        await engine.replace_transcript(session_id, leg_id, parsed["transcript"])
    return {"session_id": session_id, "leg_id": leg_id, "status": status}
