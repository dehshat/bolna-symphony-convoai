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

    async def start(self, session_id: str) -> None:
        session = self.engine.get(session_id)
        session.status = "RUNNING"
        tasks = []
        for leg_id, delay in self.plan(session_id):
            tasks.append(asyncio.create_task(self._run_leg(session_id, leg_id, delay)))
        self.tasks[session_id] = tasks

    async def _run_leg(self, session_id: str, leg_id: str, delay: float) -> None:
        leg = self.engine.get(session_id).legs[leg_id]
        speed = max(config.settings.sim_speed, 0.05)
        await asyncio.sleep(delay / speed if leg.mode == "sim" else delay)
        if leg.mode == "sim":
            session = self.engine.get(session_id)
            persona = persona_by_name(session.request.category, leg.vendor_name) or \
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
        user_data = {
            "session_id": session_id, "leg_id": leg_id, "vendor_name": leg.vendor_name,
            "service_brief": f"{req.summary}. Pickup: {req.pickup}. Drop: {req.drop}. When: {req.when}.",
            "opening_line": opening_line(req, leg.language), "language": leg.language,
        }
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
            await self._poll_execution(session_id, leg_id, execution_id)

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

    def cancel(self, session_id: str) -> None:
        for task in self.tasks.pop(session_id, []):
            task.cancel()


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
    status = parsed.get("leg_status") or leg.status
    await engine.set_leg_status(session_id, leg_id, status, **fields)
    if parsed.get("transcript"):
        await engine.replace_transcript(session_id, leg_id, parsed["transcript"])
    return {"session_id": session_id, "leg_id": leg_id, "status": status}
