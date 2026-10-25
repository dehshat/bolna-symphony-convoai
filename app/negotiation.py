"""Deterministic negotiation strategy: LLMs choose words, Python chooses numbers.

Every rupee amount the negotiator speaks comes from `decide()`. The walk-away ceiling never leaves
this module (it is not in any prompt), so a vendor cannot talk the voice LLM above it.

Leverage is truthful-only: a competitor price is cited only if another leg really quoted it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

from app.pricing import CATEGORIES, NormalizedQuote, ServiceRequest, format_inr, round_down

MAX_ROUNDS = 3
# Fraction of the gap between target and anchor we offer, per counter round (concede slowly)
CONCESSION_CURVE = (0.25, 0.5, 0.75)
LEVERAGE_GAP = 50  # a leverage counter is always at least this much below the competitor quote

LANGUAGE_NAMES = {"kn": "Kannada", "hi": "Hindi", "en": "English", "ta": "Tamil", "mr": "Marathi"}

# Romanized lines, natural to read on screen and to synthesize. The live Bolna LLM rephrases them
# into the call's language but must keep every number.
TEMPLATES: dict[str, dict[str, str]] = {
    "en": {
        "ASK_VENDOR_FIRST": "Customer is comparing 3 options. Please tell me your best all-inclusive price first.",
        "STALL_FOR_ANCHOR": "One minute sir, I'm checking with the customer. Meanwhile, what is your best all-inclusive rate?",
        "ASK_TO_BEAT": "Another operator nearby has quoted {competitor} all-inclusive. Can you do better than that?",
        "ASK_INCLUSIONS": "{quoted} noted. Is {items} included in that, or extra?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — is that per kilometre or the total for the full trip?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre? Or is that the total amount?",
        "CLARIFY_price_too_high_maybe_misheard": "Sorry, {quoted}? Please tell me the total amount once more.",
        "CLARIFY_no_price": "What will be the total all-inclusive amount?",
        "COUNTER_LEVERAGE": "Another operator nearby just quoted {competitor} all-inclusive. If you can match {counter} all-inclusive, the customer will send the UPI token advance right away on confirmation.",
        "COUNTER_LEADER": "Your rate is the best so far. If you can make it {counter} all-inclusive, I will recommend you to the customer right now.",
        "COUNTER_BUDGET": "Customer's budget is around {target}. Can you do {counter} all-inclusive?",
        "COUNTER_PUSH": "Sir, please make it {counter} all-inclusive and I'll recommend you to the customer right now.",
        "ACCEPT_HOLD": "Okay, {price} all-inclusive, noted. I'll confirm with the customer and WhatsApp you in 5 minutes. Please hold the slot for us.",
        "FINAL_HOLD": "Okay, {price} all-inclusive is your final, noted. I'll confirm with the customer and WhatsApp you in 5 minutes.",
        "WALK_AWAY": "Okay, {price} is above the customer's budget. Thank you for your time, we'll call back if anything changes.",
        "RELEASE": "The customer has finalised another option. Thank you so much for your time, have a good day!",
        "WON": "Good news, the customer has confirmed you at {price}. Booking details and UPI token advance are coming on WhatsApp.",
        "REMINDER": "You had said {previous} earlier. ",
        "UNIT_PREFIX": "{rate} per {unit} comes to about {total} for the full job. ",
        "NOT_AVAILABLE": "No problem, thank you for your time. Have a good day!",
    },
    "hi": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare kar rahe hain. Aap pehle apna best all-inclusive rate bataiye.",
        "STALL_FOR_ANCHOR": "Ek minute ji, customer se confirm kar raha hoon. Tab tak aapka best all-inclusive rate kya hoga?",
        "ASK_TO_BEAT": "Paas ke ek aur operator ne {competitor} all-inclusive bola hai. Aap isse kam kar sakte ho?",
        "ASK_INCLUSIONS": "{quoted} note kiya. Isme {items} shaamil hai ya alag se?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — yeh per kilometre hai ya poore trip ka total?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre? Ya yeh total amount hai?",
        "CLARIFY_price_too_high_maybe_misheard": "Sorry, {quoted}? Total amount ek baar phir bataiye.",
        "CLARIFY_no_price": "Total all-inclusive kitna hoga ji?",
        "COUNTER_LEVERAGE": "Bhaiya, paas ke ek aur operator ne abhi {competitor} all-inclusive quote kiya hai. Agar aap {counter} all-inclusive match kar do, toh customer confirm hote hi UPI token advance turant bhej denge.",
        "COUNTER_LEADER": "Abhi tak aapka rate sabse best hai. {counter} all-inclusive kar do, toh main abhi customer ko aapka naam recommend karta hoon.",
        "COUNTER_BUDGET": "Customer ka budget {target} ke aas-paas hai. {counter} all-inclusive ho jayega?",
        "COUNTER_PUSH": "Bhaiya, {counter} all-inclusive kar do, main abhi customer ko aapka naam recommend karta hoon.",
        "ACCEPT_HOLD": "Theek hai, {price} all-inclusive note kar liya. Customer se confirm karke 5 minute mein WhatsApp karta hoon. Slot hold rakhiye.",
        "FINAL_HOLD": "Theek hai, {price} all-inclusive aapka final, note kar liya. Customer se confirm karke 5 minute mein WhatsApp karta hoon.",
        "WALK_AWAY": "Achha, {price} customer ke budget se upar hai. Aapka time dene ke liye shukriya, kuch hua toh call karenge.",
        "RELEASE": "Customer ne doosra option final kar liya hai. Aapka bahut shukriya, aapka din shubh ho!",
        "WON": "Badhai ho, customer ne aapko {price} pe confirm kar diya. Booking details aur UPI token advance WhatsApp pe aa raha hai.",
        "REMINDER": "Aapne pehle {previous} bola tha. ",
        "UNIT_PREFIX": "{rate} per {unit} matlab poore trip ka lagbhag {total} hota hai. ",
        "NOT_AVAILABLE": "Koi baat nahi ji, aapka time dene ke liye shukriya!",
    },
    "kn": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare madtidare anna. Neevu modlu nimma best all-inclusive rate heli.",
        "STALL_FOR_ANCHOR": "Ondu nimisha anna, customer hatra confirm madtini. Allivaregu nimma best all-inclusive rate eshtu?",
        "ASK_TO_BEAT": "Hattirada innond operator {competitor} all-inclusive heliddare. Adakkintha kammi madtira?",
        "ASK_INCLUSIONS": "{quoted} note madkonde. Adralli {items} serisiddira, illa extra na?",
        "CLARIFY_price_too_low_maybe_unit_rate": "{quoted} — adu per kilometre aa, illa full trip total aa?",
        "CLARIFY_per_km_rate_too_high": "{quoted} per kilometre aa? Illa adu total amount aa?",
        "CLARIFY_price_too_high_maybe_misheard": "Sorry anna, {quoted}? Total amount innond sala heli.",
        "CLARIFY_no_price": "Total all-inclusive eshtu aagutte anna?",
        "COUNTER_LEVERAGE": "Anna, hattirada innond operator ega tane {competitor} all-inclusive quote madidru. Neevu {counter} all-inclusive match madidre, customer confirm aadtakshana UPI token advance kalistare.",
        "COUNTER_LEADER": "Ivaregu nimdhe best rate anna. {counter} all-inclusive madidre, ega ne customer ge nimma hesaru recommend madtini.",
        "COUNTER_BUDGET": "Customer budget {target} hattira ide anna. {counter} all-inclusive aagutta?",
        "COUNTER_PUSH": "Anna, {counter} all-inclusive madi, ega ne customer ge nimma hesaru recommend madtini.",
        "ACCEPT_HOLD": "Sari anna, {price} all-inclusive note madkonde. Customer hatra confirm maadi 5 nimishadalli WhatsApp madtini. Slot hold madi.",
        "FINAL_HOLD": "Sari anna, {price} all-inclusive nimma final, note madkonde. Customer hatra confirm maadi 5 nimishadalli WhatsApp madtini.",
        "WALK_AWAY": "Sari anna, {price} customer budget ginta jaasti aagutte. Time kottidakke thanks, enadru change aadre call madtivi.",
        "RELEASE": "Customer bere option final madidare anna. Nimma time ge tumba thanks, olle dina aagli!",
        "WON": "Olle suddi anna, customer nimmanna {price} ge confirm madidare. Booking details mattu UPI token advance WhatsApp nalli bartide.",
        "REMINDER": "Neevu modlu {previous} helidri anna. ",
        "UNIT_PREFIX": "{rate} per {unit} andre full trip ge sumaaru {total} aagutte. ",
        "NOT_AVAILABLE": "Paravagilla anna, time kottidakke thanks!",
    },
    "ta": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare panraanga. Neenga mudhalla unga best all-inclusive rate sollunga.",
        "STALL_FOR_ANCHOR": "Oru nimisham anna, customer kitta confirm panren. Adhukkulla unga best all-inclusive rate enna?",
        "ASK_TO_BEAT": "Pakkathula innoru operator {competitor} all-inclusive sonnaanga. Adhai vida kammi panna mudiyuma?",
        "ASK_INCLUSIONS": "{quoted} note panniten. Adhula {items} serthaa, illa extra-va?",
        "CLARIFY_no_price": "Motham all-inclusive evvalavu aagum anna?",
        "COUNTER_LEVERAGE": "Anna, pakkathula innoru operator ippo dhaan {competitor} all-inclusive quote pannaanga. Neenga {counter} all-inclusive match panna, customer confirm pannadhum UPI token advance anuppuvaanga.",
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
    },
    "mr": {
        "ASK_VENDOR_FIRST": "Customer 3 options compare kartayt. Tumhi aadhi tumcha best all-inclusive rate saanga.",
        "COUNTER_LEVERAGE": "Dada, javalchya ekā operator ne ata {competitor} all-inclusive quote kela aahe. Tumhi {counter} all-inclusive match kela tar customer confirm zalyavar lagech UPI token advance pathavtil.",
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
    lang_name = LANGUAGE_NAMES.get(language, "English")
    T, C = req.target_price, req.walkaway_ceiling

    def done(action: str, say: str, **kw) -> Decision:
        d = Decision(action=action, say=say, **kw)
        follow = FOLLOW_UP.get(action, "Then wait for the vendor's answer and call submit_and_counter again "
                                       "as soon as they state any price or charge.")
        d.end_call = action in FOLLOW_UP
        d.instruction = (f"Say this now in natural, colloquial {lang_name}, keeping every rupee amount exactly as "
                         f"written: \"{say}\" Never offer, accept or mention any other price. {follow}")
        return d

    # 1. A winner is locked: release everyone else
    if locked_leg_id:
        if locked_leg_id == leg_id and state.best_all_in:
            return done("WON", line(language, "WON", price=state.best_all_in), binding_price=state.best_all_in)
        return done("RELEASE", line(language, "RELEASE"), reason="buyer_locked_other_vendor")
    if not available and (quote is None or quote.clarify_reason == "no_price"):
        return done("NOT_AVAILABLE", line(language, "NOT_AVAILABLE"), reason="vendor_not_available")

    # 2. No price yet: never reveal budget first, and never invent a competitor
    if quote is None or quote.clarify_reason == "no_price":
        if competitor is not None:
            return done("ASK_TO_BEAT", line(language, "ASK_TO_BEAT", competitor=competitor.price),
                        leverage=asdict(competitor), reason="competitor_anchor_available")
        state.stalls += 1
        if state.stalls == 1:
            return done("ASK_VENDOR_FIRST", line(language, "ASK_VENDOR_FIRST"), reason="no_anchor_yet")
        if state.stalls == 2:
            return done("STALL_FOR_ANCHOR", line(language, "STALL_FOR_ANCHOR"), reason="waiting_for_anchor")
        state.rounds += 1
        state.last_counter = T
        return done("COUNTER", line(language, "COUNTER_BUDGET", target=T, counter=T), counter_price=T,
                    reason="budget_frame_no_anchor")

    # 3. Garbled or unit-confused price: ask again rather than negotiate on a wrong number
    if quote.status == "CLARIFY":
        state.clarifies += 1
        key = f"CLARIFY_{quote.clarify_reason}"
        return done("CLARIFY", line(language, key if key in TEMPLATES["en"] else "CLARIFY_no_price",
                                    quoted=quote.stated_price or 0), reason=quote.clarify_reason)

    # 4. Hidden add-ons: ask once, then pad with conservative estimates
    if quote.status == "NEEDS_INCLUSIONS" and state.inclusion_asks < 1:
        state.inclusion_asks += 1
        labels = CATEGORIES[req.category]["labels"]
        items = " aur ".join(labels[k] for k in quote.unknown_items) if language == "hi" else \
            " and ".join(labels[k] for k in quote.unknown_items)
        return done("ASK_INCLUSIONS", line(language, "ASK_INCLUSIONS", quoted=quote.stated_price, items=items),
                    reason="hidden_charges_unknown", notes=list(quote.unknown_items))

    q = state.best_all_in if state.best_all_in is not None else quote.all_in
    prefix = line(language, "REMINDER", previous=previous_best) if raised_price and previous_best else ""
    if explain_unit and quote.pricing_model != "flat" and quote.base_price:
        unit = {"per_km": "km", "per_hour": "hour", "per_day": "day"}[quote.pricing_model]
        prefix += line(language, "UNIT_PREFIX", rate=quote.stated_price, unit=unit, total=quote.base_price)
    other = competitor.price if competitor is not None else None

    def hold(action: str, reason: str) -> Decision:
        if q > C:  # deterministic guardrail: never hold above the walk-away ceiling
            return done("WALK_AWAY", prefix + line(language, "WALK_AWAY", price=q), reason="above_ceiling")
        return done(action, prefix + line(language, action, price=q), binding_price=q, reason=reason)

    # 5. Vendor met (or beat) our last counter, or is already at target
    if state.last_counter is not None and q <= state.last_counter:
        return hold("ACCEPT_HOLD", "vendor_met_counter")
    if q <= T:
        return hold("ACCEPT_HOLD", "at_or_below_target")

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
    if counter >= q:
        return hold("ACCEPT_HOLD", "counter_not_needed")

    first_counter = state.rounds == 0
    state.rounds += 1
    state.last_counter = counter
    if other is not None and other < q:
        return done("COUNTER", prefix + line(language, "COUNTER_LEVERAGE", competitor=other, counter=counter),
                    counter_price=counter, leverage=asdict(competitor), reason="truthful_competitor_leverage")
    if other is not None:
        return done("COUNTER", prefix + line(language, "COUNTER_LEADER", counter=counter), counter_price=counter,
                    reason="vendor_is_current_leader")
    if first_counter:
        return done("COUNTER", prefix + line(language, "COUNTER_BUDGET", target=T, counter=counter),
                    counter_price=counter, reason="budget_frame")
    return done("COUNTER", prefix + line(language, "COUNTER_PUSH", counter=counter), counter_price=counter,
                reason="push_no_competitor")
