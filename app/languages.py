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
_ORDER = ("en", "hi", "kn", "ta", "te", "mr", "bn", "gu", "ml", "pa", "od")
# What each language calls the 11 languages (for "Shall I call them in Marathi?" said in Tamil)
LANGUAGE_NAMES: dict[str, dict[str, str]] = {lang: dict(zip(_ORDER, row.split())) for lang, row in {
    "en": "English Hindi Kannada Tamil Telugu Marathi Bengali Gujarati Malayalam Punjabi Odia",
    "hi": "अंग्रेज़ी हिंदी कन्नड़ तमिल तेलुगु मराठी बांग्ला गुजराती मलयालम पंजाबी ओड़िया",
    "mr": "इंग्रजी हिंदी कन्नड तमिळ तेलुगू मराठी बंगाली गुजराती मल्याळम पंजाबी ओडिया",
    "gu": "અંગ્રેજી હિન્દી કન્નડ તમિલ તેલુગુ મરાઠી બંગાળી ગુજરાતી મલયાલમ પંજાબી ઓડિયા",
    "bn": "ইংরেজি হিন্দি কন্নড় তামিল তেলুগু মারাঠি বাংলা গুজরাটি মালয়ালম পাঞ্জাবি ওড়িয়া",
    "pa": "ਅੰਗਰੇਜ਼ੀ ਹਿੰਦੀ ਕੰਨੜ ਤਮਿਲ ਤੇਲਗੂ ਮਰਾਠੀ ਬੰਗਾਲੀ ਗੁਜਰਾਤੀ ਮਲਿਆਲਮ ਪੰਜਾਬੀ ਉੜੀਆ",
    "od": "ଇଂରାଜୀ ହିନ୍ଦୀ କନ୍ନଡ଼ ତାମିଲ ତେଲୁଗୁ ମରାଠୀ ବଙ୍ଗଳା ଗୁଜୁରାଟୀ ମାଲାୟାଲମ ପଞ୍ଜାବୀ ଓଡ଼ିଆ",
    "kn": "ಇಂಗ್ಲಿಷ್ ಹಿಂದಿ ಕನ್ನಡ ತಮಿಳು ತೆಲುಗು ಮರಾಠಿ ಬಂಗಾಳಿ ಗುಜರಾತಿ ಮಲಯಾಳಂ ಪಂಜಾಬಿ ಒಡಿಯಾ",
    "ta": "ஆங்கிலம் இந்தி கன்னடம் தமிழ் தெலுங்கு மராத்தி வங்காளம் குஜராத்தி மலையாளம் பஞ்சாபி ஒடியா",
    "te": "ఇంగ్లీష్ హిందీ కన్నడ తమిళం తెలుగు మరాఠీ బెంగాలీ గుజరాతీ మలయాళం పంజాబీ ఒడియా",
    "ml": "ഇംഗ്ലീഷ് ഹിന്ദി കന്നഡ തമിഴ് തെലുങ്ക് മറാഠി ബംഗാളി ഗുജറാത്തി മലയാളം പഞ്ചാബി ഒഡിയ",
}.items()}

# Every language's name for every language counts when the user asks for a call language ("மராத்தியில் அழையுங்கள்")
for _names in LANGUAGE_NAMES.values():
    for _code, _name in _names.items():
        NAME_TO_CODE.setdefault(_name.lower(), _code)
_LANG_NAME_RE = re.compile("|".join(sorted(map(re.escape, NAME_TO_CODE), key=len, reverse=True)), re.I)
_LANG_CUE_RE = re.compile(r"(language|bhasha|bhasa|speak|talk|call|bolo|baat|mein|me\b|in\b|madhe|la\b|bhashe|"
                          r"भाष|ভাষ|ભાષ|ಭಾಷ|భాష|ഭാഷ|ਭਾਸ਼|ଭାଷ|மொழி|फोन|फ़ोन|ফোন|ફોન|ਫ਼ੋਨ|ଫୋନ|ಫೋನ್|ఫోన్|ഫോൺ|அழை|"
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


# "What? / Say that again / I didn't understand": the whole message, so a real answer is never mistaken for one
_REPEAT = ("what", "what did you say", "sorry", "pardon", "come again", "repeat", "repeat that", "repeat please",
           "say again", "say that again", "again please", "i didn't understand", "didn't get it", "i didn't get that",
           "kya", "kya bola", "kya kaha", "phir se", "phir se bolo", "dobara", "dobara bolo", "samjha nahi",
           "क्या", "क्या बोला", "क्या कहा", "फिर से", "फिर से बोलो", "फिर से बोलिए", "दोबारा", "दोबारा बोलो",
           "समझा नहीं", "समझ नहीं आया", "काय", "काय म्हणालात", "पुन्हा", "पुन्हा सांगा", "समजलं नाही", "कळलं नाही",
           "શું", "ફરીથી", "ફરી બોલો", "સમજાયું નહીં", "কী", "কি", "আবার", "আবার বলুন", "বুঝিনি", "বুঝতে পারিনি",
           "ਕੀ", "ਫਿਰ ਤੋਂ", "ਦੁਬਾਰਾ", "ਦੁਬਾਰਾ ਦੱਸੋ", "ਸਮਝ ਨਹੀਂ ਆਇਆ", "କଣ", "କ ଣ", "ପୁଣି", "ପୁଣି କୁହନ୍ତୁ",
           "ବୁଝିପାରିଲି ନାହିଁ", "ಏನು", "ಏನಂದ್ರಿ", "ಮತ್ತೆ", "ಮತ್ತೆ ಹೇಳಿ", "ಅರ್ಥ ಆಗಲಿಲ್ಲ", "என்ன", "மீண்டும்",
           "மறுபடியும்", "மறுபடியும் சொல்லுங்கள்", "புரியவில்லை", "ఏమిటి", "ఏంటి", "మళ్ళీ", "మళ్ళీ చెప్పండి",
           "అర్థం కాలేదు", "എന്ത്", "എന്താ", "വീണ്ടും", "വീണ്ടും പറയൂ", "മനസ്സിലായില്ല")
_REPEAT_SET = set(_REPEAT)


def is_repeat(text: str) -> bool:
    """The user didn't catch the last line and wants it again ('what?', 'फिर से', 'மறுபடியும்')."""
    t = re.sub(r"\s+", " ", _PUNCT_RE.sub(" ", (text or "").lower())).strip()
    return t in _REPEAT_SET or re.sub(r"\s+(please|ji|plz)$", "", t) in _REPEAT_SET


def voice_commands() -> dict:
    """Shared with the browser so voice yes/no/repeat work the same on both sides."""
    return {"yes": list(_YES), "no": list(_NO), "repeat": list(_REPEAT)}


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


# ---------------------------------------------------------------------------------------------- which language the user speaks

_MARATHI_WORDS = ("आहे", "आहेत", "मला", "आम्हाला", "तुम्हाला", "पाहिजे", "हवा", "हवी", "हवं", "हवे", "उद्या", "आणि", "नाही",
                  "काय", "कुठे", "करा", "मध्ये", "साठी", "होय", "चालेल", "बरोबर", "जवळ", "आज", "माझं", "माझा", "माझी",
                  "किती", "झालं", "द्या", "सांगा", "घरी", "इथे", "तिथे", "लवकर", "नको")
_HINDI_WORDS = ("है", "हैं", "मुझे", "हमें", "चाहिए", "कल", "और", "नहीं", "क्या", "में", "के", "की", "का", "लिए", "हूँ",
                "हूं", "मेरा", "मेरी", "कितना", "करो", "कीजिए", "दीजिए", "बताइए", "यहाँ", "वहाँ", "जल्दी", "पास")
_HINGLISH_RE = re.compile(r"\b(mujhe|muje|hume|humein|chahiye|chahie|chaiye|hai|hain|kal|kya|nahi|nahin|karo|karna|"
                          r"karwana|mera|meri|mere|ghar|mein|aaj|liye|bhai|bhaiya|wala|wali|kitna|kitne|paisa|paise|"
                          r"jaldi|abhi|bolo|batao|chalega|theek|thik|accha|acha|haan)\b", re.I)
_ENGLISH_WORDS = {"i", "i'm", "we", "need", "want", "a", "an", "the", "to", "in", "for", "my", "is", "it", "me", "please",
                  "can", "you", "and", "of", "at", "from", "this", "that", "tomorrow", "today", "morning", "evening",
                  "near", "some", "someone", "looking", "book", "get", "find", "would", "like", "with", "around", "max",
                  "budget", "what", "how", "when", "where", "not", "no", "yes", "there", "are", "do", "does", "should"}
_BLOCKS = {code: lang.block for code, lang in LANGUAGES.items() if lang.block and code != "mr"}


def script_language(text: str, current: str = "hi") -> Optional[str]:
    """The language a line is written in, from its script: Tamil letters -> 'ta'; Devanagari -> 'hi' or 'mr' by its
    words (a bare "हो" keeps `current`). None for Latin text or too few letters to tell."""
    counts: dict[str, int] = {}
    for ch in text or "":
        cp = ord(ch)
        for code, (lo, hi) in _BLOCKS.items():
            if lo <= cp <= hi:
                counts[code] = counts.get(code, 0) + 1
                break
    if not counts:
        return None
    code, n = max(counts.items(), key=lambda kv: kv[1])
    if n < 2:
        return None
    if code == "hi":
        words = set(_PUNCT_RE.sub(" ", text).split())
        marathi, hindi = len(words & set(_MARATHI_WORDS)), len(words & set(_HINDI_WORDS))
        if marathi != hindi:
            return "mr" if marathi > hindi else "hi"
        return current if current in {"hi", "mr"} else "hi"
    return code


def confident_language(text: str, heard: Optional[str] = None, current: str = "hi") -> Optional[str]:
    """Which of the 11 languages a message is in, or None when it can't be told.

    The script decides when it can (Tamil, Bengali…; Devanagari is Hindi or Marathi by its words). Latin text is
    English, or Hindi when it is clearly romanized Hindi ("mujhe kal plumber chahiye"); `heard` is what speech
    recognition detected. Short Latin replies ("ok", "2000") and place names ("Koramangala, Bengaluru") are Latin in
    every language, so they tell nothing."""
    by_script = script_language(text, current)
    if by_script:
        return by_script
    words = re.findall(r"[A-Za-z]+", text or "")
    heard = normalize(heard)
    # Speech recognition writes Indian languages in their own script, so Latin text it labelled "Kannada" is mostly
    # names and English words: trust the label only for a real sentence
    if heard and heard != "en" and len(words) >= 4:
        return heard
    if len(_HINGLISH_RE.findall(text or "")) >= 2:
        return "hi"
    english = sum(w.lower() in _ENGLISH_WORDS for w in words)
    if (english and len(words) >= 3) or len(words) >= 6:
        return "en"
    return None


def user_language(text: str, heard: Optional[str] = None, current: str = "en") -> str:
    """The language of a message, or `current` when the message doesn't show it."""
    found = confident_language(text, heard, current)
    return found or (current if current in LANGUAGES else "en")


def mentions_language(text: str) -> bool:
    """Does the message name a language at all ('in Hindi', 'தமிழ்ல', 'मराठी')?"""
    return bool(_LANG_NAME_RE.search(text or ""))


def writes_in(text: str, code: str) -> bool:
    """Is a reply written in this language (so it can be spoken to a user who speaks it)?"""
    if code == "en":
        return script_language(text) is None
    found = script_language(text)
    return found == code or (found in {"hi", "mr"} and code in {"hi", "mr"})
