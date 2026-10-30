"""Talking to Swarm in any of the 11 languages: it detects the language, answers, sums up and recommends in it."""

import asyncio
import re

import pytest

from app import advisor, intake, languages, lexicon, userlex
from app.auction_engine import AuctionEngine, Store
from app.pricing import ServiceRequest
from app.simulator import SimulatedLeg

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def chat(messages, **kw):
    async def run():
        cid, r = None, None
        for m in messages:
            r = await intake.chat(cid, m, **kw)
            cid = r["conversation_id"]
        return r
    return asyncio.run(run())


# ---------------------------------------------------------------- the line tables

@pytest.mark.parametrize("lang", userlex.ALL)
def test_every_user_line_exists_natively_in_every_language(lang):
    table, en = userlex.LINES[lang], userlex.LINES["en"]
    assert set(table) == set(en)
    for key, text in table.items():
        assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(en[key])), (lang, key)
        if lang != "en":
            bare = PLACEHOLDER.sub(" ", text)
            assert not languages.latin_words(bare), (lang, key, text)
            assert not languages.foreign_letters(bare, lang), (lang, key, text)
    assert len(userlex.chips(lang, "chip_examples")) == 4
    assert sorted(userlex.LANGUAGE_NAMES[lang]) == sorted(lexicon.ALL)
    # The tap-to-say answers are understood without the chat model
    assert languages.is_yes(table["chip_yes"]) and not languages.is_no(table["chip_yes"])
    assert userlex.matches_chip(table["chip_change"], "chip_change")
    if lang != "en":
        for key in ("chip_today", "chip_tomorrow", "chip_weekend"):
            assert lexicon.english_when(table[key]), (lang, key)


def test_browser_gets_the_narration_in_each_language():
    lines = userlex.browser_lines("ta")
    assert lines["code"] == "ta" and lines["lines"]["recommend"].startswith("எல்லா")
    assert lines["language_names"]["mr"] == "மராத்தி"
    assert userlex.browser_lines("xx")["code"] == "en"


# ---------------------------------------------------------------- detecting the language

@pytest.mark.parametrize("text,heard,current,expected", [
    ("मला उद्या प्लंबर पाहिजे", None, "en", "mr"),
    ("मुझे कल प्लंबर चाहिए", None, "en", "hi"),
    ("हो", None, "mr", "mr"),                       # a bare Devanagari "yes" keeps Marathi
    ("எனக்கு நாளை பிளம்பர் வேண்டும்", None, "en", "ta"),
    ("ನನಗೆ ನಾಳೆ ಪ್ಲಂಬರ್ ಬೇಕು", None, "en", "kn"),
    ("నాకు రేపు ప్లంబర్ కావాలి", None, "en", "te"),
    ("আমার কাল একজন মিস্ত্রি চাই", None, "en", "bn"),
    ("મારે કાલે પ્લમ્બર જોઈએ છે", None, "en", "gu"),
    ("എനിക്ക് നാളെ പ്ലംബർ വേണം", None, "en", "ml"),
    ("ਮੈਨੂੰ ਕੱਲ੍ਹ ਪਲੰਬਰ ਚਾਹੀਦਾ ਹੈ", None, "en", "pa"),
    ("ମୋତେ କାଲି ପ୍ଲମ୍ବର ଦରକାର", None, "en", "od"),
    ("mujhe kal plumber chahiye", None, "en", "hi"),   # romanized Hindi
    ("I need a plumber tomorrow", None, "ta", "en"),
    ("Koramangala, Bengaluru", "en", "ta", "ta"),     # place names are Latin in every language
    ("ok", "en", "kn", "kn"), ("2000", None, "te", "te"),
    ("plumber chaiye Kothrud tomorrow", "hi", "en", "hi"),   # what speech recognition heard, on a real sentence
    ("Kothrud, Pune", "mr", "ta", "ta"),               # ...but not on a bare place name
])
def test_user_language(text, heard, current, expected):
    assert languages.user_language(text, heard, current) == expected


# ---------------------------------------------------------------- a whole conversation in each language

@pytest.mark.parametrize("lang", userlex.ALL)
def test_whole_request_in_the_users_language_without_gemini(lang):
    t = userlex.LINES[lang]
    first = f"{t['chip_tomorrow']} Koramangala, Bengaluru plumber" if lang != "en" else \
        "I need a plumber in Koramangala, Bengaluru tomorrow morning"
    steps = ["", first, t["chip_skip"], userlex.line(lang, "chip_budget", target=500, high=900)]
    r = chat(steps, heard_lang=lang)
    assert r["user_lang"] == lang
    # stored in English words, so every call can say it in its own language
    assert r["task"]["when"] == ("tomorrow" if lang == "en" else "tomorrow morning")
    assert r["reply"] == userlex.line(lang, "confirm", summary=intake.summary(r["task"], lang), n=3,
                                      language=userlex.language_name(lang, "kn")), r["reply"]
    assert "₹500" in r["reply"] and "₹900" in r["reply"]
    assert r["suggestions"] == [t["chip_yes"], t["chip_change"]]
    if lang != "en":
        assert languages.writes_in(r["reply"], lang)
        assert lexicon.service_name("plumber", "plumber", lang) in r["reply"]
        assert not languages.latin_words(r["reply"], allow=("Koramangala", "Bengaluru")), r["reply"]
    go = chat(steps + [t["chip_yes"]], heard_lang=lang)
    assert go["action"] == "go" and go["reply"] == userlex.line(lang, "go", service=intake.service_for_user(
        go["task"], lang))
    req = intake.to_request(go["task"])
    assert req.user_language == lang and "₹500" in req.user_summary and "**" not in req.user_summary


def test_the_greeting_follows_the_browser_and_the_reply_follows_the_speaker():
    assert chat([""], heard_lang="ta")["reply"] == userlex.line("ta", "greet")
    r = chat(["", "मला Kothrud, Pune मध्ये plumber पाहिजे"], heard_lang="ta")
    assert r["user_lang"] == "mr" and r["reply"] == userlex.line("mr", "when", service="प्लंबर")
    assert r["task"]["location"] == "Kothrud, Pune" and r["call_language"]["code"] == "mr"
    assert any("speaking Marathi" in x["text"] for x in r["trace"])


def test_the_first_language_spoken_is_kept_for_the_whole_conversation():
    """Detected once, then fixed: later English, another language, or what speech recognition hears don't flip it."""
    r = chat(["", "எனக்கு Koramangala, Bengaluru-ல plumber வேண்டும்"], heard_lang="en")
    assert r["user_lang"] == "ta" and r["user_lang_locked"]
    later = ["I need it tomorrow morning please", "मुझे कल सुबह चाहिए", "ok"]
    for i in range(1, len(later) + 1):
        r = chat(["", "எனக்கு Koramangala, Bengaluru-ல plumber வேண்டும்", *later[:i]], heard_lang="hi")
        assert r["user_lang"] == "ta" and languages.writes_in(r["reply"], "ta"), r["reply"]
    assert sum("speaking" in x["text"] for x in r["trace"]) == 1


def test_messages_that_show_no_language_decide_nothing_yet():
    r = chat(["", "Koramangala, Bengaluru"], heard_lang="kn")
    assert r["user_lang"] == "kn" and not r["user_lang_locked"]      # still the browser's guess, not fixed
    r = chat(["", "Koramangala, Bengaluru", "నాకు ప్లంబర్ కావాలి"], heard_lang="kn")
    assert r["user_lang"] == "te" and r["user_lang_locked"]


def test_a_new_conversation_detects_again():
    assert chat(["", "எனக்கு plumber வேண்டும்"])["user_lang"] == "ta"
    assert chat(["", "मला plumber पाहिजे"])["user_lang"] == "mr"


# ---------------------------------------------------------------- the call language: the place's, and it stays

def test_call_language_is_the_local_language_of_the_place_not_the_speakers():
    r = chat(["எனக்கு Kothrud, Pune-ல plumber வேண்டும்"])
    assert r["user_lang"] == "ta" and r["call_language"]["code"] == "mr"   # Tamil speaker, Marathi calls in Pune
    r = chat(["எனக்கு Kothrud, Pune-ல plumber வேண்டும்", "இல்லை, Koramangala, Bengaluru"])
    assert r["call_language"]["code"] == "kn"                               # another place, its language


def test_a_named_call_language_sticks_even_when_the_place_changes():
    r = chat(["I need a plumber in Kothrud, Pune", "call them in Hindi", "actually in Koramangala, Bengaluru"])
    assert r["call_language"]["code"] == "hi" and r["call_language"]["locked"]
    r = chat(["எனக்கு Kothrud, Pune-ல plumber வேண்டும்", "தமிழ் மொழியில் அழையுங்கள்"])   # named in Tamil
    assert r["call_language"]["code"] == "ta"


def test_the_models_guess_never_changes_the_call_language(monkeypatch):
    import json
    from app import llm

    async def gen(*a, **k):   # the model "helpfully" picks the speaker's own language for the calls
        return json.dumps({"updates": {"service_key": "plumber", "location": "Kothrud, Pune", "call_language": "ta"},
                           "asking": "when", "reply": "சரி. எப்போது வேண்டும்?", "intent": "inform"})
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", gen)
    r = chat(["எனக்கு Kothrud-ல plumber வேண்டும்"])
    assert r["call_language"]["code"] == "mr" and not r["call_language"]["locked"]


def test_an_unknown_place_gets_the_language_of_its_state(monkeypatch):
    from app import places

    async def state_of(place):
        return {"Wagholi": "Maharashtra"}.get(place.split(",")[0].strip(), "")
    monkeypatch.setattr(places, "state_of", state_of)
    r = chat(["I need a plumber", "Wagholi"])
    assert r["task"]["state"] == "Maharashtra" and r["call_language"]["code"] == "mr"


def test_a_typed_place_wins_over_an_old_gps_state(monkeypatch):
    from app import places

    async def reverse(lat, lng):
        places._geo_state[(round(lat, 3), round(lng, 3))] = "Karnataka"
        return "Shivaji Nagar"
    monkeypatch.setattr(places, "reverse_geocode", reverse)

    async def run():
        r = await intake.chat(None, "I need an electrician near me")
        r = await intake.chat(r["conversation_id"], "", lat=12.98, lng=77.6)
        assert r["call_language"]["code"] == "kn"
        return await intake.chat(r["conversation_id"], "no, in Kothrud, Pune")
    r = asyncio.run(run())
    assert r["task"]["location"] == "Kothrud, Pune" and "state" not in r["task"]
    assert r["call_language"]["code"] == "mr"


def test_the_call_agent_cannot_switch_language_mid_call():
    from app import bolna_client
    payload = bolna_client.build_agent_payload("mr", "https://swarm.example.com")
    tools = payload["agent_config"]["tasks"][0]["tools_config"]
    assert tools["transcriber"]["language"] == "mr" and not tools["transcriber"].get("multilingual")
    assert "language_injection_mode" not in payload["agent_config"]["tasks"][0]["task_config"]
    assert "switch_language" not in {t["name"] for t in tools["api_tools"]["tools"]}
    prompt = payload["agent_prompts"]["task_1"]["system_prompt"]
    assert "for the whole call" in prompt and "carry on in simple Marathi" in prompt


def test_a_language_picked_in_the_settings_turns_detection_off():
    r = chat(["", "I need a plumber in Koramangala, Bengaluru"], user_lang="kn")
    assert r["user_lang"] == "kn" and languages.writes_in(r["reply"], "kn")
    r = chat(["", "I need a plumber in Koramangala, Bengaluru"], user_lang="auto")
    assert r["user_lang"] == "en"


def test_a_gemini_reply_in_the_wrong_language_is_not_used(monkeypatch):
    import json
    from app import llm

    async def gen(*a, **k):
        assert "write \"reply\" in Tamil, in Tamil script" in a[0]
        return json.dumps({"updates": {"service_key": "plumber", "location": "Koramangala, Bengaluru"},
                           "asking": "when", "reply": "Okay, when do you need the plumber?", "intent": "inform"})
    monkeypatch.setattr(llm, "enabled", lambda: True)
    monkeypatch.setattr(llm, "generate", gen)
    r = chat(["எனக்கு Koramangala-ல ஒரு plumber வேண்டும்"])
    assert r["user_lang"] == "ta" and r["reply"] == userlex.line("ta", "when", service="பிளம்பர்")


# ---------------------------------------------------------------- the calls, narrated and recommended in it

def test_recommendation_and_slots_in_the_users_language(monkeypatch):
    monkeypatch.setattr("app.config.settings.sim_speed", 400)

    async def run():
        engine = AuctionEngine(Store(":memory:"))
        req = ServiceRequest(category="plumber", target_price=500, walkaway_ceiling=800, location="Kothrud, Pune",
                             when="today", details="tap leaking", user_language="ta")
        session = await engine.create_session(req, [{"name": "Lakshmi Plumbing", "language": "mr"}])
        persona = {"name": "Lakshmi Plumbing", "language": "mr", "style": "trap", "open_ratio_t": 1.2,
                   "floor_ratio_t": 1.05, "concession": 0.5}
        await SimulatedLeg(engine, session.session_id, "A", persona).run()
        return session
    session = asyncio.run(run())
    rec = advisor.recommend(session)
    assert rec["language"] == "ta" and rec["leg_id"]
    for text in [rec["what"], *rec["reasons"]]:
        assert languages.writes_in(text, "ta"), text
        assert not languages.latin_words(text), text
    public = session.public()
    assert public["request"]["user_language"] == "ta"
    assert advisor.user_slot("tomorrow 11 AM", session.request) == lexicon.localize_when("tomorrow 11 AM", "ta")
    assert advisor.user_items(["material / parts"], session.request)[0] == \
        lexicon.charge_words("material / parts", "ta", joiner="and")


def test_a_time_said_in_one_language_is_never_read_out_on_a_call_in_another():
    assert lexicon.english_when("நாளை காலை") == "tomorrow morning"
    assert lexicon.english_when("उद्या सकाळी 9 वाजता") == "tomorrow morning 9"
    assert lexicon.localize_when("நாளை காலை", "mr") == lexicon.WHEN_WORDS["soon"]["mr"]   # never Tamil on a Marathi call
    assert lexicon.localize_when("उद्या सकाळी", "mr") == "उद्या सकाळी"
