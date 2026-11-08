from __future__ import annotations

import pytest

from backend.app.db.registry import get_registry
from backend.app.services.advisor_agent import AdvisorAgent


@pytest.fixture
def advisor() -> AdvisorAgent:
    return AdvisorAgent(get_registry())


def test_hinglish_slot_filling_and_cpq_flow(advisor: AdvisorAgent):
    user_turn = (
        "Bhaiya, meri Jaipur mein 2 medical shops hain, total 6 staff hain. "
        "Shaam ko rush time pe Jio hotspot baith jata hai toh GPay atakta hai. "
        "Mere paas Udyam hai par GST number nahi hai. Budget lagbhag 2500 rupay hai."
    )

    response = advisor.process_turn(user_turn)

    # 1. Verify Slot Extraction
    assert response.slots.is_complete is True
    assert response.slots.segment == "micro"
    assert response.slots.city_tier == 2  # Jaipur is Tier 2
    assert response.slots.num_sites == 2
    assert response.slots.employee_count == 6
    assert response.slots.has_gstin is False
    assert response.slots.monthly_budget_cap == 2500
    assert "pos_upi_uptime" in response.slots.detected_needs

    # 2. Verify Deterministic Quote
    assert response.quote is not None
    assert response.quote.net_mrc_inr <= 2500
    assert response.quote.gst_exempt_eligible is True  # Non-GST customer
    assert len(response.recommended_skus) >= 1

    # 3. Verify Zero Price Divergence
    formatted_price = f"{response.quote.net_mrc_inr:,}"
    assert formatted_price in response.display_markdown
    assert str(response.quote.net_mrc_inr) in response.spoken_tts_text

    # 4. Verify Spoken TTS Text Hygiene (Zero markdown characters)
    assert "*" not in response.spoken_tts_text
    assert "#" not in response.spoken_tts_text
    assert "[" not in response.spoken_tts_text
    assert "]" not in response.spoken_tts_text
    assert "₹" not in response.spoken_tts_text  # Replaced by 'rupaye'


def test_english_ca_firm_flow(advisor: AdvisorAgent):
    user_turn = (
        "We are a CA firm in Mumbai with 15 employees, single branch, active GSTIN. "
        "Need fast broadband and cloud email backup for our files."
    )

    response = advisor.process_turn(user_turn)

    assert response.slots.is_complete is True
    assert response.slots.segment == "prof"
    assert response.slots.city_tier == 1  # Mumbai is Tier 1
    assert response.slots.employee_count == 15
    assert response.slots.has_gstin is True
    assert "cloud_email" in response.slots.detected_needs

    assert response.quote is not None
    assert response.quote.net_mrc_inr > 0
    # Professional firm in Tier 1 gets Biz Fibre 300
    assert "P02" in response.recommended_skus


def test_incomplete_turn_prompts_clarification(advisor: AdvisorAgent):
    user_turn = "Hello, I want an internet connection."

    response = advisor.process_turn(user_turn)

    assert response.slots.is_complete is False
    assert response.quote is None
    assert len(response.slots.missing_slots) > 0
    assert "Namaste" in response.display_markdown or "clarify" in response.display_markdown
    assert len(response.next_suggested_prompts) > 0


def test_romanized_latin_script_indic_flow(advisor: AdvisorAgent):
    import re
    # 1. Test Telugu in Latin/English alphabet ("bagunnara ela unnaru")
    res_latin = advisor.process_turn("bagunnara ela unnaru", script_mode="latin")
    assert "Nenu bagunnanu" in res_latin.display_markdown or "dhanyavadalu" in res_latin.display_markdown
    # Verify zero non-Latin Indic characters in Latin mode
    assert not re.search(r"[\u0900-\u0D7F]", res_latin.display_markdown)
    assert res_latin.display_latin is not None
    assert res_latin.display_native is not None
    assert res_latin.user_latin is not None
    assert res_latin.user_native is not None

    # 2. Test Telugu in Native Script ("నమస్కారం బాగున్నారా")
    res_native = advisor.process_turn("నమస్కారం బాగున్నారా", script_mode="native")
    assert "బాగున్నాను" in res_native.display_markdown or "నమస్కారం" in res_native.display_markdown
    assert res_native.display_latin is not None
    assert res_native.display_native is not None

    # 3. Test slot extraction from Romanized query ("hyderabad lo 4 staff clinic undi, budget 2500")
    res_slots = advisor.process_turn("hyderabad lo 4 staff clinic undi, budget 2500", script_mode="latin")
    assert res_slots.slots.segment == "prof"
    assert res_slots.slots.city_tier == 1  # Hyderabad is Tier 1
    assert res_slots.slots.employee_count == 4


def test_advisor_comparison_query(advisor: AdvisorAgent):
    from backend.app.models.schemas import MSMEProfileSlots

    prev = MSMEProfileSlots(
        segment="micro",
        city_tier=2,
        employee_count=3,
        has_gstin=False,
        monthly_budget_cap=1500,
        is_complete=True,
    )

    # When user asks to compare between multiple plans instead of accepting just one
    response = advisor.process_turn("can u compare between multiple its not like only oie right", previous_slots=prev)

    # Must provide comparison between options, not just dump a single rigid bundle template
    assert "Option 1" in response.display_markdown or "options" in response.display_markdown.lower() or "compare" in response.display_markdown.lower()
    assert response.quote is not None
    assert len(response.recommended_skus) > 0
    assert response.spoken_tts_text is not None
    assert "*" not in response.spoken_tts_text
    assert "#" not in response.spoken_tts_text


def test_romanized_telugu_query_preserves_language(advisor: AdvisorAgent):
    """Verifies that Romanized Telugu queries (Tenglish) trigger natural Romanized Telugu
    in 'display_latin' (English Words mode) and authentic Telugu script in 'display_native'.
    """
    import re

    query = "ekkuva money karchupettalanukovatle minimum enthalo ayipothado cheppandi"

    # 1. Romanized / English Words mode (script_mode='latin')
    res_latin = advisor.process_turn(query, script_mode="latin")
    assert res_latin.display_latin is not None
    assert res_latin.display_native is not None
    assert res_latin.display_markdown == res_latin.display_latin

    # Verify response is in Romanized Telugu (not standard English)
    lat_lower = res_latin.display_latin.lower()
    assert any(term in lat_lower for term in ["arthamaindi", "takkuva", "takkuvalo", "kavali", "cheppandi", "airfiber"])
    # Verify zero non-Latin Indic characters in Latin mode
    assert not re.search(r"[\u0900-\u0D7F]", res_latin.display_latin)

    # 2. Native Script mode (script_mode='native')
    res_native = advisor.process_turn(query, script_mode="native")
    assert res_native.display_markdown == res_native.display_native
    # Verify proper Telugu script characters are present in native output
    assert re.search(r"[\u0C00-\u0C7F]", res_native.display_native)

    # 3. Test Romanized Hindi (Hinglish) preservation
    query_hi = "zyada paise kharch nahi karna chahte minimum kitne me ho jayega bataiye"
    res_hi = advisor.process_turn(query_hi, script_mode="latin")
    assert res_hi.display_latin is not None
    hi_lower = res_hi.display_latin.lower()
    assert any(term in hi_lower for term in ["samajh", "kam", "budget", "airfiber", "bataiye"])
    assert not re.search(r"[\u0900-\u0D7F]", res_hi.display_latin)


def test_english_query_does_not_convert_to_telugu_in_native_script_mode(advisor: AdvisorAgent):
    """Verifies that English queries remain in English even when script_mode='native'
    (Issue #8: English chat turns must not turn into hardcoded Telugu).
    """
    import re

    # 1. English greeting in native script mode
    res = advisor.process_turn("Hello", language_code="en", script_mode="native")
    assert res.display_native is not None
    # Must NOT contain Telugu characters
    assert not re.search(r"[\u0C00-\u0C7F]", res.display_native)
    assert not re.search(r"[\u0C00-\u0C7F]", res.display_markdown)
    assert "Airtel" in res.display_native or "Welcome" in res.display_native or "Namaste" in res.display_native

    # 2. English query with auto language code in native script mode
    res_auto = advisor.process_turn("Hi, I want internet for my store", language_code="auto", script_mode="native")
    assert not re.search(r"[\u0C00-\u0C7F]", res_auto.display_native)


def test_simple_greeting_fast_path_latency(advisor: AdvisorAgent):
    """Verifies that basic greetings hit an instant deterministic sub-100ms fast-path."""
    import time

    start = time.time()
    res = advisor.process_turn("Hello", language_code="en")
    elapsed_ms = (time.time() - start) * 1000

    assert elapsed_ms < 100, f"Greeting latency too high: {elapsed_ms:.1f}ms"
    assert res.display_latin is not None
    assert len(res.display_latin) > 10


def test_universal_multilingual_no_language_restriction(advisor: AdvisorAgent):
    """Verifies that international queries (e.g. Spanish 'hola', French 'bonjour')
    are auto-detected and responded to in that language without inverted punctuation
    artifacts (no ¡) or restrictive disclaimers.
    """
    import re

    # 1. Spanish 'hola'
    res_es = advisor.process_turn("hola")
    assert res_es.display_latin.startswith("Hola!"), f"Expected clean Hola!, got: {res_es.display_latin[:10]}"
    assert "¡" not in res_es.display_latin, "Found unexpected inverted exclamation mark ¡"
    assert "superpower" not in res_es.display_latin.lower()
    # Native mode must not have Telugu
    assert not re.search(r"[\u0C00-\u0C7F]", res_es.display_native)

    # 2. French 'bonjour'
    res_fr = advisor.process_turn("bonjour")
    assert res_fr.display_latin.startswith("Bonjour!")
    assert not re.search(r"[\u0C00-\u0C7F]", res_fr.display_native)


def test_turn2_only_udyam_resolves_and_calculates_bundle(advisor: AdvisorAgent):
    """Verifies that answering 'only udyam' to GST question completes profile slots
    and computes the recommended bundle without repeating the question.
    """
    from backend.app.models.schemas import MSMEProfileSlots

    prev = MSMEProfileSlots(segment="micro", city_tier=1, employee_count=4)
    res = advisor.process_turn("only udyam", previous_slots=prev)

    assert res.slots.is_complete is True
    assert res.slots.has_gstin is False
    assert len(res.recommended_skus) > 0
    assert res.quote is not None
    assert "Recommended Bundle" in res.display_markdown or "Optimal" in res.display_markdown or len(res.display_markdown) > 50
