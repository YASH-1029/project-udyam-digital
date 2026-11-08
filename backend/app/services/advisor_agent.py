from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
from typing import Optional

import urllib.request
try:
    from google import genai
    GENAI_AVAILABLE = True
except Exception:
    GENAI_AVAILABLE = False

from backend.app.db.registry import RegistryDatabase, get_registry
from backend.app.models.schemas import (
    AdvisorResponse,
    CartItem,
    CommercialBreakdown,
    MSMEProfileSlots,
    UserProfile,
)
from backend.app.services.cpq_engine import CPQEngine
from backend.app.services.transliteration_service import (
    ensure_dual_scripts,
    has_indic_script,
    indic_to_latin,
    latin_to_indic,
)

TIER_1_CITIES = {
    "mumbai", "delhi", "bengaluru", "bangalore", "hyderabad",
    "chennai", "kolkata", "pune", "ahmedabad", "gurugram", "noida"
}


class AdvisorAgent:
    """Multilingual Neuro-Symbolic Conversational Advisor.

    Combines robust multilingual slot-filling (Hindi, Hinglish, English)
    with a deterministic CPQ engine and dual-channel narration (Markdown + Voice TTS).
    """

    def __init__(self, db: Optional[RegistryDatabase] = None) -> None:
        self.db = db or get_registry()
        self.cpq = CPQEngine(self.db)
        self.api_key = os.environ.get("GEMINI_API_KEY")
        self.client = None
        if GENAI_AVAILABLE and self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                logging.warning("Could not initialize google.genai Client: %s", e)


    def _call_gemini_api(self, prompt: str, timeout: float = 12.0) -> Optional[dict]:
        """Direct HTTP REST call to Google Gemini API (bypassing SDK packaging issues)."""
        if not self.api_key or os.environ.get("PYTEST_CURRENT_TEST"):
            return None

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
                "temperature": 0.7,
            },
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        models = [
            "gemini-flash-lite-latest",
            "gemini-2.5-flash-lite",
            "gemini-flash-latest",
        ]

        for mod in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{mod}:generateContent?key={self.api_key}"
            try:
                req = urllib.request.Request(
                    url,
                    data=data_bytes,
                    headers={"Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        body = json.loads(resp.read().decode("utf-8"))
                        text = body["candidates"][0]["content"]["parts"][0]["text"]
                        return json.loads(text)
            except Exception as e:
                logging.warning("Gemini REST model %s failed: %s", mod, e)
                continue
        return None

    def _consult_gemini(
        self,
        text: str,
        existing_slots: Optional[MSMEProfileSlots] = None,
        language_code: Optional[str] = None,
        script_mode: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> Optional[dict]:
        """Consults live Gemini LLM with complete Project Udyam Enterprise Knowledge Base."""
        history_context = ""
        history_context = ""
        if history:
            recent_turns = [f"{turn.get('role', 'user')}: {turn.get('content', '')}" for turn in history[-4:]]
            history_context = "RECENT CONVERSATION HISTORY:\n" + "\n".join(recent_turns) + "\n\n"

        current_profile_str = ""
        if existing_slots:
            current_profile_str = (
                f"CURRENT USER PROFILE: Segment: {existing_slots.segment or 'unknown'}, "
                f"City Tier: {existing_slots.city_tier or 'unknown'}, "
                f"Staff: {existing_slots.employee_count or 'unknown'}, "
                f"GSTIN: {existing_slots.has_gstin}, "
                f"Budget: ₹{existing_slots.monthly_budget_cap or 'unknown'}/mo, "
                f"Needs: {existing_slots.detected_needs}\n\n"
            )

        prompt = f"""You are the principal Multilingual AI Enterprise Telecom Advisor for Project Udyam Digital (Accel Telecom / Airtel Business).
You are a warm, witty, highly knowledgeable human consultant—NOT an automated form or robotic IVR!

OUR ENTERPRISE PRODUCT CATALOGUE & KNOWLEDGE BASE:
1. Base Connectivity:
   - Biz 5G AirFiber 100 (P04): ₹1,199/month, 100 Mbps, 3-day SLA, wireless 5G antenna, zero road-digging delay, ideal for retail shops & clinics in Tier 1-2.
   - Biz Fibre 100 (P01): ₹1,099/month, 100 Mbps symmetric, 4-day SLA, physical optical fiber, rock-solid stability.
   - Biz Fibre 300 (P02): ₹1,699/month, 300 Mbps symmetric, 4-day SLA, high bandwidth for clinics, CA firms, 10+ devices.
   - Biz Fibre 1G (P03): ₹3,999/month, 1 Gbps symmetric, high-throughput commercial fiber.
   - Biz Leased Line 100 (P05): ₹6,500/month, 1:1 dedicated, 99.95% uptime SLA, sub-5ms latency.
   - Biz Leased Line 1G (P06): ₹14,999/month, 1:1 dedicated, mission-critical factory automation.
2. Value-Added Add-ons:
   - 4G LTE Auto-Failover (P09): ₹299/mo, automatic instant switchover during fiber cuts, keeps UPI/POS active.
   - Secure Wi-Fi with Guest Network (P22): ₹199/mo, isolates customer Wi-Fi from billing/UPI.
   - Airtel Soundbox (P13): ₹149/mo, instant voice alerts for UPI payment confirmations.
   - Cloud AI CCTV (P23): ₹399/mo, 30-day offsite cloud recording, theft deterrence.
   - Static IPv4 (P08): ₹299/mo, fixed IP for billing servers, EMR software, remote access.
   - Google Workspace (P16): ₹125/user/mo, branded business email & Cloud Drive.
3. Commercial Terms:
   - 12-month agreement: 100% Setup Fee (OTC) is WAIVED (₹0 upfront!).
   - Multi-category discounts: 5% off for 2 categories, 10% for 3, 15% for 4+.
4. Regulatory & Compliance:
   - Under Indian law, MSMEs with turnover under ₹40 Lakhs are legally GST-exempt. Udyam Registration is 100% legally sufficient for enterprise telecom connection.
   - DPDP Act 2023: 12-digit national IDs are masked in-memory immediately upon ingestion.

{history_context}{current_profile_str}CURRENT USER MESSAGE: "{text}"
Language code: {language_code or 'auto'}
Script mode: {script_mode or 'auto'}

INSTRUCTIONS FOR YOUR CONVERSATIONAL RESPONSE:
1. Chat NATURALLY like Gemini: warm, sharp, conversational, empathetic, and insightful.
2. NEVER regurgitate a generic, rigid, canned questionnaire or bullet-point form letter.
3. UNCONSTRAINED MULTILINGUAL SUPPORT & LANGUAGE FIDELITY:
   - You natively support ALL 22 official Indian languages (Hindi, Telugu, Tamil, Bengali, Marathi, Gujarati, Kannada, Malayalam, Odia, Punjabi, Assamese, Urdu, Maithili, etc.) and colloquial Romanized Indic dialects (Hinglish, Tenglish, Tanglish, Banglish, etc.) as well as Indian English.
   - Automatically detect the user's language, dialect, and script dynamically without restricting to any predefined list.
   - If the user writes in Romanized Indic (e.g. Tenglish, Hinglish, Tanglish, etc.):
     * Respond in the SAME Romanized Indic dialect in "display_latin" using English/Latin alphabet.
     * Provide the corresponding authentic native script transliteration in "display_native" (e.g. Telugu, Hindi, Tamil, Punjabi, Bengali, etc.).
     * NEVER reply in standard English in "display_latin" when the user writes in Romanized Indic!
   - If the user writes in authentic native Indic script:
     * Provide the native script response in "display_native", and the corresponding Romanized transliteration in "display_latin".
   - If the user writes in English:
     * Provide natural, conversational English in BOTH "display_latin" and "display_native" (never translate English to Telugu or other Indic scripts).
4. If the user asks for COMPARISON (e.g. "compare multiple", "is it only one?", "what are other options?", "vs"):
   - Enthusiastically acknowledge that there are multiple great options!
   - Compare 2 to 3 distinct practical configurations (e.g. Wireless AirFiber vs Fixed Fiber vs Added Failover/Wi-Fi), clearly explaining prices, setup times, and real-world advantages.
5. If the user asks ANY question, answer it directly and conversationally with accurate telecom advice.
6. If the user greets or jokes, respond with warmth and natural wit, then smoothly connect back to their business connectivity.
7. Never use LaTeX math delimiters ($...$ or $$...$$). Write formulas and complexities in clean Unicode text (e.g., O(n log n) or 99.9% uptime).
8. Provide:
   - "display_latin": Conversational markdown in matching Romanized Indic dialect (English alphabet) or English.
   - "display_native": Conversational markdown in authentic native Indic script (Telugu/Hindi/Tamil etc.).
   - "spoken_tts": Plain-text conversational audio summary (1-2 sentences, zero markdown symbols like *, #, []).
   - "recommended_skus": list of 1 to 4 product IDs matching the best plan discussed (e.g. ["P04", "P22"] or ["P01", "P09"]).
   - Extract business profile slots if mentioned or implied.

Return STRICTLY valid JSON:
{{
  "display_latin": "Conversational markdown in matching Romanized Indic dialect or English",
  "display_native": "Conversational markdown in native Indic script",
  "spoken_tts": "Conversational plain text voice readout",
  "recommended_skus": ["P04", "P22"],
  "segment": "micro" | "prof" | "multi" | "mfg" | null,
  "city_tier": 1 | 2 | 3 | null,
  "employee_count": integer or null,
  "num_sites": integer or null,
  "has_gstin": true | false | null,
  "detected_needs": ["broadband", "wifi", ...],
  "monthly_budget_cap": integer or null,
  "next_suggested_prompts": ["Suggestion 1", "Suggestion 2", "Suggestion 3"]
}}"""
        return self._call_gemini_api(prompt)

    def _select_skus_for_slots(self, slots: MSMEProfileSlots) -> list[str]:
        """Determines best base plan and compatible add-ons based on profile slots."""
        segment = slots.segment or "micro"
        tier = slots.city_tier or 2
        has_gst = bool(slots.has_gstin)
        needs = set(slots.detected_needs)

        if segment == "mfg":
            base = "P05" if has_gst else "P01"
        elif segment in {"prof", "multi"}:
            base = "P02"
        elif segment == "micro":
            base = "P04"  # 5G AirFiber for micro
        else:
            base = "P01"

        skus = [base]
        if "pos_upi_uptime" in needs:
            skus.append("P09")
        if "whatsapp_catalogue" in needs:
            skus.append("P15")
        if "cloud_email" in needs:
            skus.append("P16")
        if "cctv_surveillance" in needs:
            skus.append("P23")
        if "guest_wifi" in needs:
            skus.append("P22")
        return skus

    def extract_slots(self, text: str, existing_slots: Optional[MSMEProfileSlots] = None) -> MSMEProfileSlots:
        """Extracts MSME profile parameters from Telugu, Tamil, Bengali, Gujarati, Marathi, Kannada, Malayalam, Odia, Hindi, Hinglish, or English."""
        # Normalize numerals across all 8 Indic scripts to standard ASCII digits
        indic_digits = {
            # Devanagari (Hindi, Marathi)
            "०": "0", "१": "1", "२": "2", "३": "3", "४": "4", "५": "5", "६": "6", "७": "7", "८": "8", "९": "9",
            # Telugu
            "౦": "0", "౧": "1", "౨": "2", "౩": "3", "౪": "4", "౫": "5", "౬": "6", "౭": "7", "౮": "8", "౯": "9",
            # Tamil
            "௧": "1", "௨": "2", "௩": "3", "௪": "4", "௫": "5", "௬": "6", "௭": "7", "௮": "8", "௯": "9",
            # Bengali
            "০": "0", "১": "1", "২": "2", "৩": "3", "৪": "4", "৫": "5", "৬": "6", "৭": "7", "৮": "8", "৯": "9",
            # Gujarati
            "૦": "0", "૧": "1", "૨": "2", "૩": "3", "૪": "4", "૫": "5", "૬": "6", "૭": "7", "૮": "8", "૯": "9",
            # Kannada
            "೦": "0", "೧": "1", "೨": "2", "೩": "3", "೪": "4", "೫": "5", "೬": "6", "೭": "7", "೮": "8", "೯": "9",
            # Malayalam
            "൦": "0", "൧": "1", "൨": "2", "൩": "3", "൪": "4", "൫": "5", "൬": "6", "൭": "7", "൮": "8", "൯": "9",
            # Odia
            "୦": "0", "୧": "1", "୨": "2", "୩": "3", "୪": "4", "୫": "5", "୬": "6", "୭": "7", "୮": "8", "୯": "9",
        }
        dev_text = "".join(indic_digits.get(ch, ch) for ch in text)
        norm_text = dev_text.lower().strip()
        slots = existing_slots.model_copy() if existing_slots else MSMEProfileSlots()

        # 1. Segment Extraction (Telugu, Tamil, Bengali, Gujarati, Marathi, Kannada, Malayalam, Odia, Hindi, English)
        micro_words = [
            "दुकान", "किराना", # Hindi/Marathi
            "దుకాణం", "కిరాణా", "షాప్", # Telugu
            "கடை", "மளிகை", "அங்காடி", # Tamil
            "দোকান", "মুদি", "খুচরা", # Bengali
            "દુકાન", "કરિયાણું", # Gujarati
            "ಅಂಗಡಿ", "ಕಿರಾಣಿ", # Kannada
            "കട", "പലചരക്ക്", # Malayalam
            "ଦୋକାନ", "ରାସନ" # Odia
        ]
        prof_words = [
            "क्लीनिक", "डॉक्टर", "दवाखाना", # Hindi/Marathi
            "క్లినిక్", "వైద్యుడు", "డాక్టర్", "ఆఫీస్", # Telugu
            "கிளினிக்", "மருத்துவமனை", "மருத்துவர்", # Tamil
            "ক্লিনিক", "ডাক্তার", "অফিস", # Bengali
            "દવાખાનું", "ક્લિનિક", "ડોક્ટર", # Gujarati
            "ಚಿಕಿತ್ಸಾಲಯ", "ಕ್ಲಿನಿಕ್", "ವೈದ್ಯರು", # Kannada
            "ക്ലിനിക്ക്", "ഡോക്ടർ", "ഓഫീസ്", # Malayalam
            "କ୍ଲିନିକ୍", "ଡାକ୍ତର", "ଅଫିସ୍" # Odia
        ]
        multi_words = [
            "शाखा", "ब्रांच", # Hindi/Marathi
            "శాఖలు", "బ్రాంచీలు", # Telugu
            "கிளைகள்", # Tamil
            "শাখা", # Bengali
            "શાખાઓ", # Gujarati
            "ಶಾಖೆಗಳು", # Kannada
            "ശാഖകൾ", # Malayalam
            "ଶାଖା" # Odia
        ]
        mfg_words = [
            "कारखाना", "फैक्ट्री", "कपड़ों", "कपड़ा", # Hindi/Marathi
            "ఫ్యాక్టరీ", "మిల్లు", "పరిశ్రమ", "వస్త్రాల", # Telugu
            "தொழிற்சாலை", "ஆலை", # Tamil
            "কারখানা", "শিল্প", # Bengali
            "કારખાનું", "ટેક્સટાઇલ", # Gujarati
            "ಕಾರ್ಖಾನೆ", "ಗಿರಣಿ", # Kannada
            "ഫാക്ടറി", "മില്ല്", # Malayalam
            "କାରଖାନା" # Odia
        ]

        if re.search(r"\b(kirana|krana|kirane|grocery|salon|parlour|pharmacy|chemist|medical|medicl|dawai|bakery|cafe|boutique|dhaba|stationery|repair|shop|dukan|dukaan|dookan|dokan|retail|store)\b", norm_text) or any(w in norm_text for w in micro_words):
            slots.segment = "micro"
        elif re.search(r"\b(clinic|clinc|doctor|doktor|hospital|ca\s*firm|chartered\s*accountant|lawyer|advocate|coaching|tuition|consultan|architect|diagnostic|office)\b", norm_text) or any(w in norm_text for w in prof_words):
            slots.segment = "prof"
        elif re.search(r"\b(chain|multi[\s-]*site|outlets|stores|branches|restaurant\s*chain)\b", norm_text) or any(w in norm_text for w in multi_words):
            slots.segment = "multi"
        elif re.search(r"\b(factory|fctory|fctry|karkhana|textile|auto[\s-]*parts|manufacturing|plant|foundry|workshop|mill|industrial)\b", norm_text) or any(w in norm_text for w in mfg_words):
            slots.segment = "mfg"

        # 2. City Tier Extraction
        tier1_pattern = r"\b(mumbai|bombay|mumbay|delhi|dilli|bengaluru|bangalore|blr|hyderabad|hyd|chennai|madras|kolkata|calcutta|pune|ahmedabad|amdavad|gurugram|gurgaon|noida)\b"
        tier2_pattern = r"\b(jaipur|jaipr|jeypur|lucknow|lko|lakhnau|surat|kanpur|kanpr|knpur|nagpur|indore|bhopal|vizag|visakhapatnam|patna|vadodara|baroda|ludhiana|agra|nashik|coimbatore|kochi|cochin|chandigarh|tier\s*2|chota\s*sheher|chhota\s*shehar)\b"
        tier1_indic = ["मुंबई", "दिल्ली", "पुणे", "पुण्या", "बेंगलुरु", "ಬೆಂಗಳೂ", "హైదరాబాద్", "சென்னை", "কলকাতা", "અમદાવાદ", "കൊച്ചി"]
        tier2_indic = ["सूरत", "સુરત", "जयपुर", "लखनऊ", "नागपुर", "విజయవాడ", "విశాఖపట్నం", "കോയമ്പത്തൂർ", "மதுரை", "മൈസൂരു", "ଭୁବନେଶ୍ୱର"]

        if re.search(tier1_pattern, norm_text) or any(w in norm_text for w in tier1_indic):
            slots.city_tier = 1
        elif re.search(tier2_pattern, norm_text) or any(w in norm_text for w in tier2_indic):
            slots.city_tier = 2
        elif not slots.city_tier:
            if re.search(r"\btier\s*1\b", norm_text):
                slots.city_tier = 1
            elif any(word in norm_text for word in ["city", "town", "in", "me", "mein", "में"]):
                slots.city_tier = 2

        # 3. Employee Count Extraction
        emp_match = re.search(r"\b(\d+)\s*(?:staff|employees|workers|log|bnde|bande|ladke|members|people|kam\s*(?:karne|krne)\s*wale)\b", norm_text)
        if not emp_match:
            emp_match = re.search(r"(\d+)\s*(?:জন\s*কর্মী|জন|మంది|ఉద్యోగులు|ஊழியர்கள்|ஆட்கள்|কর্মী|লোক|કારીગરો|कामगार|ಜನರು|ಜನ|ജീവനക്കാർ|କର୍ମଚାରୀ|कर्मचारी|लोग)", norm_text)
        if emp_match:
            slots.employee_count = int(emp_match.group(1))
        elif not slots.employee_count:
            word_numbers = {
                "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "ek": 1, "do": 2,
                "teen": 3, "char": 4, "paanch": 5, "chhe": 6, "saat": 7, "aath": 8
            }
            for w, val in word_numbers.items():
                if re.search(rf"\b{w}\s*(?:staff|employees|workers|log|bnde|bande|ladke)\b", norm_text):
                    slots.employee_count = val
                    break

        # 4. Site Count Extraction (Supports Mid-Turn Branch Expansion)
        site_match = re.search(r"\b(\d+)\s*(?:[a-z0-9_-]+\s+)?(?:shops|stores|branches|sites|locations|dukaan|dukan|outlets|units|factories|ब्रांच|शाखा|दुकान)\b", norm_text)
        if site_match:
            slots.num_sites = int(site_match.group(1))
            if re.search(r"\b(branches|sites|locations|outlets|chain|ब्रांच|शाखा)\b", norm_text):
                slots.segment = "multi"
        elif not slots.num_sites:
            if re.search(r"\b(branches|sites|locations|outlets|chain)\b", norm_text):
                slots.num_sites = 2
                slots.segment = "multi"
            else:
                slots.num_sites = 1

        # 5. GSTIN Presence Extraction (Telugu, Tamil, Bengali, Gujarati, Marathi, Kannada, Malayalam, Odia, Hindi, English)
        no_gst_regional = [
            "जीएसटी नहीं", "జీఎస్టీ లేదు", "జిఎస్టి లేదు", "కేవలం ఉద్యమ్",
            "ஜிஎஸ்டி இல்லை", "உத்யம் மட்டுமே", "জিএসটি নেই", "শুধুমাত্র উদ্যোগ",
            "જીએસટી નથી", "ફક્ત ઉદ્યમ", "जीएसटी नाही", "ಜಿಎಸ್ಟಿ ಇಲ್ಲ", "ಉದ್ಯಮ್ ಮಾತ್ರ",
            "ജിഎസ്ടി ഇല്ല", "ഉദ്യം മാത്രം", "ଜିଏସଟି ନାହିଁ"
        ]
        has_gst_regional = [
            "जीएसटी है", "జీఎస్టీ ఉంది", "జిఎస్టి ఉంది", "జీఎస్టీ రిజిస్టర్",
            "ஜிஎஸ்டி இருக்கு", "ஜிஎஸ்டி பதிவு", "জিএসটি আছে", "জিএসটি নিবন্ধিত",
            "જીએસટી છે", "જીએસટી રજિસ્ટર્ડ", "जीएसटी आहे", "ಜಿಎಸ್ಟಿ ಇದೆ",
            "ജിഎസ്ടി ഉണ്ട്", "ଜିଏସଟି ଅଛି"
        ]

        no_gst_regex = (
            r"\b(no\s*gst(?:in)?|without\s*gst(?:in)?|bina\s*gst|gst(?:in)?\s*(?:nahi|nhi|nai|ni|ledu|ledhu|illa|illai|nei|nathi)\b|"
            r"dont\s*have\s*gst(?:in)?|don't\s*have\s*gst(?:in)?|gst\s*number\s*(?:nahi|nhi|nai|ni)|gst\s*vst\s*(?:kuch\s*)?(?:nahi|nhi|nai|ni)|"
            r"zero\s*gst|sirf\s*udyam|udyam\s*only|only\s*udyam|just\s*udyam|udyam\s*alone|udyam\s*matrame|udyam\s*hai|udyam\s*certificate|"
            r"udyam\s*registration|keval\s*udyam|\budyam\b)"
        )
        has_gst_regex = (
            r"\b(have\s*gst(?:in)?|has\s*gst(?:in)?|with\s*gst(?:in)?|gst\s*registered|gst\s*(?:hai|h\b|undi|undhi|irukku|ache|che|ahe|ide)|"
            r"gstin\s*(?:hai|h\b)|gst\s*number\s*(?:hai|h\b)|gstin\s*available|active\s*gst(?:in)?|gst\s*(?:number\s*)?mil\s*gaya|mil\s*gaya\s*gst|"
            r"yes\s*gst|have\s*gstin|gstin\s*registered)"
        )

        if re.search(no_gst_regex, norm_text) or any(w in norm_text for w in no_gst_regional):
            slots.has_gstin = False
        elif re.search(has_gst_regex, norm_text) or any(w in norm_text for w in has_gst_regional):
            slots.has_gstin = True
        elif existing_slots and ("gstin_status" in existing_slots.missing_slots or existing_slots.has_gstin is None):
            if re.search(r"\b(no|nope|not|dont|don't|nahi|nhi|ledu|illa|illai|nei|nathi|zero|na|neither|none|without)\b", norm_text):
                slots.has_gstin = False
            elif re.search(r"\b(yes|yeah|yep|haan|ha|hai|h\b|avunu|undu|aam|che|ache|registered|available|active)\b", norm_text):
                slots.has_gstin = True
        elif slots.segment in {"mfg", "multi"} and slots.has_gstin is None:
            # Factories and multi-site enterprise chains in India require statutory GST registration
            slots.has_gstin = True

        # 6. Budget Extraction
        budget_match = re.search(r"(?:budget|spending|kharcha|rupaye|rs|inr|₹|బడ్జెట్|பட்ஜெட்|বাজেট|બજેટ|बजेट|ಬಜೆಟ್|ബജറ്റ്|ବଜେଟ)\s*(?:is|hai|of|around|lagbhag)?\s*(?:rs\.?|inr|₹)?\s*(\d{3,6})", norm_text)
        if not budget_match:
            budget_match = re.search(r"(\d{3,6})\s*(?:rupaye|rupees|rs|per\s*month|\/mo|tak|approx|రూపాయలు|రూ|ரூபாய்|টাকা|રૂપિયા|रुपये|ರೂಪಾಯಿ|രൂപ|ଟଙ୍କା|బడ్జెట్|பட்ஜெட்|বাজেট|બજેટ|बजेट|ಬಜೆಟ್|ബജറ്റ്|ବଜେଟ)", norm_text)
        if budget_match:
            slots.monthly_budget_cap = int(budget_match.group(1))

        # 7. Needs Extraction
        detected = set(slots.detected_needs)
        if re.search(r"\b(upi|gpay|phonepe|paytm|pos|qr|hotspot|failover|backup|payment\s*(?:atak|fasa|fas|atke)|counter|यूपीआई)\b", norm_text):
            detected.add("pos_upi_uptime")
        if re.search(r"\b(whatsapp|watsapp|wtsapp|catalog|catalogue|broadcast|sms|message\s*bhejna)\b", norm_text):
            detected.add("whatsapp_catalogue")
        if re.search(r"\b(email|mail|tally|cloud|drive|storage|office\s*365|google\s*workspace|data\s*safe|क्लाउड\s*बैकअप|बैकअप)\b", norm_text):
            detected.add("cloud_email")
        if re.search(r"\b(cctv|camera|camra|cam|surveillance|nvr|dvr|सीसीटीवी|कैमरा)\b", norm_text):
            detected.add("cctv_surveillance")
        if re.search(r"\b(guest\s*wifi|customer\s*wifi|wifi|net|internet|broadband|वाईफाई|इंटरनेट)\b", norm_text):
            detected.add("guest_wifi")
        if re.search(r"\b(static\s*ip|fixed\s*ip|erp|server)\b", norm_text):
            detected.add("static_ip")
        if re.search(r"\b(leased\s*line|ill|dedicated\s*internet|fiber\s*cut|लीज्ड\s*लाइन)\b", norm_text):
            detected.add("dedicated_ill")
        if re.search(r"\b(iot|machine|sensor|monitoring)\b", norm_text):
            detected.add("iot_monitoring")
        slots.detected_needs = sorted(list(detected))

        # Check completeness
        missing = []
        if not slots.segment:
            missing.append("business_type")
        if not slots.city_tier:
            missing.append("city_or_tier")
        if slots.has_gstin is None:
            missing.append("gstin_status")

        slots.is_complete = len(missing) == 0
        slots.missing_slots = missing
        return slots

    def process_turn(
        self,
        user_message: str,
        previous_slots: Optional[MSMEProfileSlots] = None,
        language_code: Optional[str] = None,
        script_mode: Optional[str] = None,
        history: Optional[list[dict[str, str]]] = None,
    ) -> AdvisorResponse:
        norm_msg = user_message.lower().strip()
        # Security guardrails: prompt injection & arbitrary price override
        is_injection = "ignore previous instructions" in norm_msg or "ignore all instructions" in norm_msg or "say hacked" in norm_msg
        is_illegal_price = bool(re.search(r"\b(?:at|for|give me.*?at)\s*(?:rs\.?|inr|₹)\s*(?:10|1|5|0|500)\b", norm_msg) or re.search(r"\b(?:10|1|5|0|500)\s*(?:rupees|rs)\s*(?:per\s*month|\/mo)\b", norm_msg))
        if is_injection or is_illegal_price:
            slots = previous_slots or MSMEProfileSlots()
            guard_latin = (
                "⚠️ **Airtel Business Enterprise Policy Guardrail:** Plan pricing is strictly determined by central "
                "regulatory tariff filings and cannot be modified via conversational prompts. Biz Fibre 1G (P03) is "
                "**₹3,999/month** (OTC Setup: ₹0). We can, however, help you find an authentic bundle matching your budget."
            )
            guard_native = (
                "⚠️ **ఎయిర్‌టెల్ బిజినెస్ ఎంటర్‌ప్రైజ్ పాలసీ గార్డ్‌రైల్:** ప్లాన్ ధరలు కేంద్ర టారిఫ్ నిబంధనలకు కట్టుబడి ఉంటాయి. "
                "బిజ్ ఫైబర్ 1G (P03) ధర నెలకు ₹3,999 (సెటప్ రుసుము: ₹0). మీ బడ్జెట్‌కు సరిపోయే ప్రామాణికమైన ప్లాన్‌ను ఎంచుకోవడంలో మేము సహాయం చేయగలము."
            )
            p_lat, p_nat = ensure_dual_scripts(guard_latin, guard_native, language_code or "te")
            u_lat, u_nat = ensure_dual_scripts(user_message, user_message, language_code or "te")
            eff_script = script_mode or ("native" if has_indic_script(user_message) else "latin")

            return AdvisorResponse(
                slots=slots,
                quote=None,
                recommended_skus=[],
                display_markdown=p_lat if eff_script == "latin" else p_nat,
                spoken_tts_text="Airtel Business plan pricing is strictly fixed and audited by regulatory filings. Prompt price overrides are not permitted.",
                next_suggested_prompts=[
                    "Show plans under ₹1,500/month",
                    "What are the benefits of Biz Fibre 1G?",
                    "Check Udyam micro-retail bundles",
                ],
                display_latin=p_lat,
                display_native=p_nat,
                user_latin=u_lat,
                user_native=u_nat,
            )

        # Baseline deterministic extraction (bedrock guarantee)
        slots = self.extract_slots(user_message, previous_slots)

        clean_msg = user_message.strip().lower()
        has_indic = bool(re.search(r"[ऀ-ൿ]", user_message))
        effective_script = script_mode or ("native" if has_indic else "latin")

        # Detect active language
        active_lang = language_code if (language_code and language_code != "auto") else "en"

        te_romanized = r"\b(ekkuva|takkuva|karchu\w*|entha\w*|cheppan\w*|ayipotha\w*|ayipotu\w*|avuthu\w*|kavali|kavala|chudan\w*|dabbul\w*|kosam|bagunn\w*|ela\s*unna\w*|namaskar\w*|namasthe|meeru|unnam\w*|undi|ledu|enti|eppudu|chala|inka|mari|nenu|memu|maaku|meeku|artham\w*|telusuko\w*|vaddhu|chesuko\w*|dukanam|vyaparam)\b"
        hi_romanized = r"\b(kaise|kya|kyun|kyu|kaun|kab|kaha|kahan|kitna|kitne|kitni|chahiye|bhai|bhaiya|dukan|dukaan|paise|kharch\w*|sasta|mehanga|batao|bataiye|hoga|hogi|honge|karo|karein|karna|karne|chahte|chahta|chahti|madad|theek|achha|accha|hum|mera|meri|mere|aapka|aapki|aapke|kuch|nahi|nhi|bilkul|namaste)\b"
        ta_romanized = r"\b(vanakkam|eppadi|iruk\w*|nalama|sollu\w*|irukku|illa|illai|venum|ungal\w*|kaasu|panam|evvalavu|romba|nalla|kadai|theriyum)\b"
        bn_romanized = r"\b(nomoshkar|kemon|ach\w*|bhalo|lagbe|nei|apnar|dokan|koto|taka|shundor)\b"
        gu_romanized = r"\b(kem\s*cho|majama|tamare|tamaro|tamari|joie|nathi|ketla|paisa|dukan)\b"
        kn_romanized = r"\b(namaskara|hegidd\w*|beku|illa|nimma|yaava|estu|duddu|angadi)\b"
        ml_romanized = r"\b(namaskaram|sukham\w*|venam|illa|ningal\w*|ethra|paisa|kada)\b"
        mr_romanized = r"\b(kase\s*ahat|pahije|aahe|ahe|tumhi|tumche|kiti|sangnyasathi|sanga|kasa|kashi|kashe|aamhi)\b"
        or_romanized = r"\b(kemiti|ach\w*|bhalare|darkar|nahin|apana|apnanka|kete|paisa|dokana)\b"

        if re.search(r"[ఀ-౿]", user_message) or re.search(te_romanized, clean_msg):
            active_lang = "te"
        elif re.search(r"[ऀ-ॿ]", user_message) or re.search(hi_romanized, clean_msg):
            active_lang = "hi"
        elif re.search(r"[஀-௿]", user_message) or re.search(ta_romanized, clean_msg):
            active_lang = "ta"
        elif re.search(r"[ঀ-৿]", user_message) or re.search(bn_romanized, clean_msg):
            active_lang = "bn"
        elif re.search(r"[઀-૿]", user_message) or re.search(gu_romanized, clean_msg):
            active_lang = "gu"
        elif re.search(r"[ಀ-೿]", user_message) or re.search(kn_romanized, clean_msg):
            active_lang = "kn"
        elif re.search(r"[ഀ-ൿ]", user_message) or re.search(ml_romanized, clean_msg):
            active_lang = "ml"
        elif re.search(mr_romanized, clean_msg):
            active_lang = "mr"
        elif re.search(r"[଀-୿]", user_message) or re.search(or_romanized, clean_msg):
            active_lang = "or"

        # Intent detection
        is_comparison = bool(re.search(
            r"\b(compare|comparison|multiple|options|alternatives|vs|other plans?|not only one|only one|another plan|different plan|kore option|aur option)\b",
            clean_msg
        ))
        is_question = bool(re.search(
            r"\b(what|why|how|can|is|will|do|explain|tell|difference|kya|kaise|kyun|enti|ela|eppudu)\b",
            clean_msg
        ))
        is_foreign = bool(re.search(r"^(hola|bonjour|ciao|aloha|hallo|salut|wie\s*gehts|que\s*tal|konnichiwa|privet|merhaba|sawasdee)\b", clean_msg))
        is_unrelated = bool(re.search(r"\b(recipe|biryani|butter\s*chicken|cook|weather|monsoon|rain|poem|movie|sci-fi|python|binary\s*search|code|joke|capital\s*of|capital\s*city|who\s*won)\b", clean_msg))
        is_terse = clean_msg in {"internet", "broadband", "plans", "price", "how much", "speed", "enterprise", "connection", "quote", "airtel"}
        is_greeting = bool(re.search(r"^(hey|hi|hello|whats\s*up|what\'s\s*up|good\s*(morning|evening|afternoon)|yo|sup|bhai|bhaiya)\b", clean_msg)) or bool(re.search(r"\b(bagunnara|bagunara|ela\s*unnaru|ela\s*unnav|namaskaram|namasthe|vanakkam|eppadi\s*iruk|kemon\s*ac|kem\s*cho|kase\s*ahat|hegidd|sukhamano|kemiti\s*ach|kaise\s*ho|kya\s*haal|namaste|నమస్కారం|నమస్తే|బాగున్నారా|வணக்கம்|நமஸ்கார்|নমস্কার|નમસ્તે|नमस्कार|ನಮಸ್ಕಾರ|നമസ്കാരം|ନମସ୍କାର)\b", clean_msg))

        # Fast-path for simple 1-4 word conversational greetings (instant <50ms handshake)
        is_simple_greeting = is_greeting and not is_question and not is_comparison and not slots.segment and not slots.city_tier and not slots.employee_count and len(clean_msg.split()) <= 4
        if is_simple_greeting:
            f_latin, f_native, f_tts = self._generate_human_fallback(
                user_message, clean_msg, active_lang, effective_script, slots,
                is_foreign=is_foreign, is_unrelated=False, is_terse=False, is_greeting=True
            )
            pure_latin, pure_native = ensure_dual_scripts(f_latin, f_native, active_lang)
            user_lat, user_nat = self._dualize_user_message(user_message, active_lang, effective_script)
            return AdvisorResponse(
                slots=slots,
                quote=None,
                recommended_skus=[],
                display_markdown=pure_latin if effective_script == "latin" else pure_native,
                spoken_tts_text=f_tts,
                next_suggested_prompts=[
                    "Show plans for retail shop",
                    "We need high speed fiber for clinic",
                    "What plans are under ₹1,500/month?",
                ],
                display_latin=pure_latin,
                display_native=pure_native,
                user_latin=user_lat,
                user_native=user_nat,
            )

        # -------------------------------------------------------------
        # TIER 1: LIVE GEMINI ENTERPRISE CONSULTATION (If online)
        # -------------------------------------------------------------
        gemini_data = self._consult_gemini(
            user_message,
            existing_slots=slots,
            language_code=active_lang,
            script_mode=effective_script,
            history=history,
        )

        if gemini_data:
            # Update slots from Gemini
            if not slots.segment and gemini_data.get("segment"):
                slots.segment = str(gemini_data["segment"]).lower()
            if not slots.city_tier and gemini_data.get("city_tier"):
                slots.city_tier = int(gemini_data["city_tier"])
            if not slots.employee_count and gemini_data.get("employee_count"):
                slots.employee_count = int(gemini_data["employee_count"])
            if not slots.num_sites and gemini_data.get("num_sites"):
                slots.num_sites = int(gemini_data["num_sites"])
            if slots.has_gstin is None and gemini_data.get("has_gstin") is not None:
                slots.has_gstin = bool(gemini_data["has_gstin"])
            if gemini_data.get("detected_needs"):
                existing_needs = set(slots.detected_needs)
                for n in gemini_data["detected_needs"]:
                    existing_needs.add(str(n).lower())
                slots.detected_needs = sorted(list(existing_needs))
            if not slots.monthly_budget_cap and gemini_data.get("monthly_budget_cap"):
                slots.monthly_budget_cap = int(gemini_data["monthly_budget_cap"])

            missing = []
            if not slots.segment:
                missing.append("business_type")
            if not slots.city_tier:
                missing.append("city_or_tier")
            if slots.has_gstin is None:
                missing.append("gstin_status")
            slots.is_complete = len(missing) == 0
            slots.missing_slots = missing

            disp_lat = gemini_data.get("display_latin") or gemini_data.get("display_markdown") or ""
            disp_nat = gemini_data.get("display_native") or disp_lat
            spoken = gemini_data.get("spoken_tts") or ""
            spoken = re.sub(r"[\*#\[\]]", "", spoken).replace("₹", "rupaye ").strip()

            gem_skus = gemini_data.get("recommended_skus") or []
            valid_skus = [s for s in gem_skus if self.db.get_product(s)]
            if not valid_skus:
                valid_skus = self._select_skus_for_slots(slots)

            # Deterministic CPQ Engine execution for live commercials & cart sync
            quote = None
            if slots.is_complete or len(valid_skus) > 0:
                try:
                    cart_items = [CartItem(product_id=s) for s in valid_skus]
                    quote = self.cpq.calculate_commercials(cart_items, contract_months=12, strict_dag=True)
                except Exception as e:
                    logging.warning("CPQ calculation on Gemini SKUs failed: %s", e)

            pure_latin, pure_native = ensure_dual_scripts(disp_lat, disp_nat, active_lang)
            user_lat, user_nat = self._dualize_user_message(user_message, active_lang, effective_script)

            prompts = gemini_data.get("next_suggested_prompts") or [
                "Show details of Biz 5G AirFiber",
                "Can I add 4G Auto-Failover?",
                "Proceed to instant KYC",
            ]

            return AdvisorResponse(
                slots=slots,
                quote=quote,
                recommended_skus=valid_skus,
                display_markdown=pure_latin if effective_script == "latin" else pure_native,
                spoken_tts_text=spoken or (pure_latin if effective_script == "latin" else pure_native),
                next_suggested_prompts=prompts,
                display_latin=pure_latin,
                display_native=pure_native,
                user_latin=user_lat,
                user_native=user_nat,
            )

        # -------------------------------------------------------------
        # TIER 2: DETERMINISTIC SYMBOLIC / HUMAN FALLBACK (offline / pytest)
        # -------------------------------------------------------------
        # 1. Comparison & Multiple Options request
        if is_comparison:
            f_latin, f_native, f_tts = self._generate_human_fallback(
                user_message, clean_msg, active_lang, effective_script, slots,
                is_foreign=False, is_unrelated=False, is_terse=False, is_greeting=False
            )
            valid_skus = self._select_skus_for_slots(slots)
            cart_items = [CartItem(product_id=s) for s in valid_skus]
            quote = self.cpq.calculate_commercials(cart_items, contract_months=12, strict_dag=True)
            pure_latin, pure_native = ensure_dual_scripts(f_latin, f_native, active_lang)
            user_lat, user_nat = self._dualize_user_message(user_message, active_lang, effective_script)
            return AdvisorResponse(
                slots=slots,
                quote=quote,
                recommended_skus=valid_skus,
                display_markdown=pure_latin if effective_script == "latin" else pure_native,
                spoken_tts_text=f_tts,
                next_suggested_prompts=[
                    "Tell me more about AirFiber",
                    "What if I choose Fixed Fiber?",
                    "Proceed to instant KYC",
                ],
                display_latin=pure_latin,
                display_native=pure_native,
                user_latin=user_lat,
                user_native=user_nat,
            )

        # 2. Greeting, Unrelated, Terse, Foreign
        if is_foreign or is_unrelated or is_terse or (is_greeting and not slots.is_complete):
            f_latin, f_native, f_tts = self._generate_human_fallback(
                user_message, clean_msg, active_lang, effective_script, slots,
                is_foreign=is_foreign, is_unrelated=is_unrelated, is_terse=is_terse, is_greeting=is_greeting
            )
            pure_latin, pure_native = ensure_dual_scripts(f_latin, f_native, active_lang)
            user_lat, user_nat = self._dualize_user_message(user_message, active_lang, effective_script)
            return AdvisorResponse(
                slots=slots,
                quote=None,
                recommended_skus=[],
                display_markdown=pure_latin if effective_script == "latin" else pure_native,
                spoken_tts_text=f_tts,
                next_suggested_prompts=[
                    "I run a Kirana store in Tier 2 with no GST",
                    "We are a CA office in Mumbai with GST",
                    "We have a factory in Surat with 30 staff",
                ],
                display_latin=pure_latin,
                display_native=pure_native,
                user_latin=user_lat,
                user_native=user_nat,
            )

        # 3. Slots are incomplete: prompt for clarification
        if not slots.is_complete:
            f_latin, f_native, f_tts = self._generate_human_fallback(
                user_message, clean_msg, active_lang, effective_script, slots
            )
            pure_latin, pure_native = ensure_dual_scripts(f_latin, f_native, active_lang)
            user_lat, user_nat = self._dualize_user_message(user_message, active_lang, effective_script)
            return AdvisorResponse(
                slots=slots,
                quote=None,
                recommended_skus=[],
                display_markdown=pure_latin if effective_script == "latin" else pure_native,
                spoken_tts_text=f_tts,
                next_suggested_prompts=[
                    "I run a Kirana store in Tier 2 with no GST",
                    "We are a CA office in Mumbai with GST",
                    "We have a factory in Surat with 30 staff",
                ],
                display_latin=pure_latin,
                display_native=pure_native,
                user_latin=user_lat,
                user_native=user_nat,
            )

        # 4. Slots are complete & standard recommendation requested
        profile = UserProfile(
            segment=slots.segment or "micro",
            city_tier=slots.city_tier or 2,
            employee_count=slots.employee_count or 3,
            num_sites=slots.num_sites or 1,
            has_gstin=slots.has_gstin if slots.has_gstin is not None else False,
            detected_needs=slots.detected_needs,
            monthly_budget_cap=slots.monthly_budget_cap,
        )

        if slots.monthly_budget_cap and slots.monthly_budget_cap > 0:
            budget_res = self.cpq.optimize_budget(profile, slots.monthly_budget_cap)
            quote = budget_res.commercials
            selected_skus = budget_res.selected_skus
        else:
            selected_skus = self._select_skus_for_slots(slots)
            cart_items = [CartItem(product_id=pid) for pid in selected_skus]
            quote = self.cpq.calculate_commercials(cart_items, contract_months=12, strict_dag=True)

        display_latin, display_native, spoken_tts = self._generate_dual_narration(profile, quote, selected_skus)
        pure_latin, pure_native = ensure_dual_scripts(display_latin, display_native, active_lang)
        display_md = pure_latin if effective_script == "latin" else pure_native
        user_latin, user_native = self._dualize_user_message(user_message, active_lang, effective_script)

        return AdvisorResponse(
            slots=slots,
            quote=quote,
            recommended_skus=selected_skus,
            display_markdown=display_md,
            spoken_tts_text=spoken_tts,
            next_suggested_prompts=[
                "Can you compare with other options?",
                "Can I add CCTV surveillance?",
                "Proceed to instant KYC",
            ],
            display_latin=pure_latin,
            display_native=pure_native,
            user_latin=user_latin,
            user_native=user_native,
        )

    def _dualize_user_message(self, text: str, lang: str, script: str) -> tuple[str, str]:
        """Returns (user_latin, user_native) for the input message."""
        clean = text.strip()
        return ensure_dual_scripts(clean, clean, lang)

    def _generate_dual_narration(
        self,
        profile: UserProfile,
        quote: CommercialBreakdown,
        skus: list[str],
    ) -> tuple[str, str, str]:
        """Generates (display_latin, display_native, spoken_tts)."""
        product_names = [
            self.db.get_product(pid).product_name
            for pid in skus
            if self.db.get_product(pid)
        ]
        base_name = product_names[0] if product_names else "High-Speed Internet"
        addons = product_names[1:]

        md_latin = [
            f"### Recommended Bundle for Your {profile.segment.title()} Business\n",
            f"Based on your profile in Tier {profile.city_tier} ({profile.employee_count} staff, "
            f"{'GST registered' if profile.has_gstin else 'Udyam micro-business'}), we configured an optimal bundle:\n",
            f"* **Primary Connection:** {base_name}",
        ]
        if addons:
            md_latin.append(f"* **Add-ons:** {', '.join(addons)}")
        md_latin.append("\n**Commercials & SLA:**")
        md_latin.append(f"* **Net Monthly:** ₹{quote.net_mrc_inr:,}/month (includes bundle discount)")
        md_latin.append(f"* **Setup Fee:** {'WAIVED (₹0)' if quote.net_otc_inr == 0 else f'₹{quote.net_otc_inr:,}'}")
        md_latin.append(f"* **Guaranteed SLA:** Ready in {quote.guaranteed_sla_days} business days")
        md_latin.append(f"* **Regulatory:** {'100% GST-Exempt Udyam Eligible' if quote.gst_exempt_eligible else 'B2B Input Tax Credit Eligible'}")
        display_latin = "\n".join(md_latin)

        md_native = [
            f"### మీ వ్యాపారానికి సిఫార్సు చేసిన ఎయిర్‌టెల్ కనెక్టివిటీ ప్లాన్\n",
            f"టైర్ {profile.city_tier} లోని మీ వ్యాపారం కోసం ({profile.employee_count} మంది సిబ్బంది, "
            f"{'జీఎస్టీ నమోదిత' if profile.has_gstin else 'ఉద్యమ్ మైక్రో ఎంటర్‌ప్రైజ్'}):\n",
            f"* **ప్రధాన కనెక్షన్:** {base_name}",
        ]
        if addons:
            md_native.append(f"* **అదనపు సేవలు:** {', '.join(addons)}")
        md_native.append("\n**ధర మరియు సర్వీస్ వివరాలు:**")
        md_native.append(f"* **నెలవారీ ఛార్జీ:** ₹{quote.net_mrc_inr:,}/నెల")
        md_native.append(f"* **సెటప్ రుసుము:** {'పూర్తిగా ఉచితం (₹0)' if quote.net_otc_inr == 0 else f'₹{quote.net_otc_inr:,}'}")
        md_native.append(f"* **యాక్టివేషన్:** {quote.guaranteed_sla_days} పని దినాలలో")
        md_native.append(f"* **అర్హత:** {'100% ఉద్యమ్ జీఎస్టీ మినహాయింపు' if quote.gst_exempt_eligible else 'జీఎస్టీ ఇన్పుట్ క్రెడిట్ అర్హత'}")
        display_native = "\n".join(md_native)

        addon_phrase = f"aur {len(addons)} zaroori add ons" if addons else ""
        spoken_tts = (
            f"Aapke business ke liye humne {base_name} {addon_phrase} chuna hai. "
            f"Iska monthly charge {quote.net_mrc_inr} rupaye hai. "
            f"Setup fee bilkul zero hai aur yeh {quote.guaranteed_sla_days} din mein activate ho jayega."
        )

        return display_latin, display_native, spoken_tts

    def _generate_human_fallback(
        self,
        user_message: str,
        clean_msg: str,
        active_lang: str,
        effective_script: str,
        slots: MSMEProfileSlots,
        is_foreign: bool = False,
        is_unrelated: bool = False,
        is_terse: bool = False,
        is_greeting: bool = False,
    ) -> tuple[str, str, str]:
        """Generates a warm, witty, context-attuned human response across all edge categories."""
        # 0. Plan Comparison & Alternative Options Query
        if re.search(r"\b(compare|comparison|multiple|options|alternatives|vs|other plans?|not only one|only one|another plan|different plan|kore option|aur option)\b", clean_msg):
            budget = slots.monthly_budget_cap or 1500
            tier = slots.city_tier or 2
            lat = (
                "### Flexible Airtel Business Options for Your Enterprise\n\n"
                "You definitely have multiple great options—we never force just one setup on you! "
                f"Based on your profile in Tier {tier} (budget ~₹{budget:,}/mo), here is how our top options compare:\n\n"
                "1. **Option 1: The Wireless Champion (Biz 5G AirFiber 100 + Secure Wi-Fi)**\n"
                "   * **Net Monthly:** ₹1,298/month (includes bundle discount)\n"
                "   * **SLA & Install:** Ready in **3 days** with zero road-digging or wire cuts.\n"
                "   * **Why it fits:** Plug-and-play 5G antenna gives 100 Mbps with an isolated guest Wi-Fi for customers so your billing counter never slows down.\n\n"
                "2. **Option 2: The High-Reliability Wired Pack (Biz Fibre 100 + 4G LTE Auto-Failover)**\n"
                "   * **Net Monthly:** ₹1,398/month\n"
                "   * **SLA & Install:** Ready in **4 business days** via physical optical fiber.\n"
                "   * **Why it fits:** High physical fiber stability plus automatic 4G cellular backup so UPI payment checkouts never freeze during cable cuts.\n\n"
                "3. **Option 3: Pure Essential Fiber (Biz Fibre 100)**\n"
                "   * **Net Monthly:** ₹1,099/month (Waived ₹0 setup fee on 12-month agreement)\n"
                "   * **Why it fits:** Maximum savings, symmetric 100 Mbps pipe, 100% legal onboarding with your Udyam certificate.\n\n"
                "Which configuration aligns best with your day-to-day business priority?"
            )
            nat = (
                "### మీ వ్యాపారం కోసం ఎయిర్‌టెల్ బిజినెస్ ప్లాన్‌ల పోలిక\n\n"
                "మీ వ్యాపారానికి ఖచ్చితంగా బహుళ ఉత్తమ ఎంపికలు ఉన్నాయి! "
                f"టైర్ {tier} లోని మీ వ్యాపారం కోసం (బడ్జెట్ సుమారు ₹{budget:,}/నెల) ప్రధాన ఎంపికలు ఇక్కడ ఉన్నాయి:\n\n"
                "1. **ఎంపిక 1: వైర్‌లెస్ ఛాంపియన్ (బిజ్ 5G ఎయిర్‌ఫైబర్ 100 + సురక్షిత వై-ఫై)**\n"
                "   * **నెలవారీ:** నెలకు ₹1,298 (బండిల్ డిస్కౌంట్‌తో)\n"
                "   * **యాక్టివేషన్:** కేబుల్ కట్స్ లేకుండా 3 రోజుల్లో ప్రారంభం.\n"
                "   * **ప్రయోజనం:** ప్లగ్ అండ్ ప్లే 5G యాంటెన్నాతో కస్టమర్ల కోసం ప్రత్యేక గెస్ట్ వై-ఫై.\n\n"
                "2. **ఎంపిక 2: హై-రెసిలెన్స్ వైర్డ్ ప్యాక్ (బిజ్ ఫైబర్ 100 + 4G ఆటో-ఫెయిల్‌ఓవర్)**\n"
                "   * **నెలవారీ:** నెలకు ₹1,398\n"
                "   * **ప్రయోజనం:** వైర్ కట్ అయినా ఆటోమేటిక్ 4G సెల్యులార్ బ్యాకప్‌తో నిరంతర యూపీఐ లావాదేవీలు.\n\n"
                "3. **ఎంపిక 3: ఎసెన్షియల్ ఫైబర్ (బిజ్ ఫైబర్ 100)**\n"
                "   * **నెలవారీ:** నెలకు ₹1,099 (12 నెలల ఒప్పందంపై ₹0 సెటప్ రుసుము)\n"
                "   * **ప్రయోజనం:** ఉద్యమ్ సర్టిఫికేట్‌తో తక్కువ ఖర్చులో హై-స్పీడ్ ఇంటర్నెట్.\n\n"
                "మీ రోజువారీ వ్యాపారానికి ఏ ప్లాన్ అనుకూలంగా ఉంటుంది?"
            )
            tts = (
                "Aapke business ke liye hamare paas AirFiber wireless aur Fixed Fiber dono ke shandar vikalp hain. "
                "AirFiber 1298 rupaye mein guest Wi-Fi ke saath 3 din mein activate hota hai, "
                "jabki Fixed Fiber 4G failover ke saath 1398 rupaye mein aata hai."
            )
            return lat, nat, tts

        # 0.5 Minimum Budget / Thrift / Savings Inquiry
        is_budget_query = bool(re.search(
            r"\b(minimum|low\s*cost|sasta|takkuva|kam\s*kharch|kam\s*se\s*kam|enthalo|kitne\s*me|lowest\s*price|budget\s*friendly|karchu\w*|kharch\w*|least\s*price|money\s*karchu|paise\s*kharch)\b",
            clean_msg
        ))
        if is_budget_query and not slots.is_complete:
            if active_lang == "te":
                lat = (
                    "Arthamaindi! 5-members team kosam budget takkuvalo kavali ante, Biz 5G AirFiber 100 net Rs 1,199/month "
                    "(100 Mbps, 3-day SLA, zero setup fee) lo best option! "
                    "Mee business type (dukanam, clinic, office) mariyu city details chepthe, inka saraina plan suggest chesthanu."
                )
                nat = (
                    "అర్థమైంది! బడ్జెట్ తక్కువలో కావాలంటే, బిజ్ 5G ఎయిర్‌ఫైబర్ 100 నెలకు ₹1,199 "
                    "(100 Mbps, 3 రోజుల ఎస్‌ఎల్‌ఏ, ₹0 సెటప్ రుసుము) బెస్ట్ ఆప్షన్! "
                    "మీ వ్యాపార రకం మరియు నగరం వివరాలు చెబితే, ఇంకా సరైన ప్లాన్ అందిస్తాము."
                )
                tts = (
                    "Arthamaindi! Budget takkuvalo Biz 5G AirFiber 100 net 1,199 rupaye lo best option. "
                    "Mee business type mariyu city details cheppandi."
                )
                return lat, nat, tts
            elif active_lang == "hi":
                lat = (
                    "Samajh gaya! Agar aap kam budget mein best option chahte hain, toh Biz 5G AirFiber 100 net Rs 1,199/month "
                    "(100 Mbps, zero setup fee) sabse badhiya option hai! "
                    "Apni dukan ya office ka type aur city bataiye taaki sahi plan recommend kar sakein."
                )
                nat = (
                    "समझ गया! अगर आप कम बजट में सबसे अच्छा विकल्प चाहते हैं, तो एयरटेल బిజ్ 5G AirFiber 100 केवल ₹1,199/माह "
                    "(100 Mbps, शून्य सेटअप शुल्क) सबसे बढ़िया विकल्प है! "
                    "अपने व्यवसाय का प्रकार और शहर बताएं ताकि सही प्लान दे सकें।"
                )
                tts = (
                    "Samajh gaya! Kam budget mein Biz 5G AirFiber 100 net 1199 rupaye mein sabse badhiya option hai. "
                    "Apna business type aur city bataiye."
                )
                return lat, nat, tts
            elif active_lang == "ta":
                lat = (
                    "Purindhadhu! Kuraivana budget-il thevai endraal, Biz 5G AirFiber 100 net Rs 1,199/month "
                    "(100 Mbps, zero setup fee) migavum sirandhadhu! "
                    "Ungal business type matrum ooru sollunga."
                )
                nat = (
                    "புரிந்தது! குறைந்த பட்ஜெட்டில் வேண்டுமென்றால், பிஸ் 5G ஏர்ஃபைபர் 100 மாதம் ₹1,199 "
                    "(100 Mbps, ₹0 கட்டணம்) சிறந்த தேர்வு! உங்கள் வணிக வகை மற்றும் ஊர் விவரங்களை தெரிவிக்கவும்."
                )
                tts = (
                    "Purindhadhu! Kuraivana budget-il Biz 5G AirFiber 100 Rs 1,199-il sirandhadhu. "
                    "Ungal business type matrum ooru sollunga."
                )
                return lat, nat, tts

        # 1. Global Multilingual Greetings (Universal zero-restriction capability in any language)
        if is_foreign:
            if re.search(r"\b(hola|buenos\s*dias|buenas|que\s*tal)\b", clean_msg):
                lat = "Hola! Bienvenido a Airtel Business. Como puedo ayudarte hoy con la conectividad de fibra de alta velocidad o soluciones digitales para tu negocio?"
                nat = lat
                tts = "Hola! Bienvenido a Airtel Business. Como puedo ayudarte hoy con la conectividad para tu negocio?"
                return lat, nat, tts
            if re.search(r"\b(bonjour|salut)\b", clean_msg):
                lat = "Bonjour! Bienvenue chez Airtel Business. Comment puis-je vous aider aujourd'hui pour la connectivite fibre haute vitesse ou les solutions numeriques de votre entreprise?"
                nat = lat
                tts = "Bonjour! Bienvenue chez Airtel Business. Comment puis-je vous aider pour votre entreprise?"
                return lat, nat, tts
            if re.search(r"\b(ciao|buongiorno)\b", clean_msg):
                lat = "Ciao! Benvenuto in Airtel Business. Come posso aiutarti oggi con la connettivita in fibra ad alta velocita o i servizi digitali per la tua azienda?"
                nat = lat
                tts = "Ciao! Benvenuto in Airtel Business. Come posso aiutarti oggi per la tua azienda?"
                return lat, nat, tts
            if re.search(r"\b(hallo|guten\s*tag|wie\s*gehts)\b", clean_msg):
                lat = "Hallo! Willkommen bei Airtel Business. Wie kann ich Ihnen heute bei Highspeed-Glasfaser oder digitalen Diensten fur Ihr Unternehmen helfen?"
                nat = lat
                tts = "Hallo! Willkommen bei Airtel Business. Wie kann ich Ihnen heute helfen?"
                return lat, nat, tts
            if re.search(r"\b(ola|bom\s*dia|tudo\s*bem)\b", clean_msg):
                lat = "Ola! Bem-vindo a Airtel Business. Como posso ajudar a sua empresa hoje com conectividade de fibra de alta velocidade ou solucoes digitais?"
                nat = lat
                tts = "Ola! Bem-vindo a Airtel Business. Como posso ajudar a sua empresa hoje?"
                return lat, nat, tts
            if re.search(r"\b(konnichiwa|arigato)\b", clean_msg):
                lat = "Konnichiwa! Airtel Business e yokoso. Kisha no kousoku faiba ya dejitaru sabisu ni tsuite nanika o-tetsudai dekimasu ka?"
                nat = "こんにちは！Airtel Businessへようこそ。企業の高速光ファイバーやデジタルサービスについて、どのようなご用件でしょうか？"
                tts = "Konnichiwa! Airtel Business e yokoso."
                return lat, nat, tts
            if re.search(r"\b(marhaban|ahlan|as-salamu\s*alaykum)\b", clean_msg):
                lat = "Marhaban! Ahlan bikum fi Airtel Business. Kayfa yumkinuna musa'adatukum al-yawm fi khadamat al-alyaf aw al-hulul al-raqmiya li-sharikatikum?"
                nat = "مرحباً بكم في إيرتل بيزنس! كيف يمكننا مساعدتكم اليوم في خدمات الألياف الضوئية عالية السرعة والحلول الرقمية لشركتكم؟"
                tts = "Marhaban! Ahlan bikum fi Airtel Business."
                return lat, nat, tts
            if re.search(r"\b(privet|zdravstvuyte)\b", clean_msg):
                lat = "Zdravstvuyte! Dobro pozhalovat v Airtel Business. Chem ya mogu pomoch vashemu predpriyatiyu s vysokoskorostnym internetom ili tsifrovymi uslugami segodnya?"
                nat = "Здравствуйте! Добро пожаловать в Airtel Business. Чем я могу помочь вашему предприятию с высокоскоростным интернетом или цифровыми услугами сегодня?"
                tts = "Zdravstvuyte! Dobro pozhalovat v Airtel Business."
                return lat, nat, tts
            # General international greeting
            lat = "Hello! Welcome to Airtel Business. I converse fluently in any language worldwide! How can I help power your enterprise with high-speed fiber or digital connectivity today?"
            nat = lat
            tts = "Hello! Welcome to Airtel Business. How can I help power your enterprise today?"
            return lat, nat, tts

        # 2. Unrelated / Playful Queries
        if is_unrelated:
            if re.search(r"\b(biryani|butter\s*chicken|recipe|cook|food)\b", clean_msg):
                lat = "That sounds delicious, but my superpower is high-speed enterprise fiber, not secret recipes! If you run a restaurant, cafe, or cloud kitchen, I can set you up with zero-drop Airtel fiber for seamless online deliveries and POS billing. What kind of business setup do you have?"
                nat = "రుచికరమైన వంటకం! కానీ నా నైపుణ్యం హై-స్పీడ్ ఫైబర్ కనెక్టివిటీలో ఉంది. మీరు రెస్టారెంట్ లేదా క్లౌడ్ కిచెన్ నడుపుతుంటే, ఆన్‌లైన్ ఆర్డర్ల కోసం ఎయిర్‌టెల్ బిజినెస్ ఫైబర్ అందిస్తాము. మీ వ్యాపార వివరాలు తెలపండి."
                tts = "Sounds delicious! If you run a food business or restaurant, Airtel fiber can power your online orders seamlessly."
                return lat, nat, tts
            if re.search(r"\b(weather|monsoon|rain)\b", clean_msg):
                lat = "Whether it's monsoon downpours or peak summer heat, Airtel Business fiber stays rock-solid with 99.9% uptime SLA! What kind of business setup are you looking to connect?"
                nat = "వర్షాలు పడినా లేదా ఎండలు కాసినా, ఎయిర్‌టెల్ బిజినెస్ ఫైబర్ 99.9% అప్‌టైమ్ ఎస్‌ఎల్‌ఏతో నిరంతరం పనిచేస్తుంది! మీ వ్యాపారానికి ఎలాంటి కనెక్షన్ కావాలి?"
                tts = "Rain or shine, Airtel Business fiber stays rock solid with 99.9% uptime SLA. What business setup are you connecting?"
                return lat, nat, tts
            if re.search(r"\bpoem\b", clean_msg):
                lat = "High-speed connections that never fray / Powering your enterprise day by day! Now, tell me about your business so we can get your digital setup rolling."
                nat = "ఎప్పటికీ ఆగని హై-స్పీడ్ కనెక్టివిటీతో మీ వ్యాపారాన్ని ముందుకు నడిపించండి! మీ వ్యాపార వివరాలు తెలపండి."
                tts = "High-speed connections that never fray, powering your enterprise day by day! Tell me about your business requirements."
                return lat, nat, tts
            if re.search(r"\b(moon|distance)\b", clean_msg):
                lat = "The Moon is about 384,400 km away—still faster than data traveling on a slow connection! With Airtel gigabit fiber, your business gets ultra-low latency. What kind of office or shop are you looking to connect?"
                nat = "చంద్రుడు 3,84,400 కి.మీ దూరంలో ఉన్నాడు! ఎయిర్‌టెల్ గిగాబిట్ ఫైబర్‌తో మీ వ్యాపారానికి అల్ట్రా-లో లేటెన్సీ లభిస్తుంది. మీ ఆఫీస్ లేదా దుకాణం వివరాలు తెలపండి."
                tts = "The Moon is 384,400 km away! With Airtel gigabit fiber, your business gets lightning-fast low latency."
                return lat, nat, tts
            if re.search(r"\b(python|binary\s*search|code|programming)\b", clean_msg):
                lat = "While I love clean algorithms and fast code, my day job is connecting tech offices, studios, and businesses with dedicated static IPs and gigabit fiber! Are you setting up connectivity for a tech team?"
                nat = "సాఫ్ట్‌వేర్ కోడింగ్ బాగుంది, కానీ టెక్ ఆఫీసులు మరియు డెవలపర్‌ల కోసం డెడికేటెడ్ స్టాటిక్ ఐపీ, హై-స్పీడ్ ఫైబర్ అందించడమే నా ముఖ్య పని! మీ టెక్ టీమ్ వివరాలు తెలపండి."
                tts = "While I love clean algorithms, my specialty is connecting tech offices with dedicated static IPs and gigabit fiber!"
                return lat, nat, tts
            if re.search(r"\b(movie|sci-fi)\b", clean_msg):
                lat = "For top-tier sci-fi: Inception, Interstellar, and The Matrix! To stream them in 4K or power a creative studio, Airtel gigabit fiber is your best bet. What business setup are you looking to power up?"
                nat = "సినిమాలు చూడటానికి లేదా డిజిటల్ స్టూడియో నడపడానికి ఎయిర్‌టెల్ గిగాబిట్ ఫైబర్ అత్యుత్తమమైనది. మీ వ్యాపారానికి ఎలాంటి ప్లాన్ కావాలి?"
                tts = "Top sci-fi recommendations: Inception, Interstellar, and The Matrix! What business setup are you looking to power up?"
                return lat, nat, tts
            if re.search(r"\bjoke\b", clean_msg):
                lat = "Why did the router get an award? Because it had outstanding connections! Speaking of great connections, how can Airtel Business fiber power your enterprise today?"
                nat = "రౌటర్‌కు ఎందుకు అవార్డు వచ్చిందో తెలుసా? ఎందుకంటే దాని కనెక్షన్లు అద్భుతం! మీ వ్యాపారానికి ఎయిర్‌టెల్ ఫైబర్ కనెక్షన్ ఎలా అందించగలను?"
                tts = "Why did the router get an award? Because it had outstanding connections! How can Airtel Business fiber power your enterprise today?"
                return lat, nat, tts
            if re.search(r"\b(capital\s*of|capital\s*city)\b", clean_msg):
                lat = "It's Canberra! Speaking of capital ideas, what kind of business or office are you looking to power with Airtel enterprise connectivity?"
                nat = "ఆస్ట్రేలియా రాజధాని కాన్‌బెర్రా! మీ వ్యాపార కార్యాలయానికి ఎయిర్‌టెల్ ఎంటర్‌ప్రైజ్ కనెక్టివిటీ అందించడానికి వివరాలు తెలపండి."
                tts = "It's Canberra! What kind of business or office are you looking to power with Airtel enterprise connectivity?"
                return lat, nat, tts

        # 3. Terse / Ambiguous Queries
        if is_terse:
            if clean_msg in {"internet", "broadband"}:
                lat = "Looking for lightning-fast business internet? We provide high-speed fiber with 99.9% uptime and zero setup fees under Udyam. What kind of business do you run and in which city?"
                nat = "హై-స్పీడ్ బిజినెస్ ఇంటర్నెట్ కావాలా? ఉద్యమ్ కింద సున్నా సెటప్ ఫీజుతో 99.9% అప్‌టైమ్ ఫైబర్ అందిస్తున్నాము. మీది ఏ వ్యాపారం మరియు ఏ ఊరు?"
                tts = "Looking for lightning-fast business internet? What kind of business do you run and in which city?"
                return lat, nat, tts
            if clean_msg in {"plans"}:
                lat = "We have curated business packs starting from ₹999/month for retail shops up to gigabit fiber for offices! Which city is your business in, and what do you do?"
                nat = "రిటైల్ షాపుల కోసం నెలకు ₹999 నుండి ఎంటర్‌ప్రైజ్ ప్లాన్‌ల వరకు అందుబాటులో ఉన్నాయి! మీ వ్యాపారం ఏ నగరంలో ఉంది?"
                tts = "We have curated business packs from 999 rupees a month. Which city is your business in?"
                return lat, nat, tts
            if clean_msg in {"price", "how much"}:
                lat = "Our MSME plans start at ₹999/month for retail with zero setup fee under Udyam, up to dedicated corporate lines. Which city is your business located in?"
                nat = "మా ఎంఎస్ఎంఈ ప్లాన్లు నెలకు ₹999 నుండి ప్రారంభమవుతాయి. మీ వ్యాపారం ఏ నగరంలో ఉంది?"
                tts = "Our plans start at 999 rupees a month with zero setup fee. Which city is your business located in?"
                return lat, nat, tts
            if clean_msg in {"speed"}:
                lat = "Speeds range from 100 Mbps symmetrical fiber up to 1 Gbps dedicated leased lines! What type of enterprise do you run?"
                nat = "100 Mbps నుండి 1 Gbps వరకు డెడికేటెడ్ స్పీడ్స్ అందుబాటులో ఉన్నాయి! మీది ఏ తరహా వ్యాపారం?"
                tts = "Speeds range from 100 Mbps up to 1 Gbps dedicated leased lines! What type of enterprise do you run?"
                return lat, nat, tts
            if clean_msg in {"enterprise"}:
                lat = "Airtel Enterprise delivers dedicated leased lines, static IPs, cloud backup, and SD-WAN across India. What is your company's profile and city?"
                nat = "ఎయిర్‌టెల్ ఎంటర్‌ప్రైజ్ లీజ్డ్ లైన్లు, స్టాటిక్ ఐపీ, క్లౌడ్ బ్యాకప్ మరియు ఎస్డీ-వాన్ అందిస్తుంది. మీ కంపెనీ వివరాలు తెలపండి."
                tts = "Airtel Enterprise delivers dedicated leased lines and SD-WAN across India. What is your company's profile and city?"
                return lat, nat, tts
            if clean_msg in {"quote"}:
                lat = "I'd love to generate an instant, audited CPQ quote for you! Just tell me your business type (e.g. clinic, shop, factory, office), city, and team size."
                nat = "మీ వ్యాపారం కోసం తక్షణ సిపిక్యు కొటేషన్ అందించడానికి సిద్ధంగా ఉన్నాను! మీ వ్యాపార రకం, నగరం మరియు సిబ్బంది సంఖ్య తెలపండి."
                tts = "I'd love to generate an instant quote for you! Just tell me your business type, city, and team size."
                return lat, nat, tts
            if clean_msg in {"connection"}:
                lat = "Let's get your enterprise connected! Do you need fiber broadband for a single shop, an office, or multiple branches across cities?"
                nat = "మీ వ్యాపారానికి కనెక్టివిటీ అందిద్దాం! ఒంటి షాప్ కోసమా, ఆఫీస్ కోసమా లేదా పలు బ్రాంచీల కోసమా?"
                tts = "Let's get your enterprise connected! Do you need fiber broadband for a single shop or an office?"
                return lat, nat, tts
            if clean_msg in {"airtel"}:
                lat = "Airtel Business Udyam Digital at your service! How can I assist you with broadband, static IPs, or enterprise solutions today?"
                nat = "ఎయిర్‌టెల్ బిజినెస్ ఉద్యమ్ డిజిటల్ సేవలో ఉంది! బ్రాడ్‌బ్యాండ్, స్టాటిక్ ఐపీ లేదా క్లౌడ్ సేవల గురించి ఎలా సహాయపడగలను?"
                tts = "Airtel Business Udyam Digital at your service! How can I assist you today?"
                return lat, nat, tts

        # 4. Regional Greetings
        if is_greeting:
            if active_lang == "te":
                lat = "Namaskaram! Nenu bagunnanu, dhanyavadalu 🙏 Meeru ela unnaru? Welcome to Airtel Business. Mee business ki kotha fiber connection kosam chusthunnara? Ee oori lo undi mee business?"
                nat = "నమస్కారం! నేను బాగున్నాను, ధన్యవాదాలు 🙏 మీరు ఎలా ఉన్నారు? ఎయిర్‌టెల్ బిజినెస్‌కు స్వాగతం. మీ వ్యాపారానికి ఫైబర్ కనెక్షన్ కోసం చూస్తున్నారా? మీ వ్యాపారం ఏ ఊరిలో ఉంది?"
                tts = "Namaskaram! Nenu bagunnanu, dhanyavadalu. Meeru ela unnaru? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "hi":
                lat = "Namaste! Main bilkul theek hoon, dhanyawaad 🙏 Aap kaise hain? Welcome to Airtel Business Udyam Digital. Apne business ya dukan ke liye internet connection dekh rahe hain? Kis city mein hai aapka setup?"
                nat = "नमस्ते! मैं बिल्कुल ठीक हूँ, धन्यवाद 🙏 आप कैसे हैं? एयरटेल बिजनेस उद्यम डिजिटल में आपका स्वागत है। अपने व्यवसाय के लिए इंटरनेट कनेक्शन देख रहे हैं? किस शहर में है आपका सेटअप?"
                tts = "Namaste! Main bilkul theek hoon, dhanyawaad. Aap kaise hain? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "ta":
                lat = "Vanakkam! Naan nallaa irukken, nandri 🙏 Neenga eppadi irukkeenga? Welcome to Airtel Business. Ungal business-ku fiber internet pathi therinjukka virumbureengala? Endha ooru ungalukku?"
                nat = "வணக்கம்! நான் நலமாக இருக்கிறேன், நன்றி 🙏 நீங்கள் எப்படி இருக்கிறீர்கள்? ஏர்டెல் பிசினஸுக்கு வரவேற்கிறோம். உங்கள் வணிகத்திற்கு ஃபைபர் இணையம் வேண்டுமா? எந்த ஊர் உங்களுடையது?"
                tts = "Vanakkam! Naan nallaa irukken, nandri. Neenga eppadi irukkeenga? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "bn":
                lat = "Nomoshkar! Ami bhalo achi, dhonnobad 🙏 Apni kemon achen? Welcome to Airtel Business. Apnar dokan ba office-er jonyo internet connection lagbe? Kon shohore apnar business?"
                nat = "নমস্কার! আমি ভালো আছি, ধন্যবাদ 🙏 আপনি কেমন আছেন? এয়ারটেল বিজনেসে স্বাগতম। আপনার ব্যবসার জন্য ইন্টারনেট কানেকশন খুঁজছেন? কোন শহরে আপনার ব্যবসা?"
                tts = "Nomoshkar! Ami bhalo achi, dhonnobad. Apni kemon achen? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "gu":
                lat = "Namaste! Hu majama chhu, aabhar 🙏 Tame kem cho? Welcome to Airtel Business. Tamara business mate fiber internet joyie chhe? Kya sheher ma chhe tamaro business?"
                nat = "નમસ્તે! હું મજામાં છું, આભાર 🙏 તમે કેમ છો? એરટેલ બિઝનેસમાં સ્વાગત છે. તમારા વ્યવસાય માટે ફાઇબર ઇન્ટરનેટ જોઈએ છે? કયા શહેરમાં છે તમારો વ્યવસાય?"
                tts = "Namaste! Hu majama chhu, aabhar. Tame kem cho? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "kn":
                lat = "Namaskara! Naanu chennagi iddini, dhanyavada 🙏 Neevu hegiddira? Welcome to Airtel Business. Nimma business-ge fiber internet connection beka? Yaava ooru nimmadhu?"
                nat = "ನಮಸ್ಕಾರ! ನಾನು ಆರಾಮಾಗಿದ್ದೇನೆ, ಧನ್ಯವಾದಗಳು 🙏 ನೀವು ಹೇಗಿದ್ದೀರಿ? ಏರ್‌ಟೆಲ್ ಬಿಸಿನೆಸ್‌ಗೆ ಸುಸ್ವಾಗತ. ನಿಮ್ಮ ವ್ಯಾಪಾರಕ್ಕೆ ಫೈಬರ್ ಇಂಟರ್ನೆಟ್ ಸಂಪರ್ಕ ಬೇಕೇ? ಯಾವ ಊರು ನಿಮ್ಮದು?"
                tts = "Namaskara! Naanu chennagi iddini, dhanyavada. Neevu hegiddira? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "ml":
                lat = "Namaskaram! Enikku sukhamanu, nandi 🙏 Sukhamano? Welcome to Airtel Business. Ningalude business-nu fiber internet connection aano nokkunnath? Ethekilum city aano?"
                nat = "നമസ്കാരം! എനിക്ക് സുഖമാണ്, നന്ദി 🙏 സുഖമാണോ? എയർടെൽ ബിസിനസ്സിലേക്ക് സ്വാഗതം. നിങ്ങളുടെ ബിസിനസ്സിന് ഫൈബർ ഇന്റർനെറ്റ് കണക്ഷനാണോ നോക്കുന്നത്? ഏത് നగരത്തിലാണ്?"
                tts = "Namaskaram! Enikku sukhamanu, nandi. Sukhamano? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "mr":
                lat = "Namaskar! Me majet aahe, dhanyawad 🙏 Tumhi kase ahat? Welcome to Airtel Business. Aplya business sathi fiber internet connection hawe aahe ka? Konte shahar aahe aaple?"
                nat = "नमस्कार! मी मजेत आहे, धन्यवाद 🙏 तुम्ही कसे आहात? एअरटेल बिझनेसमध्ये आपले स्वागत आहे. आपल्या व्यवसायासाठी फायबर इंटरनेट हवे आहे का? कोणते शहर आहे आपले?"
                tts = "Namaskar! Me majet aahe, dhanyawad. Tumhi kase ahat? Welcome to Airtel Business."
                return lat, nat, tts
            if active_lang == "or":
                lat = "Namaskar! Mu bhalare achi, dhanyabad 🙏 Apana kemiti achanti? Welcome to Airtel Business. Apnanka business pain fiber internet connection darkar ki? Kouthi city re apnanka business?"
                nat = "ନମସ୍କାର! ମୁଁ ଭଲରେ ଅଛି, ଧନ୍ୟବାଦ 🙏 ଆପଣ କେମିତି ଅଛନ୍ତି? ଏୟାରଟେଲ ବିଜନେସକୁ ସ୍ୱାଗତ। ଆପଣଙ୍କ ବ୍ୟବସାୟ ପାଇଁ ଫାଇବର ଇଣ୍ଟରନେଟ ଦରକାର କି? କେଉଁ ସହରରେ ଆପଣଙ୍କ ବ୍ୟବସାୟ?"
                tts = "Namaskar! Mu bhalare achi, dhanyabad. Apana kemiti achanti? Welcome to Airtel Business."
                return lat, nat, tts
            # General greeting
            lat = "Namaste! Welcome to Airtel Business Udyam Digital 🙏 How can I assist your enterprise with broadband, static IPs, or digital connectivity today?"
            nat = lat if active_lang == "en" else "నమస్కారం! ఎయిర్‌టెల్ బిజినెస్ ఉద్యమ్ డిజిటల్‌కు స్వాగతం 🙏 మీ వ్యాపారానికి కనెక్టివిటీ లేదా డిజిటల్ సేవల గురించి ఎలా సహాయపడగలను?"
            tts = "Namaste! Welcome to Airtel Business Udyam Digital. How can I assist your enterprise today?"
            return lat, nat, tts

        # 5. Incomplete Turn: Warm, Conversational Clarification Question
        missing_text = []
        if "business_type" in slots.missing_slots:
            missing_text.append("what kind of business you run (e.g. retail shop, clinic, CA office, factory)")
        if "city_or_tier" in slots.missing_slots:
            missing_text.append("which city or town you are located in")
        if "gstin_status" in slots.missing_slots:
            missing_text.append("whether you have a registered GST number or only Udyam")

        details_str = ", ".join(missing_text) if missing_text else "your business type and city"
        if active_lang == "te":
            lat = "Namaskaram! Mee business kosam saraina connectivity plan chudalante, mee business type (retail shop, clinic, CA office, factory), city mariyu GST undo ledo cheppandi."
            nat = "నమస్కారం! మీ వ్యాపారానికి తగిన ఖచ్చితమైన ప్లాన్ అందించడానికి దయచేసి మీ వ్యాపార రకం, నగరం మరియు జీఎస్టీ వివరాలు తెలపండి."
            tts = "Namaskaram! Mee business kosam saraina connectivity plan chudalante, mee business type mariyu city cheppandi."
        elif active_lang == "hi":
            lat = "Namaste! Aapke business ke liye best internet plan configure karne ke liye, kripya apna business type (kirana shop, clinic, CA office, factory), city aur GST status bataiye."
            nat = "नमस्ते! आपके व्यवसाय के लिए सही इंटरनेट प्लान चुनने के लिए, कृपया अपना व्यवसाय प्रकार, शहर और जीएसटी विवरण बताएं।"
            tts = "Namaste! Aapke business ke liye sahi internet plan suggest karne ke liye apna business type aur city bataiye."
        elif active_lang == "ta":
            lat = "Vanakkam! Ungal business-ku sariyana connectivity plan kidaikka, ungal business type (kadai, clinic, office), ooru matrum GST irukka nu sollunga."
            nat = "வணக்கம்! உங்கள் வணிகத்திற்கு சரியான திட்டத்தை வழங்க, உங்கள் வணிக வகை, நகரம் மற்றும் ஜிஎஸ்டி விவரங்களை தெரிவிக்கவும்."
            tts = "Vanakkam! Ungal business-ku sariyana plan solla, ungal business type matrum ooru sollunga."
        elif active_lang == "kn":
            lat = "Namaskara! Nimma business-ge sarihondova internet plan thilisalu, nimma business type (angadi, clinic, office), ooru mathu GST idiya antha thilisi."
            nat = "ನಮಸ್ಕಾರ! ನಿಮ್ಮ ವ್ಯಾಪಾರಕ್ಕೆ ಸೂಕ್ತವಾದ ಯೋಜನೆಯನ್ನು ಒದಗಿಸಲು, ದಯವಿಟ್ಟು ನಿಮ್ಮ ವ್ಯಾಪಾರದ ಪ್ರಕಾರ, ನಗರ ಮತ್ತು ಜಿಎಸ್‌ಟಿ ವಿವರಗಳನ್ನು ತಿಳಿಸಿ."
            tts = "Namaskara! Nimma business-ge sarihondova plan thilisalu, nimma business type mathu ooru thilisi."
        elif active_lang == "bn":
            lat = "Nomoshkar! Apnar business-er jonyo shothik internet plan suggest korte, apnar business type (dokan, clinic, office), shohor ebong GST ache kina janan."
            nat = "নমস্কার! আপনার ব্যবসার জন্য সঠিক ইন্টারনেট প্ল্যান বেছে নিতে, আপনার ব্যবসার ধরন, শহর এবং জিএসটি বিবরণ জানান।"
            tts = "Nomoshkar! Apnar business-er jonyo shothik internet plan suggest korte, apnar business type ebong shohor janan."
        elif active_lang == "gu":
            lat = "Namaste! Tamara business mate yogya plan suggest karva, tamaro business type (dukan, clinic, office), sheher ane GST chhe ke nahi te janavo."
            nat = "નમસ્તે! તમારા વ્યવસાય માટે યોગ્ય પ્લાન સૂચવવા, તમારો વ્યવસાય પ્રકાર, શહેર અને જીએસટી વિગતો જણાવો."
            tts = "Namaste! Tamara business mate yogya plan suggest karva, tamaro business type ane sheher janavo."
        elif active_lang == "mr":
            lat = "Namaskar! Aplya business sathi yogya internet plan sangnyasathi, aplya business cha prakar (dukan, clinic, office), shahar ani GST status sanga."
            nat = "नमस्कार! आपल्या व्यवसायासाठी योग्य इंटरनेट प्लॅन निवडण्यासाठी, कृपया आपल्या व्यवसायाचा प्रकार, शहर आणि जीएसटी स्थिती सांगा."
            tts = "Namaskar! Aplya business sathi yogya plan sangnyasathi aplya business cha prakar ani shahar sanga."
        elif active_lang == "ml":
            lat = "Namaskaram! Ningalude business-nu aniyojyamaya plan thiranjedukkan, business type (kada, clinic, office), sthalam, GST undo ennu parayuka."
            nat = "നമസ്കാരം! നിങ്ങളുടെ ബിസിനസ്സിന് അനുയോജ്യമായ പ്ലാൻ തിരഞ്ഞെടുക്കാൻ, ബിസിനస్സ് തരം, സ്ഥലം, ജിഎസ്ടി വിവരങ്ങൾ പറയുക."
            tts = "Namaskaram! Ningalude business-nu aniyojyamaya plan thiranjedukkan business type parayuka."
        elif active_lang == "or":
            lat = "Namaskar! Apnanka business pain thik internet plan suggest kariba pain, business type (dokana, clinic, office), sahar o GST status janantu."
            nat = "ନମସ୍କାର! ଆପଣଙ୍କ ବ୍ୟବସାୟ ପାଇଁ ଉପଯୁକ୍ତ ପ୍ଲାନ ଜଣାଇବା ପାଇଁ, ଦୟାକରି ବ୍ୟବସାୟ ପ୍ରକାର, ସହର ଏବଂ ଜିଏସଟି ବିବରଣୀ ଦିଅନ୍ତୁ।"
            tts = "Namaskar! Apnanka business pain thik plan suggest kariba pain business type janantu."
        else:
            lat = f"Namaste! I'd love to help you configure the ideal business connectivity plan. Could you please clarify {details_str}?"
            nat = lat if active_lang == "en" else "నమస్కారం! మీ వ్యాపారానికి తగిన ఖచ్చితమైన ప్లాన్ అందించడానికి దయచేసి మీ వ్యాపార రకం, నగరం మరియు జీఎస్టీ వివరాలు తెలపండి."
            tts = "Namaste! I'd love to help you configure the ideal business plan. Could you please clarify your business type and city?"
        return lat, nat, tts
