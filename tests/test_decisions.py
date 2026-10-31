"""The concierge's decision tree (app.intake.decide): greet, ask one thing, repeat, never get stuck, confirm, go."""

import asyncio

import pytest

from app import intake, languages, services, userlex


def chat(messages, **kw):
    async def run():
        cid, r = None, None
        for m in messages:
            r = await intake.chat(cid, m, **kw)
            cid = r["conversation_id"]
        return r
    return asyncio.run(run())


PLUMBER = ["", "plumber needed in JP Nagar today", "skip"]       # next: the budget question


def test_first_visit_gets_the_full_greeting_and_a_returning_user_a_short_one():
    assert chat([""])["reply"] == userlex.line("en", "greet")
    assert chat([""], returning=True)["reply"] == userlex.line("en", "greet_back")
    assert chat([""], heard_lang="kn", returning=True)["reply"] == userlex.line("kn", "greet_back")


@pytest.mark.parametrize("lang", userlex.ALL)
def test_greetings_are_short_enough_to_speak_and_explain_what_to_do(lang):
    greet = userlex.line(lang, "greet")
    assert len(greet) < 260 and "“" in greet          # one example in quotes, so people know what to say
    assert len(userlex.line(lang, "greet_back")) < 60


@pytest.mark.parametrize("text", ["what?", "Sorry?", "say that again please", "फिर से", "क्या?", "மறுபடியும்",
                                  "ಮತ್ತೆ ಹೇಳಿ", "ఏంటి", "আবার বলুন", "ਦੁਬਾਰਾ", "କ'ଣ", "വീണ്ടും", "ફરીથી", "पुन्हा सांगा"])
def test_repeat_requests_in_every_language(text):
    assert languages.is_repeat(text)


@pytest.mark.parametrize("text", ["what about tomorrow", "Koramangala", "around 500", "again the tap leaks"])
def test_real_answers_are_not_repeat_requests(text):
    assert not languages.is_repeat(text)


def test_what_repeats_the_question_plainly_and_changes_nothing():
    before = chat(["", "I need a plumber"])
    r = chat(["", "I need a plumber", "what?"])
    assert r["decision"] == "repeat" and r["reply"] == before["reply"]
    assert r["task"] == before["task"]


def test_a_greeting_or_a_bare_place_is_not_taken_for_the_service():
    r = chat(["", "hello"])
    assert not r["task"].get("service_key") and r["reply"] == userlex.line("en", "service_q")
    r = chat(["", "Koramangala, Bengaluru"])
    assert not r["task"].get("service_key") and r["task"]["location"] == "Koramangala, Bengaluru"
    assert r["reply"] == userlex.line("en", "service_q")
    r = chat(["", "Koramangala, Bengaluru", "I need a plumber tomorrow"])
    assert r["task"]["service_key"] == "plumber" and r["task"]["location"] == "Koramangala, Bengaluru"


def test_an_answer_that_gives_nothing_is_met_with_sorry_then_a_safe_default():
    r = chat(PLUMBER + ["hmm"])
    assert r["decision"] == "ask" and r["reply"].startswith(userlex.line("en", "again"))
    r = chat(PLUMBER + ["hmm", "umm"])
    low, high = services.suggested_budget(services.BY_KEY["plumber"])
    assert r["decision"] == "confirm" and r["ready"]
    assert (r["task"]["target_price"], r["task"]["walkaway_ceiling"]) == (low, high)
    assert any("usual price range" in x["text"] for x in r["trace"])


def test_a_skipped_time_becomes_as_soon_as_possible_after_two_tries():
    r = chat(["", "I need a cook for my family in Koramangala", "skip", "skip"])
    assert r["task"]["when"] == "as soon as possible"
    assert r["missing"][0] == "details"


def test_the_place_and_the_service_are_never_guessed():
    r = chat(["", "I need a plumber", "", ""])        # empty turns (GPS, language picker) are not misses
    assert r["decision"] == "ask" and r["missing"][0] == "location" and not r["task"].get("location")


def test_yes_with_a_change_updates_the_summary_instead_of_calling():
    steps = PLUMBER + ["around 500 max 900"]
    r = chat(steps + ["yes but tomorrow 5 pm"])
    assert r["action"] == "" and r["decision"] == "confirm" and r["task"]["when"].startswith("tomorrow")
    assert chat(steps + ["yes"])["action"] == "go"
    assert chat(steps + ["no"])["decision"] == "change"
