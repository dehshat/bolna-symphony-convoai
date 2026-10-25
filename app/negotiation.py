"""Deterministic negotiation strategy: LLMs choose words, Python chooses numbers.

Every rupee amount the negotiator speaks comes from `decide()`. The walk-away ceiling never leaves
this module (it is not in any prompt), so a vendor cannot talk the voice LLM above it.

Leverage is truthful-only: a competitor price is cited only if another leg really quoted it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from app import services
from app.pricing import NormalizedQuote, ServiceRequest, category_rules, format_inr, round_down

MAX_ROUNDS = 3
# Fraction of the gap between target and anchor we offer, per counter round (concede slowly)
CONCESSION_CURVE = (0.25, 0.5, 0.75)
LEVERAGE_GAP = 50  # a leverage counter is always at least this much below the competitor quote

LANGUAGE_NAMES = {"kn": "Kannada", "hi": "Hindi", "en": "English", "ta": "Tamil", "mr": "Marathi"}

# Sarvam's TTS pronounces native script far better than romanized text ("ठीक है" vs "theek hai").
SCRIPT_HINT = {
    "hi": "Write it in Devanagari script (e.g. 'ठीक है भैया'), keeping words like toll, AC, UPI, WhatsApp in English",
    "kn": "Write it in Kannada script (e.g. 'ಸರಿ ಅಣ್ಣ'), keeping words like toll, AC, UPI, WhatsApp in English",
    "ta": "Write it in Tamil script, keeping words like toll, AC, UPI, WhatsApp in English",
    "mr": "Write it in Devanagari script, keeping words like toll, AC, UPI, WhatsApp in English",
    "en": "Write it in simple Indian English",
}

# Romanized lines, natural to read on screen and to synthesize. The live Bolna LLM rephrases them
# into the call's language but must keep every number.
TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "ASK_VENDOR_FIRST": "The customer is comparing three options. What's your best all-inclusive price?",
        "STALL_FOR_ANCHOR": "One moment, I'm checking with the customer. Meanwhile, what's your best all-inclusive price?",
        "ASK_TO_BEAT": "Another {who} nearby quoted {competitor}, all-inclusive. Can you do better?",
        "ASK_INCLUSIONS": "{quoted}, got it. Does that include {items}, or is that extra?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — is that per kilometre, or for the whole trip?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre? Or is that the total?",
        "CLARIFY_price_too_high_maybe_misheard": "Sorry, {quoted}? Could you tell me the total once more?",
        "CLARIFY_no_price": "What would the total be, all-inclusive?",
        "COUNTER_LEVERAGE": "Another {who} nearby quoted {competitor}, all-inclusive. If you can do {counter}, the customer will send a UPI advance as soon as they confirm.",
        "COUNTER_LEVERAGE_AGAIN": "I have {competitor} on the table. Can you do {counter} as your final?",
        "COUNTER_LEADER": "Yours is the best rate so far. If you make it {counter}, I'll recommend you right away.",
        "COUNTER_LEADER_AGAIN": "Just a little more. Can you do {counter}?",
        "COUNTER_BUDGET": "The customer's budget is around {target}. Can you do {counter}, all-inclusive?",
        "COUNTER_PUSH": "Please make it {counter}, all-inclusive, and I'll tell the customer right now.",
        "ACCEPT_HOLD": "Great, {price} all-inclusive. I'll confirm with the customer and WhatsApp you in five minutes. Please hold the slot.",
        "FINAL_HOLD": "Okay, {price} as your final, noted. I'll WhatsApp you in five minutes.",
        "WALK_AWAY": "I see, {price} is above the customer's budget. Thanks for your time, we'll call if anything changes.",
        "RELEASE": "The customer has gone with another option. Thank you so much for your time!",
        "WON": "Good news, the customer confirmed you at {price}. Details and the UPI advance are coming on WhatsApp.",
        "REMINDER": "You had said {previous} earlier. ",
        "UNIT_PREFIX": "{rate} per {unit} comes to about {total} for the full job. ",
        "NOT_AVAILABLE": "No problem, thank you for your time!",
        "ASK_SLOT": "Do you have a slot {when}? If not, what's the earliest you can do?",
        "ASK_FEE": "{slot} works. What would the fee be, all-inclusive?",
        "HOLD_SLOT": "Please note {slot} for the customer{fee_part}. I'll confirm on WhatsApp in five minutes.",
        "BOOKING_OVER_BUDGET": "{fee} is above the customer's budget. Thank you, we'll call back if needed.",
        "WON_BOOKING": "Good news, the customer confirms {slot}{fee_part}. Details are coming on WhatsApp.",
        "FEE_PART": ", fee {fee}",
    },
    "hi": {
        "ASK_VENDOR_FIRST": "Customer teen jagah rate check kar rahe hain. Aapka best rate kya hoga, sab milake?",
        "STALL_FOR_ANCHOR": "Ek minute ji, customer se baat kar raha hoon. Tab tak aapka best rate bata dijiye, sab milake?",
        "ASK_TO_BEAT": "Paas ke ek {who} ne {competitor} sab milake bola hai. Aap isse kam karoge?",
        "ASK_INCLUSIONS": "{quoted} theek hai. Isme {items} shaamil hai, ya alag lagega?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — yeh per kilometre hai ya poore trip ka?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre? Ya poore trip ka total?",
        "CLARIFY_price_too_high_maybe_misheard": "Maaf kijiye, {quoted}? Ek baar total phir se bata dijiye.",
        "CLARIFY_no_price": "Total kitna hoga ji, sab milake?",
        "COUNTER_LEVERAGE": "Bhaiya, paas ke ek {who} ne {competitor} sab milake bola hai. Aap {counter} kar do, toh customer confirm hote hi UPI advance bhej denge.",
        "COUNTER_LEVERAGE_AGAIN": "Bhaiya, {competitor} ka offer saamne hai. {counter} last kar do?",
        "COUNTER_LEADER": "Abhi tak aapka rate sabse achha hai. {counter} kar do, toh main aapka naam aage bhej deta hoon.",
        "COUNTER_LEADER_AGAIN": "Bas thoda aur bhaiya. {counter} kar do?",
        "COUNTER_BUDGET": "Customer ka budget {target} ke aas-paas hai. {counter} sab milake ho jayega?",
        "COUNTER_PUSH": "Bhaiya, {counter} kar do na, sab milake. Main abhi customer ko bata deta hoon.",
        "ACCEPT_HOLD": "Theek hai, {price} sab milake. Customer se confirm karke paanch minute mein WhatsApp karta hoon, tab tak hold rakhiye.",
        "FINAL_HOLD": "Theek hai, {price} aapka final, note kar liya. Paanch minute mein WhatsApp karta hoon.",
        "WALK_AWAY": "Achha, {price} budget se zyada ho raha hai. Shukriya bhaiya, zarurat padi toh phir call karenge.",
        "RELEASE": "Customer ne doosra option le liya hai. Aapka time dene ke liye bahut shukriya!",
        "WON": "Badhai ho, customer ne aapko {price} pe confirm kiya hai. Details aur UPI advance WhatsApp pe aa rahe hain.",
        "REMINDER": "Aapne pehle {previous} bola tha. ",
        "UNIT_PREFIX": "{rate} per {unit} matlab poore trip ka lagbhag {total}. ",
        "NOT_AVAILABLE": "Koi baat nahi ji, shukriya!",
        "ASK_SLOT": "{when} ka koi slot milega? Nahi toh sabse jaldi kab ho sakta hai?",
        "ASK_FEE": "{slot} theek hai. Fees kitni hogi, sab milake?",
        "HOLD_SLOT": "{slot} customer ke liye note kar lijiye{fee_part}. Paanch minute mein WhatsApp pe confirm karta hoon.",
        "BOOKING_OVER_BUDGET": "{fee} budget se zyada hai. Shukriya, zarurat padi toh call karenge.",
        "WON_BOOKING": "Customer ne {slot} confirm kiya hai{fee_part}. Details WhatsApp pe aa rahe hain.",
        "FEE_PART": ", fees {fee}",
    },
    "kn": {
        "ASK_VENDOR_FIRST": "Customer mooru kade rate nodtidare anna. Nimma best rate eshtu, ella serisi?",
        "STALL_FOR_ANCHOR": "Ondu nimisha anna, customer hatra maatadtini. Allivaregu nimma best rate heli, ella serisi?",
        "ASK_TO_BEAT": "Hattirada ondu {who} {competitor} ella serisi heliddare. Adakkintha kammi madtira?",
        "ASK_INCLUSIONS": "{quoted} sari. Adralli {items} serisiddira, illa bere aagutta?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — adu per kilometre aa, illa full trip ge aa?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre aa? Illa adu total aa?",
        "CLARIFY_price_too_high_maybe_misheard": "Sorry anna, {quoted}? Total innondsala heli.",
        "CLARIFY_no_price": "Total eshtu aagutte anna, ella serisi?",
        "COUNTER_LEVERAGE": "Anna, hattirada ondu {who} {competitor} ella serisi heliddare. Neevu {counter} madidre, customer confirm aadtakshana UPI advance kalistare.",
        "COUNTER_LEVERAGE_AGAIN": "Anna, {competitor} offer nanna hatra ide. {counter} final madtira?",
        "COUNTER_LEADER": "Ivaregu nimdhe best rate anna. {counter} madidre, ega ne nimma hesaru customer ge kalistini.",
        "COUNTER_LEADER_AGAIN": "Innu swalpa anna. {counter} madtira?",
        "COUNTER_BUDGET": "Customer budget {target} hattira ide anna. {counter} ella serisi aagutta?",
        "COUNTER_PUSH": "Anna, {counter} madi, ella serisi. Ega ne customer ge heltini.",
        "ACCEPT_HOLD": "Sari anna, {price} ella serisi. Customer hatra confirm maadi aidu nimishadalli WhatsApp madtini, hold madi.",
        "FINAL_HOLD": "Sari anna, {price} nimma final, note madkonde. Aidu nimishadalli WhatsApp madtini.",
        "WALK_AWAY": "Sari anna, {price} budget ginta jaasti aagutte. Thanks, beku aadre matte call madtini.",
        "RELEASE": "Customer bere option aaykondidare anna. Nimma time ge tumba thanks!",
        "WON": "Olle suddi anna, customer nimmanna {price} ge confirm madidare. Details mattu UPI advance WhatsApp nalli bartide.",
        "REMINDER": "Neevu modlu {previous} helidri anna. ",
        "UNIT_PREFIX": "{rate} per {unit} andre full trip ge sumaaru {total}. ",
        "NOT_AVAILABLE": "Paravagilla anna, thanks!",
        "ASK_SLOT": "{when} slot sigutta? Illa andre bega yavaga aagutte?",
        "ASK_FEE": "{slot} sari. Fees eshtu aagutte, ella serisi?",
        "HOLD_SLOT": "{slot} customer ge note madkolli{fee_part}. Aidu nimishadalli WhatsApp nalli confirm madtini.",
        "BOOKING_OVER_BUDGET": "{fee} budget ginta jaasti. Thanks, beku aadre call madtini.",
        "WON_BOOKING": "Customer {slot} confirm madidare{fee_part}. Details WhatsApp nalli bartide.",
        "FEE_PART": ", fees {fee}",
    },
    "ta": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare panraanga. Neenga mudhalla unga best all-inclusive rate sollunga.",
        "STALL_FOR_ANCHOR": "Oru nimisham anna, customer kitta confirm panren. Adhukkulla unga best all-inclusive rate enna?",
        "ASK_TO_BEAT": "Pakkathula innoru {who} {competitor} all-inclusive sonnaanga. Adhai vida kammi panna mudiyuma?",
        "ASK_INCLUSIONS": "{quoted} note panniten. Adhula {items} serthaa, illa extra-va?",
        "CLARIFY_no_price": "Motham all-inclusive evvalavu aagum anna?",
        "COUNTER_LEVERAGE": "Anna, pakkathula innoru {who} ippo dhaan {competitor} all-inclusive quote pannaanga. Neenga {counter} all-inclusive match panna, customer confirm pannadhum UPI token advance anuppuvaanga.",
        "COUNTER_LEADER": "Ippo varaikkum unga rate dhaan best anna. {counter} all-inclusive panna, ippove customer kitta unga per recommend panren.",
        "COUNTER_BUDGET": "Customer budget {target} kitta irukku anna. {counter} all-inclusive mudiyuma?",
        "COUNTER_PUSH": "Anna, {counter} all-inclusive pannunga, ippove customer kitta recommend panren.",
        "ACCEPT_HOLD": "Sari anna, {price} all-inclusive note panniten. Customer kitta confirm panni 5 nimishathula WhatsApp panren.",
        "FINAL_HOLD": "Sari anna, {price} all-inclusive unga final, note panniten. Customer kitta confirm panni 5 nimishathula WhatsApp panren.",
        "WALK_AWAY": "Sari anna, {price} customer budget-ku mela irukku. Nandri, edhavadhu maarina call panrom.",
        "RELEASE": "Customer vera option final pannitaanga. Romba nandri anna!",
        "WON": "Nalla seidhi anna, customer ungalai {price}-ku confirm pannitaanga. Details WhatsApp-la varum.",
        "REMINDER": "Neenga mudhalla {previous} sonneenga anna. ",
        "UNIT_PREFIX": "{rate} per {unit} na motham trip-ku {total} aagum. ",
        "NOT_AVAILABLE": "Paravaayilla anna, nandri!",
        "COUNTER_LEVERAGE_AGAIN": "Anna, {competitor} offer en kitta irukku. {counter} final panna mudiyuma?",
        "COUNTER_LEADER_AGAIN": "Innum konjam anna. {counter} panna mudiyuma?",
    },
    "mr": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare kartayt. Tumhi aadhi tumcha best all-inclusive rate saanga.",
        "COUNTER_LEVERAGE": "Dada, javalchya ekā {who} ne ata {competitor} all-inclusive quote kela aahe. Tumhi {counter} all-inclusive match kela tar customer confirm zalyavar lagech UPI token advance pathavtil.",
        "ACCEPT_HOLD": "Theek aahe, {price} all-inclusive note kela. Customer la vicharun 5 minitat WhatsApp karto.",
        "RELEASE": "Customer ne dusra option final kela aahe. Khup dhanyavaad!",
    },
}

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
}


def line(language: str, key: str, **kwargs) -> str:
    table = TEMPLATES.get(language) or TEMPLATES["en"]
    template = table.get(key) or TEMPLATES["hi" if language == "mr" else "en"].get(key) or TEMPLATES["en"][key]
    return template.format(**{k: format_inr(v) if isinstance(v, int) else v for k, v in kwargs.items()})


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

    def to_dict(self) -> dict:
        return asdict(self)


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
           available: bool = True, explain_unit: bool = False) -> Decision:
    """Pick the next move for one call leg. Mutates `state` (rounds, stalls, counters)."""
    who = services.get(req.category, req.service_label).who

    def line_(key: str, **kw) -> str:
        return line(language, key, who=who, **kw)

    lang_name = LANGUAGE_NAMES.get(language, "English")
    T, C = req.target_price, req.walkaway_ceiling

    def done(action: str, say: str, **kw) -> Decision:
        d = Decision(action=action, say=say, **kw)
        follow = FOLLOW_UP.get(action, "Then stop and listen. Call submit_and_counter again as soon as the vendor "
                                       "states any price or charge.")
        d.end_call = action in FOLLOW_UP
        d.instruction = (f"Say this next, as one short natural spoken turn in colloquial {lang_name}: \"{say}\" "
                         f"{SCRIPT_HINT.get(language, SCRIPT_HINT['en'])}. Keep every rupee amount exactly as given "
                         f"(digits with ₹). You may start with a tiny acknowledgement like 'achha' or 'theek hai'. "
                         f"Do not add any other price, promise or question. {follow}")
        return d

    if req.goal == "appointment":
        return _decide_booking(req, state, quote, language, locked_leg_id, leg_id, available, done, line_)

    # 1. A winner is locked: release everyone else
    if locked_leg_id:
        if locked_leg_id == leg_id and state.best_all_in:
            return done("WON", line_("WON", price=state.best_all_in), binding_price=state.best_all_in)
        return done("RELEASE", line_("RELEASE"), reason="buyer_locked_other_vendor")
    if not available and (quote is None or quote.clarify_reason == "no_price"):
        return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="vendor_not_available")

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
        return done("CLARIFY", line_(key if key in TEMPLATES["en"] else "CLARIFY_no_price",
                                    quoted=quote.stated_price or 0), reason=quote.clarify_reason)

    # 4. Hidden add-ons: ask once, then pad with conservative estimates
    if quote.status == "NEEDS_INCLUSIONS" and state.inclusion_asks < 1:
        state.inclusion_asks += 1
        labels = category_rules(req.category)["labels"]
        items = " aur ".join(labels[k] for k in quote.unknown_items) if language == "hi" else \
            " and ".join(labels[k] for k in quote.unknown_items)
        return done("ASK_INCLUSIONS", line_("ASK_INCLUSIONS", quoted=quote.stated_price, items=items),
                    reason="hidden_charges_unknown", notes=list(quote.unknown_items))

    q = state.best_all_in if state.best_all_in is not None else quote.all_in
    prefix = line_("REMINDER", previous=previous_best) if raised_price and previous_best else ""
    unit_names = {"per_km": "km", "per_hour": "hour", "per_day": "day", "per_month": "month", "per_visit": "visit"}
    if explain_unit and quote.pricing_model in unit_names and quote.base_price and \
            quote.base_price != quote.stated_price:
        unit = unit_names[quote.pricing_model]
        prefix += line_("UNIT_PREFIX", rate=quote.stated_price, unit=unit, total=quote.base_price)
    other = competitor.price if competitor is not None else None

    def hold(action: str, reason: str) -> Decision:
        if q > C:  # deterministic guardrail: never hold above the walk-away ceiling
            return done("WALK_AWAY", prefix + line_("WALK_AWAY", price=q), reason="above_ceiling")
        return done(action, prefix + line_(action, price=q), binding_price=q, reason=reason)

    # 5. Vendor met (or beat) our last counter, or is already at target
    if state.last_counter is not None and q <= state.last_counter:
        return hold("ACCEPT_HOLD", "vendor_met_counter")
    if q <= T:
        return hold("ACCEPT_HOLD", "at_or_below_target")

    # 5b. A vendor who answers two counters without moving is at their floor: stop haggling, close politely
    if state.last_counter is not None and previous_best is not None and quote.status == "VALID":
        state.no_move = state.no_move + 1 if q >= previous_best else 0
        if state.no_move >= 2:
            return hold("FINAL_HOLD", "vendor_firm")

    # 6. Out of rounds: hold if within ceiling, else walk away
    if state.rounds >= MAX_ROUNDS:
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
        return done("COUNTER", counter_line("COUNTER_BUDGET", target=T), counter_price=counter,
                    reason="budget_frame")
    return done("COUNTER", counter_line("COUNTER_PUSH"), counter_price=counter, reason="push_no_competitor")


def _decide_booking(req: ServiceRequest, state: LegNegotiationState, quote: Optional[NormalizedQuote], language: str,
                    locked_leg_id: Optional[str], leg_id: str, available: bool, done, line_) -> Decision:
    """Appointments (clinic, dentist, salon…): get a slot and the fee, hold it, never haggle."""
    fee = quote.all_in if quote is not None and quote.status != "CLARIFY" and quote.all_in else None
    fee_part = line_("FEE_PART", fee=fee) if fee else ""
    when = req.when or ("today" if language == "en" else "aaj" if language == "hi" else "ivattu")
    if locked_leg_id:
        if locked_leg_id == leg_id and state.slot:
            return done("WON_BOOKING", line_("WON_BOOKING", slot=state.slot, fee_part=fee_part), binding_price=fee)
        return done("RELEASE", line_("RELEASE"), reason="buyer_locked_other_vendor")
    if not available and not state.slot:
        return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="vendor_not_available")
    if not state.slot:
        state.slot_asks += 1
        if state.slot_asks > 2:
            return done("NOT_AVAILABLE", line_("NOT_AVAILABLE"), reason="no_slot_offered")
        return done("ASK_SLOT", line_("ASK_SLOT", when=when), reason="need_slot")
    if fee is None and state.fee_asks < 1:
        state.fee_asks += 1
        return done("ASK_FEE", line_("ASK_FEE", slot=state.slot), reason="need_fee")
    if fee and req.has_budget and fee > req.walkaway_ceiling:
        return done("BOOKING_OVER_BUDGET", line_("BOOKING_OVER_BUDGET", fee=fee), reason="fee_above_ceiling")
    return done("HOLD_SLOT", line_("HOLD_SLOT", slot=state.slot, fee_part=fee_part), binding_price=fee,
                reason="slot_held")
