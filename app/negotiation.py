"""Deterministic negotiation strategy: LLMs choose words, Python chooses numbers.

Every rupee amount the negotiator speaks comes from `decide()`. The walk-away ceiling never leaves
this module (it is not in any prompt), so a vendor cannot talk the voice LLM above it.

Leverage is truthful-only: a competitor price is cited only if another leg really quoted it.
"""

from __future__ import annotations

import re
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from typing import Optional

from app import config, languages, lexicon, services
from app.lexicon import TEMPLATES, charge_words, localize_when, spoken_en
from app.pricing import NormalizedQuote, ServiceRequest, category_rules, round_down

MAX_ROUNDS = 3   # hard upper bound; the per-call limit is MAX_COUNTER_ROUNDS (default 1: one fair counter, then close)
# Fraction of the gap between target and anchor we offer, per counter round (concede slowly)
CONCESSION_CURVE = (0.25, 0.5, 0.75)
CLOSE_ENOUGH = 0.05             # within 5% (min ₹50) of the target: accept, don't haggle over small change
LEVERAGE_GAP = 50  # a leverage counter is always at least this much below the competitor quote

LANGUAGE_NAMES = {code: lang.name for code, lang in languages.LANGUAGES.items()}


def script_hint(language: str) -> str:
    """What the live voice LLM is told about the language of a line it is about to speak."""
    if language not in lexicon.NATIVE:
        return "Say it in simple, warm Indian English"
    lang = languages.get(language)
    return (f"It is already in {lang.name}: say it in {lang.name} only ({lang.script} script), with no English words; "
            f"brand names like WhatsApp stay in {lang.script} script too")


# What the live Bolna LLM is told to do after speaking the line
FOLLOW_UP = {
    "ACCEPT_HOLD": "Do NOT say 'booked' or 'confirmed'. Thank them and end the call politely.",
    "FINAL_HOLD": "Do NOT say 'booked' or 'confirmed'. Thank them and end the call politely.",
    "WALK_AWAY": "Do not offer any other number. Thank them and end the call politely.",
    "RELEASE": "Then end the call politely. Do not negotiate further.",
    "WON": "Then end the call politely.",
    "NOT_AVAILABLE": "Then end the call politely.",
    "HOLD_SLOT": "Do NOT say 'booked' or 'confirmed'. Thank them and end the call politely.",
    "BOOKING_OVER_BUDGET": "Thank them and end the call politely.",
    "WON_BOOKING": "Answer any quick question, thank them and end the call politely.",
    "CALL_BACK": "Then end the call politely. Do not ask for a price.",
    "INSPECTION_HOLD": "Do NOT say 'booked' or 'confirmed'. Thank them and end the call politely.",
}

# What the vendor is doing this turn. Bolna's LLM sends it as `situation`; if it doesn't, it is inferred from
# vendor_said (an English paraphrase, so English patterns plus the commonest Hindi/Kannada ones).
SITUATIONS = {"quoting", "accepts_offer", "asks_budget", "needs_inspection", "busy_call_later", "not_available",
              "other"}
_BUSY_RE = re.compile(r"\b(call (me )?(back|later|after|tomorrow|in)|busy|driving|in a meeting|not now|baad mein|"
                      r"abhi nahi|free nahi|aamele call)\b", re.I)
_INSPECT_RE = re.compile(r"(after (seeing|inspection|checking|visiting|looking)|have to see|need to see|must see|"
                         r"\binspect\w*|depends on (the )?(work|job|problem|damage)|can'?t (say|tell) (without|before|"
                         r"until)|cannot (say|tell)|only (after|once)|on site|(visit|come) (and|then|first)|"
                         r"dekh(ke| kar| ke)|nodi heltini|nodbeku)", re.I)
_ASKS_BUDGET_RE = re.compile(r"(\byour budget|what('s| is) (the |your )?budget|how much (can|will|do) you "
                             r"(pay|give|want)|budget (kitna|eshtu)|kitna doge)", re.I)
_AGREE_RE = re.compile(r"\b(ok(ay)?|fine|agreed?|deal|done|accept\w*|will do|can do|no problem|alright|sure|"
                       r"theek hai|thik hai|chalo|ho jayega|sari|aagutte|confirm)\b", re.I)
_REFUSE_RE = re.compile(r"\b(not|no|can'?t|cannot|won'?t|nahi|nahin|illa|last|minimum|lowest|final is|more)\b",
                        re.I)


def classify_situation(said: str, given: str = "", has_price: bool = False) -> str:
    given = (given or "").strip().lower()
    if given in SITUATIONS and given != "other":
        return given
    said = said or ""
    if _BUSY_RE.search(said) and not has_price:
        return "busy_call_later"
    if _INSPECT_RE.search(said):
        return "needs_inspection"
    if _ASKS_BUDGET_RE.search(said) and not has_price:
        return "asks_budget"
    plain = re.sub(r"\bno problem\b", "", said, flags=re.I)
    if _AGREE_RE.search(said) and not _REFUSE_RE.search(plain) and not has_price:
        return "accepts_offer"
    return "quoting" if has_price else "other"


def line(language: str, key: str, who: str = "vendor", **kwargs) -> str:
    """One negotiation line in the call's language. Only English lines name the kind of vendor ({who})."""
    return lexicon.render(language, key, who=who, **kwargs)


def _cap(text: str) -> str:
    return text[:1].upper() + text[1:]


@dataclass
class Utter:
    """A line in the call's language plus its English meaning (the screen shows it as a translation)."""
    text: str
    en: str

    def __add__(self, other: "Utter | str") -> "Utter":
        if isinstance(other, Utter):
            return Utter(self.text + other.text, self.en + other.en)
        return Utter(self.text + other, self.en + other)

    def __radd__(self, other: str) -> "Utter":
        return Utter(other + self.text, other + self.en)

    def __bool__(self) -> bool:
        return bool(self.text)

    def capitalized(self) -> "Utter":
        return Utter(_cap(self.text), _cap(self.en))

    def as_clause(self) -> "Utter":
        """' parts separately…' -> ' Parts separately…' (a sentence of its own after the main line)."""
        return Utter(" " + _cap(self.text.strip()), " " + _cap(self.en.strip()))


@dataclass
class Competitor:
    leg_id: str
    vendor_name: str
    price: int


@dataclass
class LegNegotiationState:
    """The slice of a call leg that the strategy needs. Owned and mutated by the AuctionEngine."""
    best_all_in: Optional[int] = None
    last_counter: Optional[int] = None
    rounds: int = 0
    stalls: int = 0
    inclusion_asks: int = 0
    clarifies: int = 0
    no_move: int = 0          # consecutive counters the vendor answered without coming down
    last_kind: str = ""       # template used for the last counter, to vary repeated lines
    slot: str = ""            # appointment slot the provider offered
    slot_asks: int = 0
    fee_asks: int = 0
    inspection: bool = False  # vendor prices only after seeing the job
    inspection_asks: int = 0
    inspection_fee: Optional[int] = None   # visiting / inspection charge they did state


@dataclass
class Decision:
    action: str
    say: str
    counter_price: Optional[int] = None
    binding_price: Optional[int] = None
    leverage: Optional[dict] = None
    reason: str = ""
    end_call: bool = False
    instruction: str = ""
    notes: list = field(default_factory=list)
    say_en: str = ""          # English meaning of `say`, for the screen

    def to_dict(self) -> dict:
        return asdict(self)


# The self-test pins the counter policy for its own task only, never for calls running alongside it
ROUNDS_OVERRIDE: ContextVar[Optional[int]] = ContextVar("rounds_override", default=None)


def max_rounds() -> int:
    override = ROUNDS_OVERRIDE.get()
    return min(MAX_ROUNDS, config.settings.max_counter_rounds if override is None else override)


def counter_for_round(req: ServiceRequest, anchor: int, rounds: int, competitor: Optional[int]) -> int:
    """Buyer-favourable counter: concede a growing share of the target→anchor gap, never above ceiling."""
    fraction = CONCESSION_CURVE[min(rounds, len(CONCESSION_CURVE) - 1)]
    counter = round_down(req.target_price + max(anchor - req.target_price, 0) * fraction)
    if competitor is not None:
        counter = min(counter, round_down(competitor - LEVERAGE_GAP))
    return max(req.target_price, min(counter, req.walkaway_ceiling))


def decide(req: ServiceRequest, state: LegNegotiationState, quote: Optional[NormalizedQuote],
           competitor: Optional[Competitor], language: str = "en", locked_leg_id: Optional[str] = None,
           leg_id: str = "", previous_best: Optional[int] = None, raised_price: bool = False,
           available: bool = True, explain_unit: bool = False, excluded: Optional[list] = None,
           situation: str = "") -> Decision:
    """Pick the next move for one call leg. Mutates `state` (rounds, stalls, counters).

    `excluded`: labels of things the vendor bills separately (parts, gas, material). They are outside the budget;
    the closing line repeats them back so both sides agree on what the price covers."""
    who = services.get(req.category, req.service_label).who
    language = language if language in lexicon.ALL else "en"

    def line_(key: str, **kw) -> Utter:
        """kw values: int (rupees), str (same in both), (english, native) tuple, or an Utter."""
        def pick(v, i):
            if isinstance(v, Utter):
                return (v.en, v.text)[i]
            return v[i] if isinstance(v, tuple) else v
        return Utter(line(language, key, who=who, **{k: pick(v, 1) for k, v in kw.items()}),
                     line("en", key, who=who, **{k: pick(v, 0) for k, v in kw.items()}))

    def items_(labels: list[str], joiner: str = "and") -> tuple[str, str]:
        """(English, native) names for charges, e.g. ('toll and driver bata', 'टोल और ड्राइवर का भत्ता')."""
        en = list(dict.fromkeys(spoken_en(x) for x in labels if x))
        nat = list(dict.fromkeys(charge_words(x, language) for x in labels if x))
        return f" {joiner} ".join(en), f" {lexicon.word(language, joiner)} ".join(nat)

    def when_(text: str) -> tuple[str, str]:
        return text, localize_when(text, language)

    T, C = req.target_price, req.walkaway_ceiling

    def done(action: str, say: Utter, **kw) -> Decision:
        say = say.capitalized()
        d = Decision(action=action, say=say.text, say_en=say.en, **kw)
        follow = FOLLOW_UP.get(action, "Then stop and listen. Call submit_and_counter again as soon as the vendor "
                                       "states any price or charge.")
        d.end_call = action in FOLLOW_UP
        d.instruction = (f"Say this next, as one short natural spoken turn: \"{say.text}\" ({script_hint(language)}.) "
                         f"It means: \"{say.en}\". Keep every rupee amount exactly as given (digits with ₹). You may "
                         f"start with a tiny acknowledgement like \"{lexicon.word(language, 'ack')}\". Do not add any "
                         f"other price, promise or question. {follow}")
        return d

    if req.goal == "appointment":
        return _decide_booking(req, state, quote, language, locked_leg_id, leg_id, available, done, line_, when_)

    # 1. A winner is locked: release everyone else
    if locked_leg_id:
        if locked_leg_id == leg_id and state.best_all_in:
            return done("WON", line_("WON", price=state.best_all_in), binding_price=state.best_all_in)
        return done("RELEASE", line_("RELEASE"), reason="buyer_locked_other_vendor")
    no_price = quote is None or quote.clarify_reason == "no_price"
    if (not available or situation == "not_available") and no_price:
        return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="vendor_not_available")
    if situation == "busy_call_later" and no_price:
        return done("CALL_BACK", line_("CALL_BACK"), reason="vendor_busy")

    # 1b. "I'll tell you after seeing the work": get a rough figure and the visiting charge, never haggle on it
    if situation == "needs_inspection":
        state.inspection = True
    if state.inspection and no_price:
        if state.inspection_asks < 1:
            state.inspection_asks += 1
            return done("ASK_ROUGH_ESTIMATE", line_("ASK_ROUGH_ESTIMATE"), reason="price_after_inspection")
        if state.inspection_fee:
            return done("INSPECTION_HOLD", line_("INSPECTION_HOLD", fee=state.inspection_fee),
                        reason="visit_fee_only", notes=["work price after inspection"])
        return done("INSPECTION_HOLD", line_("INSPECTION_HOLD_NOFEE"), reason="price_after_inspection")

    # 2. No price yet: never reveal budget first, and never invent a competitor
    if quote is None or quote.clarify_reason == "no_price":
        if competitor is not None:
            return done("ASK_TO_BEAT", line_("ASK_TO_BEAT", competitor=competitor.price),
                        leverage=asdict(competitor), reason="competitor_anchor_available")
        state.stalls += 1
        if state.stalls == 1:
            return done("ASK_VENDOR_FIRST", line_("ASK_VENDOR_FIRST"), reason="no_anchor_yet")
        if state.stalls == 2:
            return done("STALL_FOR_ANCHOR", line_("STALL_FOR_ANCHOR"), reason="waiting_for_anchor")
        state.rounds += 1
        state.last_counter = T
        return done("COUNTER", line_("COUNTER_BUDGET", target=T, counter=T), counter_price=T,
                    reason="budget_frame_no_anchor")

    # 3. Garbled or unit-confused price: ask again rather than negotiate on a wrong number
    if quote.status == "CLARIFY":
        state.clarifies += 1
        key = f"CLARIFY_{quote.clarify_reason}"
        if state.clarifies > 2:   # asked three times and still unclear: don't loop, close politely
            return done("INSPECTION_HOLD", line_("INSPECTION_HOLD_NOFEE"), reason="price_unclear")
        return done("CLARIFY", line_(key if key in TEMPLATES["en"] else "CLARIFY_no_price",
                                    quoted=quote.stated_price or 0), reason=quote.clarify_reason)

    # 4. Hidden add-ons (and parts/material we don't know about yet): ask once, then pad add-ons conservatively.
    #    Not for an inspection-priced job: its number is only an estimate, held as such below.
    ask = quote.ask_items or quote.unknown_items
    if ask and state.inclusion_asks < 1 and not state.inspection:
        state.inclusion_asks += 1
        labels = category_rules(req.category)["labels"]
        return done("ASK_INCLUSIONS", line_("ASK_INCLUSIONS", quoted=quote.stated_price,
                                            items=items_([labels[k] for k in ask])),
                    reason="hidden_charges_unknown", notes=list(ask))

    q = state.best_all_in if state.best_all_in is not None else quote.all_in
    prefix = line_("REMINDER", previous=previous_best) if raised_price and previous_best else ""
    if explain_unit and quote.pricing_model in lexicon.UNITS["en"] and quote.base_price and \
            quote.base_price != quote.stated_price:
        unit = (lexicon.unit("en", quote.pricing_model), lexicon.unit(language, quote.pricing_model))
        monthly = services.get(req.category, req.service_label).unit == "per_month"
        prefix += line_("UNIT_PREFIX_MONTH" if monthly else "UNIT_PREFIX", rate=quote.stated_price, unit=unit,
                        total=quote.base_price)
    other = competitor.price if competitor is not None else None

    named = [x for x in (excluded or []) if x][:3]
    excluded_note = line_("EXCLUDED_NOTE", items=items_(named)).as_clause() if named else ""

    def hold(action: str, reason: str) -> Decision:
        if q > C:  # deterministic guardrail: never hold above the walk-away ceiling
            return done("WALK_AWAY", prefix + line_("WALK_AWAY", price=q), reason="above_ceiling")
        return done(action, prefix + line_(action, price=q) + excluded_note, binding_price=q, reason=reason)

    # 4b. A price for an inspection-priced job is an estimate: hold it if it fits the budget, never haggle on it
    if state.inspection:
        if q > C:
            return done("WALK_AWAY", prefix + line_("WALK_AWAY", price=q), reason="estimate_above_ceiling")
        return done("INSPECTION_HOLD", prefix + line_("ESTIMATE_HOLD", price=q) + excluded_note, binding_price=q,
                    reason="estimate_after_inspection")

    # 5. Vendor met (or beat) our last counter, or is already at (or within small change of) the target
    if state.last_counter is not None and q <= state.last_counter:
        return hold("ACCEPT_HOLD", "vendor_met_counter")
    if q <= T:
        return hold("ACCEPT_HOLD", "at_or_below_target")
    if q <= min(C, T + max(LEVERAGE_GAP, round(T * CLOSE_ENOUGH))) and (other is None or q <= other):
        return hold("ACCEPT_HOLD", "close_enough_to_target")

    # 5b. A vendor who answers two counters without moving is at their floor: stop haggling, close politely
    if state.last_counter is not None and previous_best is not None and quote.status == "VALID":
        state.no_move = state.no_move + 1 if q >= previous_best else 0
        if state.no_move >= 2:
            return hold("FINAL_HOLD", "vendor_firm")

    # 6. Out of rounds: hold if within ceiling, else walk away
    if state.rounds >= max_rounds():
        return hold("FINAL_HOLD", "max_rounds")

    # 7. Counter
    anchor = min(q, other) if other is not None else q
    counter = counter_for_round(req, anchor, state.rounds, other if other is not None and other < q else None)
    if state.last_counter is not None and counter <= state.last_counter:
        # Our offers never go backwards, and each round moves a little so the call converges
        step = max(LEVERAGE_GAP, round_down((q - state.last_counter) * 0.3))
        counter = min(state.last_counter + step, C)
    if other is not None and other < q:
        # A leverage line must offer no more than the competitor's real quote: beat it, or at worst match it
        cap = round_down(other - LEVERAGE_GAP)
        counter = min(counter, cap if state.last_counter is None or cap >= state.last_counter else other)
        counter = max(counter, min(T, other))  # never push below target just to undercut: ask them to match it
    if counter >= q:
        return hold("ACCEPT_HOLD", "counter_not_needed")

    first_counter = state.rounds == 0
    state.rounds += 1
    state.last_counter = counter

    def counter_line(kind: str, **kw) -> str:
        # Say the full pitch once; on a repeat use the shorter follow-up so the call doesn't sound scripted
        key = f"{kind}_AGAIN" if state.last_kind == kind and f"{kind}_AGAIN" in TEMPLATES["en"] else kind
        state.last_kind = kind
        return prefix + line_(key, counter=counter, **kw)

    if other is not None and other < q:
        return done("COUNTER", counter_line("COUNTER_LEVERAGE", competitor=other), counter_price=counter,
                    leverage=asdict(competitor), reason="truthful_competitor_leverage")
    if other is not None and other > q:
        return done("COUNTER", counter_line("COUNTER_LEADER"), counter_price=counter,
                    reason="vendor_is_current_leader")
    if first_counter:
        kind = "COUNTER_AT_BUDGET" if counter == T else "COUNTER_BUDGET"
        return done("COUNTER", counter_line(kind, target=T), counter_price=counter, reason="budget_frame")
    return done("COUNTER", counter_line("COUNTER_PUSH"), counter_price=counter, reason="push_no_competitor")


def _decide_booking(req: ServiceRequest, state: LegNegotiationState, quote: Optional[NormalizedQuote], language: str,
                    locked_leg_id: Optional[str], leg_id: str, available: bool, done, line_, when_) -> Decision:
    """Appointments (clinic, dentist, salon…): get a slot and the fee, hold it, never haggle."""
    fee = quote.all_in if quote is not None and quote.status != "CLARIFY" and quote.all_in else None
    fee_part = line_("FEE_PART", fee=fee) if fee else ""
    if locked_leg_id:
        if locked_leg_id == leg_id and state.slot:
            return done("WON_BOOKING", line_("WON_BOOKING", slot=when_(state.slot), fee_part=fee_part),
                        binding_price=fee)
        return done("RELEASE", line_("RELEASE"), reason="buyer_locked_other_vendor")
    if not available and not state.slot:
        return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="vendor_not_available")
    if not state.slot:
        state.slot_asks += 1
        if state.slot_asks > 2:
            return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="no_slot_offered")
        return done("ASK_SLOT", line_("ASK_SLOT", when=when_(req.when or "today")), reason="need_slot")
    if fee is None and state.fee_asks < 1:
        state.fee_asks += 1
        return done("ASK_FEE", line_("ASK_FEE", slot=when_(state.slot)), reason="need_fee")
    if fee and req.has_budget and fee > req.walkaway_ceiling:
        return done("BOOKING_OVER_BUDGET", line_("BOOKING_OVER_BUDGET", fee=fee), reason="fee_above_ceiling")
    return done("HOLD_SLOT", line_("HOLD_SLOT", slot=when_(state.slot), fee_part=fee_part), binding_price=fee,
                reason="slot_held")
