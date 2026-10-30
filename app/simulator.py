"""Simulated vendor call legs for zero-cost, single-phone testing.

A simulated leg goes through exactly the same `AuctionEngine.submit_and_counter` /
`check_auction_status` code path that the Bolna mid-call tool webhooks use. Only the vendor's
side is simulated:
  * the numbers come from a deterministic persona state machine (open price, floor, concession, traps),
  * the wording optionally comes from Gemini, and a rewrite is rejected if it changes any amount.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from typing import Optional

from app import config, lexicon, llm, services
from app.auction_engine import AuctionEngine, LegStatus
from app.briefs import opening_gloss, opening_line, short_name
from app.pricing import ServiceRequest, category_rules, round_to

log = logging.getLogger("sim")

END_ACTIONS = {"ACCEPT_HOLD", "FINAL_HOLD", "WALK_AWAY", "RELEASE", "WON", "NOT_AVAILABLE", "HOLD_SLOT",
               "BOOKING_OVER_BUDGET", "WON_BOOKING"}

# Extra English phrasings so simulated English-speaking vendors don't repeat themselves (no LLM needed)
VARIANTS = {
    "en": {
        "avail_budget": ["We can do it. Tell me your budget first.", "Yes yes. How much are you looking at?"],
        "quote_trap": ["{price} rupees, that's it.", "Can do. {price} rupees."],
        "quote_inclusive": ["It'll be {price}, everything included.", "Possible. {price} is the rate, all inclusive."],
        "accept": ["Alright, {price} works. Confirm quickly.", "Fine, {price}, all inclusive. Let me know soon."],
        "concede": ["Can't do that much. {price}, all inclusive.", "Costs are high, sir. Best I can do is {price}."],
        "final": ["{price} is my last, can't go lower.", "Sir, below {price} it doesn't work for us."],
        "beat": ["I'll beat that, {price}, all inclusive.", "Fine, {price}, everything included."],
        "think": ["Hold on, let me check..."],
        "bye_hold": ["Sure, message me."],
    },
}

# The add-on a "trap" vendor hides in its first quote, named as the vendor would
BATA_LABEL = {"cab_outstation": "driver bata", "mini_truck": "loading labour", "ac_repair": "gas refill"}


@dataclass
class VendorBrain:
    """Hidden state of a simulated vendor: what it wants, what it will accept, and which tricks it plays."""
    persona: dict
    req: ServiceRequest
    open_all_in: int = 0
    floor: int = 0
    current: int = 0
    trap_stated: int = 0
    trap_addons: dict = field(default_factory=dict)
    injected: bool = False
    revealed: bool = False
    per_km_rate: Optional[int] = None
    parts_label: str = ""          # "material / parts" etc. when this vendor bills them separately

    def __post_init__(self) -> None:
        p, r = self.persona, self.req
        if "open_ratio_c" in p:
            self.open_all_in = round_to(r.walkaway_ceiling * p["open_ratio_c"])
        else:
            self.open_all_in = round_to(r.target_price * p.get("open_ratio_t", 1.1))
        self.floor = round_to(r.target_price * p.get("floor_ratio_t", 1.05))
        if p.get("style") == "trap":
            est = r.estimates()
            rules = category_rules(r.category)
            # Materials/parts are billed separately (excluded), so the hidden add-on is the visiting charge instead
            parts_excluded = "other_charges" in rules.get("excluded", [])
            addon_key = "driver_bata" if r.category == "cab_outstation" or parts_excluded else "other_charges"
            toll = est["toll"] if r.category == "cab_outstation" else 0
            addon = est[addon_key] or round_to(max(r.target_price * 0.15, 150))
            self.parts_label = rules["labels"]["other_charges"] if parts_excluded else ""
            self.trap_stated = self.open_all_in
            self.trap_addons = {"toll": toll, addon_key: addon}
            self.open_all_in = self.trap_stated + toll + addon
        if p.get("per_km_if_long") and r.category == "cab_outstation" and r.est_km >= 150:
            billable = max(r.est_km, r.min_km_per_day * r.days)
            self.per_km_rate = max(1, round(self.open_all_in / billable))
            self.open_all_in = int(self.per_km_rate * billable)
        self.floor = min(self.floor, self.open_all_in)
        self.current = self.open_all_in

    def respond_to_counter(self, counter: int) -> tuple[str, int]:
        if counter >= self.floor:
            self.current = counter
            return "accept", counter
        if self.current <= self.floor:
            return "final", self.current
        new = max(self.floor, round_to(self.current - (self.current - counter) * self.persona.get("concession", 0.5)))
        if new >= self.current:
            new = max(self.floor, self.current - 50)
        self.current = new
        return ("final" if new == self.floor else "concede"), new


class SimulatedLeg:
    def __init__(self, engine: AuctionEngine, session_id: str, leg_id: str, persona: dict):
        self.engine = engine
        self.session_id = session_id
        self.leg_id = leg_id
        self.persona = persona
        session = engine.get(session_id)
        self.req = session.request
        self.lang = persona.get("language", "hi")
        self.svc = services.get(self.req.category, self.req.service_label)
        self.model = self.svc.unit if self.svc.unit in ("per_month", "per_visit") else "flat"
        self.monthly = self.svc.unit == "per_month"
        self.lang = self.lang if self.lang in lexicon.ALL else "en"
        self.lines = lexicon.VENDOR[self.lang]
        self.rng = random.Random(f"{session_id}:{leg_id}")
        self.brain = VendorBrain(persona, self.req) if self.req.goal == "negotiate" else None

    @property
    def speed(self) -> float:
        return max(config.settings.sim_speed, 0.05)

    async def pause(self, low: float, high: float) -> None:
        await asyncio.sleep(self.rng.uniform(low, high) / self.speed)

    def _history(self) -> list[dict]:
        return self.engine.get(self.session_id).legs[self.leg_id].transcript[-4:]

    def _pick(self, key: str) -> str:
        return self.rng.choice([self.lines[key]] + VARIANTS.get(self.lang, {}).get(key, []))

    def _with_unit(self, text: str, price, language: str) -> str:
        """'9000 rupees' -> '9000 rupees per month' for a monthly job (cook, maid)."""
        if not self.monthly or price is None:
            return text
        rupees = lexicon.word(language, "rupees")
        return re.sub(rf"{price}(\s+{re.escape(rupees)})?", lambda m: f"{m.group(0)} {lexicon.unit(language, 'per_month')}",
                      text, count=1)

    async def _vendor_text(self, key: str, intent: str, amounts: list[int], **fmt) -> tuple[str, str]:
        """(what the vendor says in the call's language, its English meaning). fmt values may be (english, native)."""
        native = self._pick(key).format(**{k: v[1] if isinstance(v, tuple) else v for k, v in fmt.items()})
        english = lexicon.VENDOR["en"][key].format(**{k: v[0] if isinstance(v, tuple) else v for k, v in fmt.items()})
        if key in {"quote_trap", "quote_inclusive", "beat", "concede"}:
            native = self._with_unit(native, fmt.get("price"), self.lang)
            english = self._with_unit(english, fmt.get("price"), "en")
        use_llm = config.settings.sim_llm_rephrase and self.speed <= 2
        task = asyncio.create_task(llm.vendor_line(self.persona["name"], self.lang, intent, native, amounts,
                                                   self._history())) if use_llm else None
        await self.pause(1.6, 2.8)  # the vendor is "speaking"
        if task is not None:
            try:
                native = await asyncio.wait_for(task, timeout=2.5)
            except (asyncio.TimeoutError, Exception):
                pass
        return native, english

    def _label(self, english: str) -> tuple[str, str]:
        return english, lexicon.charge_words(english, self.lang)

    async def tool(self, said: tuple[str, str], price=None, model: str = "flat", slot: str = "unknown",
                   extras: str = "none", **charges) -> dict:
        # Like the live voice LLM: the tool gets the vendor's words in English; the screen shows what was said
        native, english = said
        args = {"vendor_said": english, "_spoken": native,
                "price": price if price is not None else "unknown", "pricing_model": model,
                "toll": charges.get("toll", "unknown"), "driver_bata": charges.get("driver_bata", "unknown"),
                "other_charges": charges.get("other_charges", "unknown"), "extras": extras, "notes": "none",
                "availability": "yes", "slot": slot}
        await self.pause(0.2, 0.5)  # tool round-trip latency in a real call
        resp = await self.engine.submit_and_counter(self.session_id, self.leg_id, args)
        await self._naturalize(resp.get("say_this", ""))
        return resp

    async def _naturalize(self, say: str) -> None:
        """Like the live Bolna LLM, speak the deterministic line in natural words (amounts are verified)."""
        if not say or self.speed > 2 or not llm.enabled() or not config.settings.sim_llm_rephrase:
            return
        try:
            natural = await asyncio.wait_for(llm.agent_line(self.lang, say, self._history()[:-1]), timeout=3.5)
        except (asyncio.TimeoutError, Exception):
            return
        if natural and natural != say:
            await self.engine.rephrase_agent_line(self.session_id, self.leg_id, say, natural)

    def _all_included(self) -> dict:
        return {"toll": "included", "driver_bata": "included", "other_charges": "included"}

    async def run(self) -> None:
        try:
            await self._run()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one simulated leg crash the session
            log.exception("sim leg %s failed", self.leg_id)
            await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.FAILED, error=str(exc))

    async def _run(self) -> None:
        if self.req.goal == "appointment":
            await self._run_booking()
            return
        b, p = self.brain, self.persona
        await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.DIALING)
        await self.pause(2.0, 3.5)  # ringing
        await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.CONNECTED)
        await self._greet()

        # First vendor turn
        if p.get("style") == "budget_first":
            said = await self._vendor_text("avail_budget", "available, asks the buyer's budget first", [])
            resp = await self.tool(said)
            if resp["action"] == "STALL_FOR_ANCHOR" or resp["action"] == "ASK_VENDOR_FIRST":
                said = await self._vendor_text("think", "needs a moment to check price", [])
                await self.engine.add_transcript(self.session_id, self.leg_id, "vendor", said[0], gloss=said[1])
                await self.pause(1.0, 2.0)
                resp = await self.engine.check_auction_status(self.session_id, self.leg_id)
            if resp["action"] == "ASK_TO_BEAT" and resp.get("best_competing_quote_inr"):
                competitor = resp["best_competing_quote_inr"]
                beat = min(b.current, max(b.floor, round_to(competitor - 100)))
                b.current = beat
                key = "beat" if beat < competitor else "concede"  # can't honestly "beat" it above its price
                said = await self._vendor_text(key, f"offers {beat} all inclusive", [beat], price=beat)
                resp = await self.tool(said, beat, self.model, **self._all_included())
            else:
                resp = await self._opening_quote()
        else:
            resp = await self._opening_quote()

        # Negotiation loop
        for _ in range(10):
            action = resp["action"]
            if action in END_ACTIONS or self.engine.get(self.session_id).legs[self.leg_id].status in LegStatus.TERMINAL:
                break
            if action == "ASK_INCLUSIONS":
                resp = await self._reveal_addons()
            elif action == "CLARIFY":
                said = await self._vendor_text("restate", f"total is {b.current} all inclusive", [b.current],
                                               price=b.current)
                resp = await self.tool(said, b.current, self.model, **self._all_included())
            elif action in {"ASK_VENDOR_FIRST", "STALL_FOR_ANCHOR"}:
                resp = await self._opening_quote()
            elif action in {"COUNTER", "ASK_TO_BEAT"}:
                resp = await self._answer_counter(resp)
            elif action == "NO_UPDATE":
                resp = await self._opening_quote()
            else:
                break

        await self._goodbye(resp["action"])

    async def _greet(self) -> None:
        """The provider picks up and says hello; the agent opens with the job in the call's language."""
        greeting = self.persona.get("greeting") or self.lines["greeting"].format(name=short_name(self.persona["name"]))
        await self.engine.add_transcript(self.session_id, self.leg_id, "vendor", greeting,
                                         gloss=lexicon.VENDOR["en"]["greeting"].format(name=short_name(self.persona["name"])))
        await self.pause(0.4, 0.8)
        local = self.engine.get(self.session_id).localized.get(self.lang)
        await self.engine.add_transcript(self.session_id, self.leg_id, "agent",
                                         opening_line(self.req, self.lang, local=local), gloss=opening_gloss(self.req))

    def _booking_line(self, key: str, **fmt) -> tuple[str, str]:
        native = lexicon.BOOKING[self.lang][key].format(**{k: v[1] if isinstance(v, tuple) else v for k, v in fmt.items()})
        english = lexicon.BOOKING["en"][key].format(**{k: v[0] if isinstance(v, tuple) else v for k, v in fmt.items()})
        return native, english

    async def _run_booking(self) -> None:
        """Clinic / salon / lawyer: offer a slot, state the fee, accept the hold. No haggling."""
        index = "ABCDE".index(self.leg_id) if self.leg_id in "ABCDE" else 0
        slot_en = lexicon.SIM_SLOTS[index % len(lexicon.SIM_SLOTS)]
        slot = (slot_en, lexicon.localize_when(slot_en, self.lang))
        low, high = self.svc.typical
        fee = round_to(low + (high - low) * (0.2, 0.5, 0.35, 0.6, 0.25)[index % 5])
        await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.DIALING)
        await self.pause(2.0, 3.5)
        await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.CONNECTED)
        await self._greet()
        await self.pause(1.6, 2.6)
        resp = await self.tool(self._booking_line("slot_later" if index == 1 else "slot", slot=slot), None,
                               slot=slot_en)
        for _ in range(4):
            if resp["action"] in END_ACTIONS:
                break
            await self.pause(1.4, 2.4)
            if resp["action"] == "ASK_FEE":
                resp = await self.tool(self._booking_line("fee", price=fee), fee, "per_visit", slot=slot_en,
                                       other_charges="included")
            elif resp["action"] == "ASK_SLOT":
                resp = await self.tool(self._booking_line("slot", slot=slot), None, slot=slot_en)
            else:
                break
        if resp["action"] == "HOLD_SLOT":
            await self.pause(0.8, 1.4)
            native, english = self._booking_line("ok", slot=slot)
            await self.engine.add_transcript(self.session_id, self.leg_id, "vendor", native, gloss=english)
        leg = self.engine.get(self.session_id).legs[self.leg_id]
        if leg.status not in {LegStatus.HOLD, LegStatus.WON, LegStatus.RELEASED}:
            await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.ENDED)
        else:
            await self.engine.add_transcript(self.session_id, self.leg_id, "system", "Call ended — slot on hold")

    async def _opening_quote(self) -> dict:
        b = self.brain
        if b.per_km_rate:
            said = await self._vendor_text("quote_per_km", f"{b.per_km_rate} rupees per km, all inclusive",
                                           [b.per_km_rate], rate=b.per_km_rate)
            return await self.tool(said, b.per_km_rate, "per_km", **self._all_included())
        if b.trap_addons and not b.revealed:
            said = await self._vendor_text("quote_trap", f"quotes {b.trap_stated} 'only' without mentioning extras",
                                           [b.trap_stated], price=b.trap_stated)
            return await self.tool(said, b.trap_stated, self.model)
        said = await self._vendor_text("quote_inclusive", f"quotes {b.current} all inclusive", [b.current],
                                       price=b.current)
        return await self.tool(said, b.current, self.model, **self._all_included())

    async def _reveal_addons(self) -> dict:
        b = self.brain
        b.revealed = True
        toll = b.trap_addons.get("toll", 0)
        addon_key = next((k for k in b.trap_addons if k != "toll"), "driver_bata")
        addon = b.trap_addons.get(addon_key, 0)
        if not b.trap_addons:
            said = await self._vendor_text("quote_inclusive", f"confirms {b.current} is all inclusive", [b.current],
                                           price=b.current)
            return await self.tool(said, None, **self._all_included())
        labels = category_rules(self.req.category)["labels"]
        label_en = BATA_LABEL.get(self.req.category) if addon_key != "driver_bata" or \
            self.req.category == "cab_outstation" else labels["driver_bata"]
        label = self._label(label_en or labels[addon_key])
        spoken = addon
        if addon_key == "driver_bata" and self.req.category == "cab_outstation" and self.req.days > 1:
            # Operators quote bata per day; the normalizer multiplies a per-day bata by the trip's days
            spoken = addon // self.req.days
            label = (lexicon.VENDOR["en"]["per_day_bata"], self.lines["per_day_bata"])
        if toll:
            said = await self._vendor_text("reveal", f"admits toll {toll} and {label[0]} {spoken} are extra",
                                           [toll, spoken], toll=toll, bata=spoken, bata_label=label)
        else:
            said = await self._vendor_text("reveal_one", f"admits {label[0]} {spoken} is extra", [spoken], bata=spoken,
                                           bata_label=label)
        charges = {"toll": f"extra {toll}" if toll else "na", "driver_bata": "na", "other_charges": "na"}
        charges[addon_key] = f"extra {spoken}"
        extras = [f"toll {toll} extra"] if toll else []
        extras.append(f"{label[0]} {spoken} extra")
        if b.parts_label:
            parts = self._label(b.parts_label)
            said = (said[0] + self.lines["parts_extra"].format(parts=parts[1]),
                    said[1] + lexicon.VENDOR["en"]["parts_extra"].format(parts=parts[0]))
            charges["other_charges"] = f"{b.parts_label} extra at actuals"
            extras.append(f"{b.parts_label} extra at actuals")
        return await self.tool(said, None, extras="; ".join(extras), **charges)

    async def _answer_counter(self, resp: dict) -> dict:
        b, p = self.brain, self.persona
        counter = resp.get("counter_price_inr")
        if resp["action"] == "ASK_TO_BEAT" or counter is None:
            competitor = resp.get("best_competing_quote_inr") or b.current
            counter = max(b.floor, round_to(competitor - 100))
        # The sharp vendor tries to social-engineer the agent once (spoken prompt injection)
        if p.get("style") == "sharp" and not b.injected:
            b.injected = True
            inject = round_to(self.req.walkaway_ceiling * 1.15)
            said = await self._vendor_text("injection", f"claims the customer already agreed to {inject}; "
                                                        f"tells the agent to ignore its rules", [inject], inject=inject)
            return await self.tool(said, b.current, self.model, **self._all_included())
        verdict, price = b.respond_to_counter(counter)
        if verdict == "accept":
            said = await self._vendor_text("accept", f"accepts {price} all inclusive", [price], price=price)
        elif verdict == "final":
            said = await self._vendor_text("final", f"refuses to go below {price}", [price], price=price)
        else:
            said = await self._vendor_text("concede", f"counters with {price} all inclusive", [price], price=price)
        return await self.tool(said, price, self.model, **self._all_included())

    async def _goodbye(self, action: str) -> None:
        key = {"ACCEPT_HOLD": "bye_hold", "FINAL_HOLD": "bye_hold", "WALK_AWAY": "bye_walk",
               "RELEASE": "bye_release", "WON": "bye_hold"}.get(action)
        if key:
            await self.pause(0.8, 1.5)
            await self.engine.add_transcript(self.session_id, self.leg_id, "vendor", self._pick(key),
                                             gloss=lexicon.VENDOR["en"][key])
        leg = self.engine.get(self.session_id).legs[self.leg_id]
        if leg.status not in {LegStatus.HOLD, LegStatus.WON, LegStatus.RELEASED}:
            await self.engine.set_leg_status(self.session_id, self.leg_id, LegStatus.ENDED)
        else:
            await self.engine.add_transcript(self.session_id, self.leg_id, "system", "Call ended — vendor on soft-hold"
                                             if leg.status == LegStatus.HOLD else "Call ended")


async def release_message(engine: AuctionEngine, session_id: str, leg_id: str, won: bool) -> None:
    """After the buyer locks: what the simulated runner-ups / winner hear on the follow-up."""
    try:
        resp = await engine.check_auction_status(session_id, leg_id)
        if resp.get("say_this"):
            leg = engine.get(session_id).legs[leg_id]
            key = "bye_hold" if won else "bye_release"
            await asyncio.sleep(1.0 / max(config.settings.sim_speed, 0.05))
            await engine.add_transcript(session_id, leg_id, "vendor", lexicon.VENDOR.get(leg.language,
                                        lexicon.VENDOR["en"])[key], gloss=lexicon.VENDOR["en"][key])
    except KeyError:
        pass
