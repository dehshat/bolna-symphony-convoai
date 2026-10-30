"""The 11 call languages: codes, scripts, where each is spoken, and checks that a line contains no English.

Every language here is supported end to end by Sarvam (saaras ASR + bulbul TTS) through Bolna, by the browser's
speech APIs for talking to the user, and by `app.lexicon` for every line the negotiator speaks.

Location → language: the call language defaults to the language of the place where the service is needed (a cook in
Pune is called in Marathi, a plumber in Chennai in Tamil). The user can always override it.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class Language:
    code: str          # our code; also the plain code Bolna's Sarvam transcriber takes
    name: str          # English name
    native: str        # the language's own name for itself
    sarvam: str        # Sarvam locale for the synthesizer
    browser: str       # BCP-47 tag for the browser's speech recognition / synthesis
    script: str        # script name, for prompts
    block: tuple       # Unicode block (first, last) of the script; None for Latin

    def public(self) -> dict:
        d = asdict(self)
        d.pop("block")
        return d


LANGUAGES: dict[str, Language] = {l.code: l for l in (
    Language("en", "English", "English", "en-IN", "en-IN", "Latin", ()),
    Language("hi", "Hindi", "हिन्दी", "hi-IN", "hi-IN", "Devanagari", (0x0900, 0x097F)),
    Language("kn", "Kannada", "ಕನ್ನಡ", "kn-IN", "kn-IN", "Kannada", (0x0C80, 0x0CFF)),
    Language("ta", "Tamil", "தமிழ்", "ta-IN", "ta-IN", "Tamil", (0x0B80, 0x0BFF)),
    Language("te", "Telugu", "తెలుగు", "te-IN", "te-IN", "Telugu", (0x0C00, 0x0C7F)),
    Language("mr", "Marathi", "मराठी", "mr-IN", "mr-IN", "Devanagari", (0x0900, 0x097F)),
    Language("bn", "Bengali", "বাংলা", "bn-IN", "bn-IN", "Bengali", (0x0980, 0x09FF)),
    Language("gu", "Gujarati", "ગુજરાતી", "gu-IN", "gu-IN", "Gujarati", (0x0A80, 0x0AFF)),
    Language("ml", "Malayalam", "മലയാളം", "ml-IN", "ml-IN", "Malayalam", (0x0D00, 0x0D7F)),
    Language("pa", "Punjabi", "ਪੰਜਾਬੀ", "pa-IN", "pa-IN", "Gurmukhi", (0x0A00, 0x0A7F)),
    Language("od", "Odia", "ଓଡ଼ିଆ", "od-IN", "or-IN", "Odia", (0x0B00, 0x0B7F)),
)}
CODES = tuple(LANGUAGES)
DEFAULT = "hi"   # widest-understood fallback when the region is unknown


def get(code: Optional[str]) -> Language:
    return LANGUAGES.get(normalize(code) or "", LANGUAGES[DEFAULT])


def normalize(code: Optional[str]) -> Optional[str]:
    """'mr-IN' / 'MR' / 'or' (browser Odia) / 'marathi' -> 'mr'; None if unknown."""
    if not code:
        return None
    c = str(code).strip().lower().replace("_", "-")
    c = {"or": "od", "or-in": "od", "ori": "od"}.get(c, c)
    if c in LANGUAGES:
        return c
    if c.split("-")[0] in LANGUAGES:
        return c.split("-")[0]
    return NAME_TO_CODE.get(c)


def name(code: str) -> str:
    return get(code).name


# How a Hindi sentence names each language (for the assistant talking to a Hindi-speaking user)
HINDI_NAMES = {"en": "अंग्रेज़ी", "hi": "हिंदी", "kn": "कन्नड़", "ta": "तमिल", "te": "तेलुगु", "mr": "मराठी",
               "bn": "बांग्ला", "gu": "गुजराती", "ml": "मलयालम", "pa": "पंजाबी", "od": "ओड़िया"}


def public_list() -> list[dict]:
    return [{**l.public(), "hi_name": HINDI_NAMES[l.code]} for l in LANGUAGES.values()]


# ---------------------------------------------------------------------------------------------- where it's spoken

STATE_LANG = {
    "karnataka": "kn", "maharashtra": "mr", "goa": "mr", "tamil nadu": "ta", "puducherry": "ta", "pondicherry": "ta",
    "andhra pradesh": "te", "telangana": "te", "west bengal": "bn", "tripura": "bn", "gujarat": "gu",
    "daman": "gu", "diu": "gu", "dadra": "gu", "kerala": "ml", "lakshadweep": "ml", "punjab": "pa",
    "chandigarh": "pa", "odisha": "od", "orissa": "od", "delhi": "hi", "national capital territory": "hi",
    "uttar pradesh": "hi", "bihar": "hi", "madhya pradesh": "hi", "rajasthan": "hi", "haryana": "hi",
    "uttarakhand": "hi", "himachal pradesh": "hi", "jharkhand": "hi", "chhattisgarh": "hi",
    "jammu": "hi", "kashmir": "hi", "ladakh": "hi", "assam": "hi", "sikkim": "hi", "meghalaya": "hi",
    "manipur": "hi", "mizoram": "hi", "nagaland": "hi", "arunachal pradesh": "hi",
}
_CITIES = {
    "kn": "bengaluru bangalore mysuru mysore mangaluru mangalore hubli hubballi dharwad belagavi belgaum udupi coorg "
          "kodagu madikeri davangere shivamogga shimoga tumakuru tumkur ballari bellary hosapete kalaburagi gulbarga "
          "hassan chikkamagaluru whitefield koramangala indiranagar jayanagar electronic city yelahanka hebbal "
          "marathahalli bellandur hsr layout btm layout jp nagar banashankari malleshwaram rajajinagar kengeri",
    "mr": "mumbai bombay pune nagpur nashik thane navi mumbai aurangabad sambhajinagar kolhapur solapur amravati "
          "pimpri chinchwad kalyan dombivli vasai virar panvel satara sangli latur akola jalgaon ahmednagar nanded "
          "andheri bandra borivali dadar powai kothrud hinjewadi baner wakad hadapsar viman nagar",
    "ta": "chennai madras coimbatore madurai trichy tiruchirappalli salem tirunelveli vellore erode hosur tiruppur "
          "thanjavur kanchipuram ooty nagercoil puducherry pondicherry tambaram velachery adyar anna nagar t nagar",
    "te": "hyderabad secunderabad warangal vijayawada visakhapatnam vizag guntur tirupati nellore kakinada "
          "rajahmundry rajamahendravaram karimnagar khammam kurnool anantapur gachibowli hitech city madhapur "
          "kondapur kukatpally banjara hills jubilee hills",
    "bn": "kolkata calcutta howrah durgapur asansol siliguri kharagpur bardhaman burdwan agartala salt lake "
          "new town rajarhat",
    "gu": "ahmedabad surat vadodara baroda rajkot gandhinagar bhavnagar jamnagar anand junagadh navsari vapi "
          "bharuch morbi",
    "ml": "kochi cochin ernakulam thiruvananthapuram trivandrum kozhikode calicut thrissur kollam kannur "
          "alappuzha alleppey kottayam palakkad malappuram munnar",
    "pa": "amritsar ludhiana jalandhar patiala mohali bathinda chandigarh pathankot hoshiarpur moga",
    "od": "bhubaneswar cuttack rourkela puri sambalpur berhampur brahmapur balasore baripada",
    "hi": "delhi new delhi noida greater noida gurgaon gurugram ghaziabad faridabad lucknow kanpur jaipur patna "
          "bhopal indore varanasi agra allahabad prayagraj ranchi dehradun shimla raipur jodhpur udaipur gwalior "
          "meerut jabalpur kota ajmer bikaner aligarh bareilly gorakhpur dhanbad jamshedpur guwahati",
}
CITY_LANG: dict[str, str] = {}
# Multi-word places first so "navi mumbai" / "jp nagar" match as phrases
_PHRASES = ("navi mumbai", "electronic city", "hsr layout", "btm layout", "jp nagar", "viman nagar", "salt lake",
            "new town", "anna nagar", "t nagar", "hitech city", "banjara hills", "jubilee hills", "new delhi",
            "greater noida", "tamil nadu")
for _lang, _blob in _CITIES.items():
    rest = _blob
    for phrase in _PHRASES:
        if phrase in rest:
            CITY_LANG[phrase] = _lang
            rest = rest.replace(phrase, " ")
    for word in rest.split():
        CITY_LANG.setdefault(word, _lang)

_PLACE_RE = re.compile(r"\b(" + "|".join(sorted(map(re.escape, {**STATE_LANG, **CITY_LANG}), key=len, reverse=True))
                       + r")\b", re.I)


def detect(location: str = "", state: str = "") -> tuple[Optional[str], str]:
    """(language code, reason) for a place. The state (from GPS reverse geocoding) wins over the text.

    Returns (None, "") when the place isn't recognised, so callers can keep an earlier choice."""
    for text in (state, location):
        matches = list(_PLACE_RE.finditer((text or "").lower()))
        if not matches:
            continue
        # A state beats a city; among cities the last one is usually the widest ("Koramangala, Bengaluru")
        m = next((x for x in matches if x.group(1) in STATE_LANG), matches[-1])
        key = m.group(1)
        code = STATE_LANG.get(key) or CITY_LANG.get(key)
        place = text[m.start():m.end()]
        place = place if any(c.isupper() for c in place) else place.title()
        return code, f"{LANGUAGES[code].name} is spoken in {place}"
    return None, ""


# ---------------------------------------------------------------------------------------------- names, yes / no

NAME_TO_CODE = {
    "english": "en", "angrezi": "en", "अंग्रेज़ी": "en", "अंग्रेजी": "en", "इंग्लिश": "en",
    "hindi": "hi", "हिंदी": "hi", "हिन्दी": "hi",
    "kannada": "kn", "ಕನ್ನಡ": "kn", "कन्नड़": "kn",
    "tamil": "ta", "தமிழ்": "ta", "तमिल": "ta",
    "telugu": "te", "తెలుగు": "te", "तेलुगु": "te",
    "marathi": "mr", "मराठी": "mr",
    "bengali": "bn", "bangla": "bn", "বাংলা": "bn", "बंगाली": "bn", "बांग्ला": "bn",
    "gujarati": "gu", "ગુજરાતી": "gu", "गुजराती": "gu",
    "malayalam": "ml", "മലയാളം": "ml", "मलयालम": "ml",
    "punjabi": "pa", "ਪੰਜਾਬੀ": "pa", "पंजाबी": "pa",
    "odia": "od", "oriya": "od", "ଓଡ଼ିଆ": "od", "ଓଡିଆ": "od", "ओड़िया": "od", "उड़िया": "od",
}
_LANG_NAME_RE = re.compile("|".join(sorted(map(re.escape, NAME_TO_CODE), key=len, reverse=True)), re.I)
_LANG_CUE_RE = re.compile(r"(language|bhasha|bhasa|speak|talk|call|bolo|baat|mein|me\b|in\b|madhe|la\b|bhashe|"
                          r"भाषा|में|बात|बोल|मध्ये|ಭಾಷೆ|ಲ್ಲಿ|மொழி|ல\b|భాష|లో|ভাষা|ভাষায়|ભાષા|માં|ഭാഷ|യിൽ|ਭਾਸ਼ਾ|ਵਿੱਚ|ଭାଷା|ରେ)",
                          re.I)


def requested_language(text: str) -> Optional[str]:
    """'Call them in Marathi' / 'मराठी में बात करो' / just 'Tamil' -> the language code; None otherwise."""
    found = {NAME_TO_CODE.get(m.group(0).lower()) or NAME_TO_CODE.get(m.group(0))
             for m in _LANG_NAME_RE.finditer(text or "")} - {None}
    if len(found) != 1:
        return None
    words = len((text or "").split())
    return found.pop() if words <= 3 or _LANG_CUE_RE.search(text) else None


_YES = ("yes", "yeah", "yep", "yup", "ok", "okay", "sure", "go ahead", "go on", "do it", "proceed", "confirm",
        "confirmed", "no problem", "book it", "book", "start", "call them", "please do", "right", "correct", "fine", "perfect",
        "haan", "han", "ha", "haa", "ji", "haanji", "theek hai", "thik hai", "chalo", "kar do", "karo", "ho", "hoy",
        "chalel", "houdu", "sari", "aama", "aamam", "seri", "avunu", "sare", "athe", "shari", "hyan", "thik ache",
        "हाँ", "हां", "हा", "जी", "जी हाँ", "ठीक है", "चलो", "कर दो", "करो", "हो", "होय", "चालेल", "करा", "बरोबर",
        "હા", "ચાલો", "બરાબર", "હા જી", "হ্যাঁ", "হ্যা", "ঠিক আছে", "করুন", "ਹਾਂ", "ਹਾਂ ਜੀ", "ਠੀਕ ਹੈ", "ਕਰੋ",
        "ହଁ", "ଠିକ ଅଛି", "ହଉ", "ಹೌದು", "ಸರಿ", "ಮಾಡಿ", "ಆಯ್ತು", "ஆமா", "ஆமாம்", "சரி", "பண்ணுங்க", "అవును",
        "సరే", "చేయండి", "అలాగే", "അതെ", "ശരി", "ഓക്കെ", "ചെയ്യൂ")
_NO = ("no", "nope", "nah", "not now", "don't", "dont", "cancel", "stop", "wait", "hold on", "change", "not yet",
       "nahi", "nahin", "mat", "ruko", "nako", "beda", "illa", "venda", "vendam", "vaddu", "ledu", "venam",
       "नहीं", "नही", "मत", "रुको", "नको", "थांबा", "ના", "না", "ਨਹੀਂ", "ନା", "ନାହିଁ", "ಬೇಡ", "ಇಲ್ಲ", "வேண்டாம்", "இல்ல",
       "వద్దు", "లేదు", "വേണ്ട", "ഇല്ല")


_PUNCT_RE = re.compile(r"[.,!?;:\"'()\[\]–—।॥]")


def _has(text: str, words) -> bool:
    # Only punctuation is stripped: Indic vowel signs are not \w, so a \W-based clean-up would break words apart
    t = " " + re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", text.lower())) + " "
    return any(f" {w} " in t for w in words)


def is_no(text: str) -> bool:
    t = (text or "").replace("no problem", "").replace("No problem", "")
    return _has(t, _NO)


def is_yes(text: str) -> bool:
    """A short 'yes / haan / ಹೌದು / चालेल'. Long sentences are not treated as a bare yes."""
    if not text or is_no(text) or len(text.split()) > 6:
        return False
    return _has(text, _YES)


def voice_commands() -> dict:
    """Shared with the browser so voice yes/no work the same on both sides."""
    return {"yes": list(_YES), "no": list(_NO)}


# ---------------------------------------------------------------------------------------------- purity checks

_LATIN_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'’.-]*")


def latin_words(text: str, allow: tuple = ()) -> list[str]:
    """English (Latin-script) words in a line, ignoring the allowed proper nouns (vendor and place names)."""
    t = text or ""
    for phrase in sorted((a for a in allow if a), key=len, reverse=True):
        t = re.sub(re.escape(phrase), " ", t, flags=re.I)
    return _LATIN_WORD_RE.findall(t)


def foreign_letters(text: str, code: str) -> list[str]:
    """Letters that are neither Latin nor the language's own script (e.g. Gujarati inside a Marathi line)."""
    lang = get(code)
    if not lang.block:
        return []
    lo, hi = lang.block
    out = []
    for ch in text or "":
        if ch.isascii() or ch in "‌‍₹":
            continue
        if unicodedata.category(ch)[0] in "LM" and not lo <= ord(ch) <= hi:
            out.append(ch)
    return out
