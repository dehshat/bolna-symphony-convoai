"""INR parsing and all-inclusive quote normalization for Indian local-service quotes.

The rule this module enforces: a vendor's *spoken* price is never trusted as the *real* price.
"₹2,200 only" with toll and driver bata extra is a ₹2,950 quote, and the buyer must see that.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Number parsing: "₹2,500", "2.5k", "pacchees sau", "do hazaar paanch sau", "eradu saavira aidu nooru"
# ---------------------------------------------------------------------------

UNITS: dict[str, float] = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "twentyfive": 25, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    # Hindi / Hinglish (romanized, common spellings)
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "paanc": 5,
    "chhe": 6, "chhah": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
    "gyarah": 11, "barah": 12, "baarah": 12, "pandrah": 15, "athara": 18, "unnees": 19,
    "bees": 20, "bis": 20, "ikkees": 21, "baees": 22, "bais": 22, "teis": 23, "chaubees": 24, "chobis": 24,
    "pacchees": 25, "pachees": 25, "pachchis": 25, "pachis": 25, "chhabbees": 26, "sattaees": 27,
    "atthaees": 28, "tees": 30, "tis": 30, "paintees": 35, "chalees": 40, "chalis": 40, "paintalis": 45,
    "pachaas": 50, "pachas": 50, "sattar": 70, "pachattar": 75, "assi": 80, "nabbe": 90,
    "dhai": 2.5, "dedh": 1.5, "dhaai": 2.5, "dhed": 1.5,
    # Kannada
    "ondu": 1, "eradu": 2, "mooru": 3, "muru": 3, "naalku": 4, "nalku": 4, "aidu": 5, "aaru": 6,
    "elu": 7, "entu": 8, "ombattu": 9, "hattu": 10, "ippattu": 20, "ippatt": 20, "mavattu": 30,
    "nalavattu": 40, "aivattu": 50, "aivatt": 50,
    # Tamil
    "onnu": 1, "ondru": 1, "rendu": 2, "irandu": 2, "moonu": 3, "naalu": 4, "anju": 5, "ainthu": 5,
    "ezhu": 7, "ettu": 8, "onbadhu": 9, "pathu": 10, "irupathu": 20, "aimbathu": 50,
    # Marathi
    "don": 2, "pach": 5, "daha": 10, "vees": 20, "pannas": 50,
}

# Words that are a complete hundreds value on their own (Kannada/Tamil/Marathi compounds)
HUNDRED_COMPOUNDS: dict[str, float] = {
    "innooru": 200, "irunooru": 200, "munnooru": 300, "nannooru": 400, "naanooru": 400,
    "ainooru": 500, "ainnooru": 500, "aidunooru": 500, "aarunooru": 600, "arunooru": 600,
    "elunooru": 700, "ezhunooru": 700, "entunooru": 800, "ennooru": 800, "ombainooru": 900,
    "pachshe": 500, "paachshe": 500, "donshe": 200, "teenshe": 300, "chaarshe": 400,
    "saheshe": 600, "satshe": 700, "aathshe": 800, "naushe": 900,
}

HUNDRED_WORDS = {"hundred", "sau", "so", "sow", "nooru", "nuru", "noor", "shambhar", "she"}
THOUSAND_WORDS = {"thousand", "hazaar", "hazar", "hajar", "hajaar", "hazzar", "saavira", "savira", "saavra",
                  "aayiram", "ayiram", "aayram", "k"}
LAKH_WORDS = {"lakh", "lac", "laakh", "lakhs"}
# Modifiers that apply to the next unit: saade teen = 3.5, sava do = 2.25, paune teen = 2.75
MODIFIERS = {"saade": 0.5, "sadhe": 0.5, "sade": 0.5, "sava": 0.25, "savva": 0.25, "paune": -0.25}

_CURRENCY_RE = re.compile(r"(₹|\brs\.?|\brupees?\b|\brupaye\b|\brupay\b|\binr\b|/-)", re.I)
_K_RE = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)
_DIGIT_RE = re.compile(r"\d+(?:\.\d+)?")


def parse_inr(value: Any) -> Optional[int]:
    """Parse a rupee amount from a number or a multilingual spoken phrase. Returns None if no amount.

    Only the first amount phrase is used ("2200 plus 400 bata" -> 2200), because the tool schema
    carries add-on charges in their own fields.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if value > 0 else None
    text = str(value).strip().lower()
    if not text or text in {"unknown", "none", "null", "na", "n/a", "-"}:
        return None
    text = _CURRENCY_RE.sub(" ", text)
    text = re.split(r"\+|\bplus\b|\bextra\b|\balag\b", text)[0]
    text = re.sub(r"(?<=\d),(?=\d)", "", text)  # 2,500 -> 2500 ; 1,20,000 -> 120000
    text = _K_RE.sub(lambda m: str(float(m.group(1)) * 1000), text)
    tokens = re.findall(r"\d+(?:\.\d+)?|[a-z_']+", text)
    # If any digits were spoken, start at the first digit so filler words ("can you do 2200") are not
    # read as numbers; multiplier words after it still apply ("2 hazaar 500", "2.5 lakh").
    first_digit = next((i for i, t in enumerate(tokens) if _DIGIT_RE.fullmatch(t)), None)
    if first_digit is not None:
        tokens = tokens[first_digit:]

    total, current, modifier, seen_number = 0.0, 0.0, 0.0, False
    last_was_digit = False
    for tok in tokens:
        if _DIGIT_RE.fullmatch(tok):
            if last_was_digit or (seen_number and current >= 100 and not total):
                break  # a second bare number starts a separate phrase ("2200 400")
            current += float(tok) + modifier
            modifier, seen_number, last_was_digit = 0.0, True, True
            continue
        last_was_digit = False
        if tok in MODIFIERS:
            modifier = MODIFIERS[tok]
        elif tok in HUNDRED_COMPOUNDS:
            current += HUNDRED_COMPOUNDS[tok]
            seen_number = True
        elif tok in UNITS and first_digit is None:
            current += UNITS[tok] + modifier
            modifier, seen_number = 0.0, True
        elif tok in HUNDRED_WORDS and (seen_number or tok in {"hundred", "nooru", "shambhar"}):
            current = (current or 1) * 100
            seen_number = True
        elif tok in THOUSAND_WORDS and (seen_number or tok != "k"):
            total += (current or 1) * 1000
            current, seen_number = 0.0, True
        elif tok in LAKH_WORDS:
            total += (current or 1) * 100000
            current, seen_number = 0.0, True
        elif tok in UNITS and seen_number and total and current == 0:
            current += UNITS[tok]  # "2 hazaar paanch sau"
        # any other word ("only", "sir", "final", "anna") is ignored
    amount = total + current
    return int(round(amount)) if amount > 0 else None


def round_to(value: float, step: int = 50) -> int:
    return int(round(value / step) * step)


def round_down(value: float, step: int = 50) -> int:
    return int(value // step * step)


def format_inr(amount: Optional[int]) -> str:
    """Indian digit grouping: 120000 -> ₹1,20,000."""
    if amount is None:
        return "—"
    s = str(int(amount))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = f"{head},{tail}"
    return f"₹{s}"


# ---------------------------------------------------------------------------
# Service request + per-category inclusion rules
# ---------------------------------------------------------------------------

CATEGORIES = {
    "cab_outstation": {
        "label": "Outstation cab",
        "required": ["toll", "driver_bata"],
        "optional": ["other_charges"],
        "labels": {"toll": "toll", "driver_bata": "driver bata", "other_charges": "parking / night charge"},
    },
    "mini_truck": {
        "label": "Mini-truck shifting",
        "required": ["other_charges"],
        "optional": ["toll"],
        "labels": {"toll": "toll", "driver_bata": "driver bata", "other_charges": "loading-unloading labour"},
    },
    "ac_repair": {
        "label": "AC repair",
        "required": ["other_charges"],
        "optional": ["driver_bata"],
        "labels": {"toll": "toll", "driver_bata": "visiting charge", "other_charges": "gas refill / parts"},
    },
}

PRICING_MODELS = {"flat", "per_km", "per_hour", "per_day"}


@dataclass
class ServiceRequest:
    category: str = "cab_outstation"
    summary: str = ""
    pickup: str = ""
    drop: str = ""
    when: str = ""
    target_price: int = 2200
    walkaway_ceiling: int = 2600
    est_km: float = 0.0          # one-way or total billable distance estimate
    days: int = 1
    est_hours: float = 4.0
    min_km_per_day: float = 250.0  # standard outstation minimum billing
    language_hint: str = "kn"

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            self.category = "cab_outstation"
        self.target_price = int(self.target_price)
        self.walkaway_ceiling = int(self.walkaway_ceiling)
        if self.walkaway_ceiling < self.target_price:
            raise ValueError("walkaway_ceiling must be >= target_price")
        self.days = max(1, int(self.days or 1))

    def estimates(self) -> dict[str, int]:
        """Conservative padding used when a vendor won't say what an add-on costs."""
        if self.category == "cab_outstation":
            return {
                "toll": round_to(max(self.est_km, 100) * 1.2),
                "driver_bata": 400 * self.days,
                "other_charges": 150,
            }
        if self.category == "mini_truck":
            return {"toll": 0 if self.est_km < 40 else round_to(self.est_km * 2), "driver_bata": 0, "other_charges": 800}
        return {"toll": 0, "driver_bata": 299, "other_charges": 1500}

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Charge items + normalization
# ---------------------------------------------------------------------------

_NOTHING_EXTRA_RE = re.compile(r"(no|nothing|koi|kuch|yenu|enu)\s+(extra|alag)(\s+(nahi|nahin|illa))?", re.I)
_INCLUDED_RE = re.compile(r"\b(incl\w*|shamil|sahit|serisi|sersi|yes|haan|han|all[- ]?in\w*|sab milake|"
                          r"include)\b", re.I)
_EXTRA_RE = re.compile(r"(not\s+incl\w*|exclud\w*|\bextra\b|\balag\b|\bseparate\w*\b|\bno\b|\bnahi\b|"
                       r"\bnahin\b|\bbere\b|\billa\b|\bactuals?\b)", re.I)
_NA_RE = re.compile(r"^(na|n/a|not applicable|none|nil|zero|0|free|no charge)$", re.I)


@dataclass
class ChargeItem:
    status: str = "unknown"  # included | extra | na | unknown
    amount: Optional[int] = None

    @property
    def resolved(self) -> bool:
        return self.status in {"included", "na"} or (self.status == "extra" and self.amount is not None)


def parse_charge(value: Any) -> ChargeItem:
    """'included' / 'extra 400' / '350' / 'toll alag' / 'unknown' -> ChargeItem."""
    if value is None:
        return ChargeItem()
    if isinstance(value, ChargeItem):
        return value
    if isinstance(value, dict):
        return ChargeItem(status=str(value.get("status", "unknown")), amount=value.get("amount"))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ChargeItem("extra", int(value)) if value > 0 else ChargeItem("na")
    text = str(value).strip()
    if not text or text.lower() in {"unknown", "not stated", "not mentioned", "?", "null", "none given"}:
        return ChargeItem()
    if _NA_RE.match(text):
        return ChargeItem("na")
    amount = parse_inr(re.sub(r"\b(extra|alag|separate|plus)\b", " ", text, flags=re.I))
    if _NOTHING_EXTRA_RE.search(text):
        return ChargeItem("included")
    if _EXTRA_RE.search(text):
        return ChargeItem("extra", amount)
    if _INCLUDED_RE.search(text):
        return ChargeItem("included")
    if amount:
        return ChargeItem("extra", amount)
    return ChargeItem()


def detect_pricing_model(value: Any, said: str = "") -> str:
    text = f"{value or ''} {said or ''}".lower()
    v = str(value or "").strip().lower()
    if v in PRICING_MODELS:
        return v
    if re.search(r"(per\s*km|/\s*km|km\s*ka|kilomet|ondu km|km ge|kilo meter)", text):
        return "per_km"
    if re.search(r"(per\s*hour|/\s*hr|ghante|ghanta|hour ka|hourly)", text):
        return "per_hour"
    if re.search(r"(per\s*day|din\s*ka|/\s*day|daily|dina ke)", text):
        return "per_day"
    return "flat"


@dataclass
class NormalizedQuote:
    status: str                      # VALID | NEEDS_INCLUSIONS | CLARIFY
    pricing_model: str
    stated_price: Optional[int]      # the number the vendor actually said
    base_price: Optional[int]        # stated price expanded to a trip total (per-km x km etc.)
    all_in: Optional[int]            # what the buyer will really pay
    components: list = field(default_factory=list)
    unknown_items: list = field(default_factory=list)
    padded_items: list = field(default_factory=list)
    charges: dict = field(default_factory=dict)
    clarify_reason: str = ""

    @property
    def hidden_charges(self) -> int:
        if self.all_in is None or self.base_price is None:
            return 0
        return self.all_in - self.base_price

    def to_dict(self) -> dict:
        d = asdict(self)
        d["hidden_charges"] = self.hidden_charges
        return d


def normalize_quote(req: ServiceRequest, price: Any, pricing_model: Any = "flat",
                    charges: Optional[dict] = None, said: str = "") -> NormalizedQuote:
    """Turn a raw spoken quote into an all-inclusive trip price with explicit components."""
    rules = CATEGORIES[req.category]
    model = detect_pricing_model(pricing_model, said)
    stated = parse_inr(price)
    charges = {k: parse_charge(v) for k, v in (charges or {}).items()}
    items = rules["required"] + rules["optional"]
    for key in items:
        charges.setdefault(key, ChargeItem())

    if stated is None:
        return NormalizedQuote("CLARIFY", model, None, None, None, charges={k: asdict(v) for k, v in charges.items()},
                               clarify_reason="no_price")

    # Expand unit pricing into a trip total
    if model == "per_km":
        billable_km = max(req.est_km or 0, req.min_km_per_day * req.days)
        base = stated * billable_km
        base_label = f"{format_inr(stated)}/km × {int(billable_km)} km"
    elif model == "per_hour":
        base = stated * req.est_hours
        base_label = f"{format_inr(stated)}/hr × {req.est_hours:g} hr"
    elif model == "per_day":
        base = stated * req.days
        base_label = f"{format_inr(stated)}/day × {req.days} day"
    else:
        base = stated
        base_label = "quoted fare"
    base = int(round(base))

    # Plausibility: catch ASR / unit confusion before it reaches the negotiation math
    reason = ""
    if model == "per_km" and stated > 200:
        reason = "per_km_rate_too_high"
    elif model == "flat" and stated < 0.3 * req.target_price:
        reason = "price_too_low_maybe_unit_rate"
    elif base > 5 * req.walkaway_ceiling:
        reason = "price_too_high_maybe_misheard"
    if reason:
        return NormalizedQuote("CLARIFY", model, stated, base, None, charges={k: asdict(v) for k, v in charges.items()},
                               clarify_reason=reason)

    estimates = req.estimates()
    components = [{"label": base_label, "amount": base, "source": "stated"}]
    unknown, padded = [], []
    all_in = base
    for key in items:
        item = charges[key]
        label = rules["labels"][key]
        if item.status in {"included", "na"}:
            continue
        if item.status == "extra" and item.amount is not None:
            amount = item.amount * (req.days if key == "driver_bata" and req.category == "cab_outstation"
                                    and item.amount <= 1000 else 1)
            components.append({"label": f"{label} (extra)", "amount": amount, "source": "vendor"})
            all_in += amount
            continue
        # unknown, or "extra" with no amount: pad conservatively
        estimate = estimates.get(key, 0)
        if key in rules["required"] or item.status == "extra":
            unknown.append(key)
            padded.append(key)
            if estimate:
                components.append({"label": f"{label} (est.)", "amount": estimate, "source": "estimate"})
                all_in += estimate

    status = "NEEDS_INCLUSIONS" if unknown else "VALID"
    return NormalizedQuote(status, model, stated, base, int(all_in), components, unknown, padded,
                           {k: asdict(v) for k, v in charges.items()})


def merge_charges(previous: dict, new: dict) -> dict:
    """New tool call fields override earlier ones, except that 'unknown' never erases known info."""
    merged = {k: parse_charge(v) for k, v in (previous or {}).items()}
    for key, value in (new or {}).items():
        item = parse_charge(value)
        if item.status != "unknown" or key not in merged:
            merged[key] = item
    return merged
