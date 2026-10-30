"""11 call languages: complete, purely native (not one English word on a non-English call), picked from the place."""

import asyncio
import re
import time

import pytest

from app import bolna_client, briefs, languages, lexicon, scenarios
from app.auction_engine import AuctionEngine, Store
from app.pricing import ServiceRequest
from app.simulator import SimulatedLeg

NATIVE = lexicon.NATIVE
PLACEHOLDER = re.compile(r"\{[a-z_]+\}")


def clean(text):
    return PLACEHOLDER.sub("", text)


def test_eleven_languages_including_marathi():
    assert len(languages.CODES) == 11 and "mr" in languages.CODES
    assert set(languages.CODES) == set(lexicon.ALL)
    assert {l.sarvam for l in languages.LANGUAGES.values()} == {
        "en-IN", "hi-IN", "kn-IN", "ta-IN", "te-IN", "mr-IN", "bn-IN", "gu-IN", "ml-IN", "pa-IN", "od-IN"}
    assert languages.normalize("or-IN") == "od" and languages.normalize("Marathi") == "mr"


@pytest.mark.parametrize("table", ["TEMPLATES", "VENDOR", "BOOKING", "OPENINGS", "WORDS", "UNITS"])
def test_every_language_has_every_line(table):
    data = getattr(lexicon, table)
    for lang in lexicon.ALL:
        assert set(data[lang]) == set(data["en"]), (table, lang, set(data["en"]) ^ set(data[lang]))
        for key, text in data[lang].items():
            assert set(PLACEHOLDER.findall(text)) <= set(PLACEHOLDER.findall(data["en"][key])) | {"{who}"}, (lang, key)


def test_service_time_and_charge_words_cover_every_language():
    from app import services
    assert set(lexicon.SERVICE_NAMES) >= {s.key for s in services.SERVICES} | {"other"}
    for table in (lexicon.SERVICE_NAMES, lexicon.CHARGE_WORDS, lexicon.WHEN_WORDS):
        for key, row in table.items():
            assert set(row) == set(NATIVE), key
    assert all(len(v) == 12 for v in lexicon.MONTHS.values())


@pytest.mark.parametrize("lang", NATIVE)
def test_no_english_and_no_wrong_script_anywhere_in_the_lexicon(lang):
    blobs = [*lexicon.TEMPLATES[lang].values(), *lexicon.VENDOR[lang].values(), *lexicon.BOOKING[lang].values(),
             *lexicon.OPENINGS[lang].values(), *lexicon.WORDS[lang].values(), *lexicon.UNITS[lang].values(),
             *(row[lang] for row in lexicon.SERVICE_NAMES.values()),
             *(row[lang] for row in lexicon.CHARGE_WORDS.values()),
             *(row[lang] for row in lexicon.WHEN_WORDS.values()), *lexicon.MONTHS[lang]]
    for text in blobs:
        assert not languages.latin_words(clean(text)), (lang, text)
        assert not languages.foreign_letters(clean(text), lang), (lang, text)


@pytest.mark.parametrize("lang", NATIVE)
def test_rendered_lines_are_pure(lang):
    sample = dict(competitor=2450, counter=2300, target=2200, price=2500, quoted=780, previous=2400, rate=14,
                  total=2030, fee=300, items=lexicon.charge_words("toll / driver bata", lang),
                  unit=lexicon.unit(lang, "per_km"), when=lexicon.localize_when("tomorrow 11 am", lang),
                  slot=lexicon.localize_when("today 5 PM", lang), fee_part="", who="operator")
    for key in lexicon.TEMPLATES["en"]:
        line = lexicon.render(lang, key, **sample)
        assert "{" not in line and not languages.latin_words(line), (lang, key, line)


@pytest.mark.parametrize("when, lang, expected", [
    ("Tomorrow 6:00 AM", "hi", "कल सुबह 6 बजे"),
    ("Tomorrow 6:00 AM", "kn", "ನಾಳೆ ಬೆಳಿಗ್ಗೆ 6 ಗಂಟೆಗೆ"),
    ("Tomorrow 6:00 AM", "bn", "কাল সকালে 6টায়"),          # 'কাল' (tomorrow) is inside 'সকালে': never dropped
    ("today evening", "mr", "आज संध्याकाळी"),
    ("from next monday", "mr", "पुढच्या सोमवारपासून"),
    ("from next monday", "gu", "આવતા સોમવારથી"),
    ("from monday", "kn", "ಸೋಮವಾರದಿಂದ"),
    ("15th November 4 pm", "hi", "15 नवंबर शाम 4 बजे"),
    ("after lunch", "ta", "சீக்கிரம்"),                     # untranslatable -> "soon", never English
    ("कल शाम 5 बजे", "hi", "कल शाम 5 बजे"),                  # already native: unchanged
])
def test_times_are_said_in_the_call_language(when, lang, expected):
    assert lexicon.localize_when(when, lang) == expected


def test_charges_are_named_in_the_call_language():
    assert lexicon.charge_words("gas refill / parts", "hi") == "गैस भरवाई या पुर्ज़े"
    assert lexicon.charge_words("GST 18%", "mr") == "जीएसटी 18 टक्के"
    assert lexicon.charge_words("widget fee", "gu") == "બીજા ખર્ચ"      # unknown -> "other charges", never English
    assert lexicon.charge_words("material / parts", "en") == "material or parts"


@pytest.mark.parametrize("place, state, code", [
    ("Koramangala, Bengaluru", "", "kn"), ("JP Nagar", "", "kn"), ("Kothrud, Pune", "", "mr"),
    ("Andheri, Mumbai", "", "mr"), ("T Nagar, Chennai", "", "ta"), ("Gachibowli, Hyderabad", "", "te"),
    ("Salt Lake, Kolkata", "", "bn"), ("Navrangpura, Ahmedabad", "", "gu"), ("Kakkanad, Kochi", "", "ml"),
    ("Model Town, Ludhiana", "", "pa"), ("Saheed Nagar, Bhubaneswar", "", "od"), ("Sector 18, Noida", "", "hi"),
    ("some village", "Maharashtra", "mr"), ("some village", "Odisha", "od"),
])
def test_call_language_follows_the_place(place, state, code):
    assert languages.detect(place, state)[0] == code


def test_unknown_place_keeps_no_language():
    assert languages.detect("near me") == (None, "")


@pytest.mark.parametrize("text, code", [("call them in Marathi", "mr"), ("मराठी में बात करो", "mr"), ("Tamil", "ta"),
                                        ("ಕನ್ನಡ", "kn"), ("I need a Marathi cook", None), ("hindi and tamil", None)])
def test_language_requests(text, code):
    assert languages.requested_language(text) == code


@pytest.mark.parametrize("text, yes, no", [("yes", True, False), ("हाँ", True, False), ("ಹೌದು", True, False),
                                           ("चालेल", True, False), ("go ahead", True, False), ("no", False, True),
                                           ("नहीं", False, True), ("no problem", True, False),
                                           ("I need a cook in pune please right now", False, False)])
def test_yes_and_no_in_many_languages(text, yes, no):
    assert languages.is_yes(text) is yes and languages.is_no(text) is no


@pytest.mark.parametrize("lang", NATIVE)
def test_opening_lines_are_native(lang):
    places = ("Koramangala", "Kempegowda Airport", "JP Nagar", "Mysuru")
    for req in (ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800, location="Koramangala",
                               when="today evening", details="kitchen tap leaking"),
                ServiceRequest(category="dentist", goal="appointment", target_price=0, walkaway_ceiling=0,
                               location="Koramangala", when="tomorrow 11 am"),
                ServiceRequest(category="cab_outstation", pickup="JP Nagar", drop="Mysuru", when="tomorrow 6 am",
                               target_price=2200, walkaway_ceiling=2600),
                ServiceRequest(category="other", service_label="someone to fix my gate", target_price=500,
                               walkaway_ceiling=900, location="Koramangala")):
        line = briefs.opening_line(req, lang)
        assert not languages.latin_words(line, allow=places), (lang, line)


def test_gemini_localized_words_are_used_in_the_opening():
    req = ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800, location="Koramangala",
                         when="today", details="kitchen tap leaking")
    line = briefs.opening_line(req, "kn", local={"place": "ಕೋರಮಂಗಲ", "details": "ಅಡುಗೆಮನೆ ನಲ್ಲಿ ಸೋರುತ್ತಿದೆ"})
    assert "ಕೋರಮಂಗಲ" in line and "ಸೋರುತ್ತಿದೆ" in line and not languages.latin_words(line)


@pytest.mark.parametrize("lang", lexicon.ALL)
def test_bolna_agent_for_every_language(lang):
    payload = bolna_client.build_agent_payload(lang, "https://swarm.example.com")
    task = payload["agent_config"]["tasks"][0]
    tools, cfg = task["tools_config"], task["task_config"]
    assert tools["transcriber"]["language"] == lang
    assert tools["synthesizer"]["provider_config"]["language"] == languages.get(lang).sarvam
    assert cfg["backchanneling"] is (lang == "en")   # Bolna's fillers are English
    assert tools["transcriber"]["endpointing"] >= 800 and cfg["incremental_delay"] > 0   # patient turn-taking
    assert cfg["number_of_words_for_interruption"] >= 2
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "{job_native}" in prompt and "{session_id}" in prompt
    if lang != "en":
        name = languages.name(lang)
        assert f"Speak ONLY {name}" in prompt and "Never say an English word" in prompt
        for line in (cfg["check_user_online_message"],
                     tools["api_tools"]["tools_params"]["submit_and_counter"]["pre_call_message"]):
            assert not languages.latin_words(line), line
    assert payload["agent_config"]["agent_name"] == f"Swarm Negotiator ({lang.upper()})"


def test_every_scenario_in_every_language():
    report = asyncio.run(scenarios.language_sweep())
    failures = {code: v["failures"] for code, v in report["languages"].items() if v["failures"]}
    assert report["passed"] == report["total"] == len(scenarios.SCENARIOS) * 11, failures


@pytest.mark.parametrize("lang", lexicon.ALL)
def test_simulated_calls_in_every_language(lang, monkeypatch):
    """A full simulated call per language: every agent line native; the vendor speaks the language too."""
    monkeypatch.setattr("app.config.settings.sim_speed", 400)

    async def run():
        engine = AuctionEngine(Store(":memory:"))
        req = ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800, location="Koramangala",
                             when="today", details="tap leaking")
        session = await engine.create_session(req, [{"name": "Lakshmi Plumbing", "language": lang}])
        persona = {"name": "Lakshmi Plumbing", "language": lang, "style": "trap", "open_ratio_t": 1.2,
                   "floor_ratio_t": 1.05, "concession": 0.5}
        await SimulatedLeg(engine, session.session_id, "A", persona).run()
        return session.legs["A"]
    start = time.time()
    leg = asyncio.run(run())
    assert time.time() - start < 10
    agent = [t for t in leg.transcript if t["role"] == "agent"]
    vendor = [t for t in leg.transcript if t["role"] == "vendor"]
    assert len(agent) >= 3 and leg.status in {"HOLD", "ENDED"}
    for t in agent + vendor:
        if lang != "en":
            assert not languages.latin_words(t["text"], allow=("Koramangala", "Lakshmi Plumbing")), t
            assert t.get("gloss"), t   # the screen shows the English meaning
