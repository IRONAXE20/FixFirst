"""The agent: one caller turn in, the words to speak out.

Order of a turn:
  1. Deterministic hazard screen on the caller's words (no LLM involved).
  2. The LLM chooses a tool (start a playbook, answer the current step, record an error
     code, escalate, flag a hazard) or replies briefly in its own words.
  3. The server executes the tool. Instructions come only from playbook scripts, spoken
     word for word; the LLM's own words pass through the unsafe-instruction guard.
"""
import asyncio
import re
import secrets
import time
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from loguru import logger

from .config import Settings, Tenant, now_ist
from .db import fetch_customer, fetch_customer_by_phone, fetch_rentals, link_number
from .knowledge import DocLibrary
from .llm import ChatModel, LLMError, LLMResult, ToolCall
from .playbooks import Node, PlaybookError, PlaybookLibrary, PlaybookRun
from .safety import HAZARD_KINDS, SafetyScreen, physical_instruction, unsafe_instruction
from .session import CallSession, appliance_name
from .tickets import TicketService

HISTORY_LIMIT = 40
STALL_LIMIT = 3
# Steps the caller already answered unasked: at most this many extra LLM calls (~0.25 s each) per turn,
# look-ahead rounds and skip checks together.
LOOKAHEAD_ROUNDS = 2
ASK_CALLER = "ask_caller"   # look-ahead outcome: not answered yet, read the step to the caller
QUOTE_MIN_WORDS = 2

# The greeting promises "say technician at any time", so explicit requests skip the LLM.
_TECH_REQUEST = [re.compile(p) for p in (
    r"\b(send|book|need|want|get|arrange|call)\b.{0,25}\b(technician|technicion|mechanic|engineer|someone|somebody|a person|a human|repair ?man|service ?man|mistri)\b",
    r"\b(technician|technicion|mechanic|engineer|mistri|aadmi|banda|bande|insaan|service wala|kisi ko)\b.{0,25}\b(bhej\w*|bulao|bulaiye|bula do|bulwa\w*|chahiye|book kar\w*|bhijwa\w*)",
    r"(टेक्नीशियन|मैकेनिक|मिस्त्री|आदमी|किसी को).{0,20}(भेज|बुला|चाहिए)",
)]
_TECH_NOT = re.compile(r"\b(nahi|nahin|don'?t|do not|no need|not|agar|if|otherwise|warna)\b|नहीं|अगर")
# Evidence that the caller really answered a safety question with "no hazard".
_DENIAL = re.compile(r"\b(no|nope|nothing|none|not|nahi|nahin|nai|kuch nahi|koi nahi|theek|thik|fine|all good|sab sahi|clear|safe)\b|नहीं|कुछ नहीं|ठीक")
_HAZARD_WORD = re.compile(r"(smell|spark|chingar|dhu|smoke|burn|jal|paani|pani|water|leak|current|shock|jhatk|tingl|गंध|स्मेल|चिंगार|धुआ|पानी|करंट)")
_STRONG_DENIAL = re.compile(r"^\W*(no|nope|nahi|nahin|nai|na|नहीं|ना)\b|kuch( bhi)? nahi|aisa kuch nahi|waisa kuch nahi|nothing|"
                            r"all (fine|normal|good)|sab (theek|thik|normal)|bilkul normal|कुछ नहीं|ऐसा कुछ नहीं")
# Words a caller would use when something really is dangerous; an LLM-only hazard flag without
# any of them gets one safety question first instead of the emergency script.
_DANGER_WORD = re.compile(r"(spark|chingar|dhu|smoke|burn|jalne|jala|jali|current|shock|jhatk|tingl|gas|aag|fire|flame|"
                          r"leak|paani|pani|water|wet|geela|battery|batri|hurt|behosh|unconscious|melt|pighal|"
                          r"trip|toot|tut|broke|damag|chemical|"
                          r"चिंगार|धुआ|स्मोक|करंट|शॉक|झटक|गैस|आग|जलने|पानी|लीक|बैटरी|बेहोश|गीला|ट्रिप|टूट)")
# "bye" mid-playbook: the caller is leaving before the fix is done (eval F03 looped on "okay, bye").
_BYE = re.compile(r"\b(bye|goodbye|alvida|tata)\b|बाय|अलविदा")
_SWITCHGEAR = re.compile(r"switch|स्विच|\bmcb\b|main board|meter|मीटर")
_HAZARD_WORD_STRICT = re.compile(r"(smell|spark|chingar|dhu|smoke|burn|jalne|leak|current|shock|tingl|गंध|स्मेल|चिंगार|धुआ|करंट)")


_HINDI_WORDS = {"hai", "hain", "nahi", "nahin", "kar", "karo", "raha", "rahi", "rahe", "ka", "ki", "ke", "mein", "pe",
                "par", "se", "aa", "kya", "haan", "ho", "hua", "gaya", "gayi", "toh", "bhi", "kuch", "aur", "abhi", "wala",
                "wali", "bilkul", "theek", "thik", "ji", "mujhe", "maine", "aap", "hum", "tha", "thi", "diya", "liya"}
_ENGLISH_WORDS = {"the", "is", "it", "my", "not", "and", "but", "yes", "no", "a", "to", "of", "on", "in", "it's", "have",
                  "has", "was", "there", "this", "that", "i", "i'm", "don't", "can't", "isn't", "please", "what", "with"}


def detect_language(text: str) -> str | None:
    """Hindi if there's Devanagari or enough Hindi words in Roman script; English if clearly English; else None."""
    if re.search(r"[\u0900-\u097F]", text):
        return "hi"
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return None
    hi = sum(w in _HINDI_WORDS for w in words)
    en = sum(w in _ENGLISH_WORDS for w in words)
    if hi >= 2 and hi > en:
        return "hi"
    if en >= 2 and en > hi:
        return "en"
    return None


_DONE = re.compile(r"\b(nothing|that'?s all|that is all|all good|no thanks?|thanks?|thank you|bye|goodbye|"
                   r"bas|kuch nahi|kuchh nahi|nahi ji|bas itna|bas yahi|shukriya|dhanyavaad|dhanyawad|theek hai|thik hai|"
                   r"ok|okay|alvida)\b|बस|कुछ नहीं|धन्यवाद|शुक्रिया|थैंक|ठीक है|बाय|अलविदा|ओके")
# A bare "no" counts only as the first word: "नहीं, धन्यवाद" is done, "मुश्किल नहीं हो जाएगा" ("it won't be hard") isn't.
_DONE_NO = re.compile(r"^(no|nope|nahi|nahin|nai|na|नहीं|नही|ना)\b|^(नहीं|नही|ना)(?![ऀ-ॿ])")
_MORE = re.compile(r"\b(ac|fridge|frij|washing|machine|washer|tv|inverter|battery|purifier|stove|chulha|geyser|microwave|"
                   r"another|also|aur ek|ek aur|bhi|kharab|problem|issue|leak|band hai|chal nahi|nahi chal|kaam nahi|"
                   r"technician|kab|when|kitne)\b|एसी|फ्रिज|मशीन|टीवी|इन्वर्टर|बैटरी|प्यूरीफायर|स्टोव|चूल्हा|खराब|प्रॉब्लम|"
                   r"और एक|एक और|भी|लीक|बंद है|नहीं चल|टेक्नीशियन|कब")


_SAFETY_ACTION_Q = re.compile(
    r"\b(on|off|chalu|chaalu|chal raha|chal rahi|band kar\w*|band karu\w*|band kar ?doon|kya kar\w*|kya karna|"
    r"switch|plug|nikal\w*|regulator|khidki|bahar|chhu\w*|touch|use kar\w*|what should i do|turn (it )?off)\b|"
    r"ऑन|ऑफ|चालू|चल रहा|चल रही|बंद कर|बंद करूँ|बंद करूं|क्या करूँ|क्या करूं|क्या करें|क्या करना|स्विच|प्लग|निकाल|"
    r"रेगुलेटर|खिड़की|बाहर|छू")


_ACTION_DONE = re.compile(r"(kar diya|kar di|kar diye|ho gaya|ho gayi|nikal diya|band hai|bahar aa gay\w*|done)|"
                          r"कर दिया|कर दी|हो गया|हो गई|निकाल दिया|बंद है|बाहर आ गए|बाहर आ गई")


def caller_is_done(text: str) -> bool:
    """After "anything else?": a short no / thanks / bye, with no new appliance or problem in it."""
    t = text.lower().strip()
    return len(t.split()) <= 8 and bool(_DONE.search(t) or _DONE_NO.search(t)) and not _MORE.search(t)


# "Talk to a person" is a human handoff (RentBasket ecosystem), not a technician visit.
_HUMAN_REQUEST = [re.compile(p) for p in (
    r"\b(talk|speak|connect|transfer)\b.{0,15}\b(human|person|agent|someone|somebody|executive|manager|customer care|"
    r"real person|representative|staff)\b",
    r"\b(insaan|insan|manager|customer care|executive|staff|kisi aadmi|kisi insaan|real person|kisi se)\b.{0,20}"
    r"\b(baat|connect|transfer|mila\w*)",
    r"(इंसान|मैनेजर|कस्टमर केयर|किसी से|किसी आदमी).{0,20}(बात|connect|मिला)",
)]


def human_requested(text: str) -> bool:
    for clause in re.split(r"[.,!?;।]| but | par | lekin ", text.lower()):
        if any(rx.search(clause) for rx in _HUMAN_REQUEST) and not _TECH_NOT.search(clause):
            return True
    return False


_AFFIRM = re.compile(r"\b(haan|han|ha|haa|ji|jee|yes|yeah|yep|sahi|bilkul|correct|right|wahi|wohi|usi|same|theek|thik|ok|okay)\b"
                     r"|हाँ|हां|जी|सही|बिल्कुल|वही|उसी|ठीक")
_DENY = re.compile(r"^\W*(no|nope|nahi|nahin|nai|na|galat|wrong)\b|^\W*(नहीं|नही|ना|गलत)|\b(dusra|doosra|dusre|other one|another)\b|दूसर")
_DIGIT_WORDS = {"zero": "0", "shunya": "0", "sunya": "0", "one": "1", "ek": "1", "two": "2", "do": "2", "three": "3",
                "teen": "3", "four": "4", "char": "4", "chaar": "4", "five": "5", "paanch": "5", "panch": "5", "six": "6",
                "chhe": "6", "che": "6", "chhah": "6", "seven": "7", "saat": "7", "eight": "8", "aath": "8", "nine": "9",
                "nau": "9", "शून्य": "0", "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5", "पाँच": "5",
                "छह": "6", "छः": "6", "सात": "7", "आठ": "8", "नौ": "9"}


def spoken_digits(text: str) -> str:
    """Digits in what speech-to-text heard, spoken words included ("nau aath 1 0 ..." -> "9810...")."""
    out = []
    for tok in re.findall(r"[0-9]+|[a-z]+|[ऀ-ॿ]+", text.lower()):
        if tok.isdigit():
            out.append(tok)
        elif tok in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[tok])
    return "".join(out)


def phone_in(text: str) -> str | None:
    digits = spoken_digits(text)
    digits = digits[2:] if len(digits) == 12 and digits.startswith("91") else digits
    return digits[-10:] if len(digits) >= 10 and digits[-10] in "6789" else None


def otp_in(text: str) -> str | None:
    digits = spoken_digits(text)
    return digits if 4 <= len(digits) <= 6 else None


def technician_requested(text: str) -> bool:
    for clause in re.split(r"[.,!?;।]| but | par | lekin ", text.lower()):
        if any(rx.search(clause) for rx in _TECH_REQUEST) and not _TECH_NOT.search(clause):
            return True
    return False


# ---- evidence for steps the caller answered before being asked -------------------------
_NEGATIONS = {"no", "not", "nahi", "nahin", "nai", "na", "never", "nothing", "none", "mat", "bina", "dont", "cant", "isnt",
              "wasnt", "doesnt", "didnt", "wont", "नहीं", "नही", "ना", "न", "मत", "बिना"}
_CLAUSE_END = set(".,!?;:।|\n")
# Words too common to say whether the caller talked about a step (pre-filter only, see _caller_covers).
_STOP = _HINDI_WORDS | _ENGLISH_WORDS | {
    "the", "and", "for", "are", "any", "your", "you", "this", "that", "there", "with", "from", "into", "then", "than",
    "now", "right", "please", "just", "also", "still", "again", "does", "did", "has", "have", "was", "were", "can",
    "its", "our", "but", "what", "when", "how", "some", "one", "all", "very", "much", "more", "most", "out", "off",
    "too", "yeah", "okay", "haan", "check", "let", "let's", "tell", "see", "look", "make", "sure", "about", "around",
    "usually", "minutes", "five", "ten", "give", "time", "normal", "thing", "something", "anything", "like", "after",
    "before", "start", "started", "starts", "starting", "stop", "stopped", "work", "works", "working", "worked", "turn",
    "turned", "turning", "run", "runs", "running", "chal", "chala", "chali", "chalu", "chalta", "karo", "karna", "kiya",
    "kiye", "karke", "hui", "hue", "hota", "hoti", "aata", "aati", "aaya", "diya", "dena", "liya", "ab", "phir", "koi",
    "sab", "bas", "ek", "woh", "yeh", "ye", "wo", "acs", "fridge", "machine", "washing", "washer", "geyser", "unit",
    "appliance", "है", "हैं", "क्या", "और", "को", "की", "के", "का", "में", "से", "पर", "या", "कि", "भी", "तो", "ये", "वो",
    "यह", "वह", "एक", "कोई", "कुछ", "अगर", "फिर", "अब", "जब", "तब", "हाँ", "बस", "सब", "बहुत", "कर", "करके", "कीजिए",
    "करें", "करते", "रहा", "रही", "रहे", "था", "थी", "हो", "होता", "होती", "गया", "गई", "दीजिए", "आप", "आपका", "आपके",
    "मुझे", "मैं", "हम", "उस", "उसे", "इस", "इसे", "जो", "जिस", "साथ", "बाद", "पहले", "लगभग", "आमतौर", "ठीक", "चल", "चालू",
    "only", "means", "called", "inside", "nothing", "safety", "quick", "नहीं", "नही", "अभी", "मतलब", "यानी", "सिर्फ", "ज़्यादातर", "अंदर", "इसमें", "जिसमें",
    "कहते"}


def _words(text: str) -> list[tuple[str, int, int, int]]:
    """(word, start, end, clause) for each word of NFC text. Built by hand because `\\w` splits
    Devanagari words at their vowel signs."""
    out, start, clause = [], None, 0
    for i, ch in enumerate(text + " "):
        if ch == "'" or unicodedata.category(ch)[0] in "LMN":
            start = i if start is None else start
            continue
        if start is not None:
            word = text[start:i].lower().strip("'")
            if word:
                out.append((word, start, i, clause))
            start = None
        if ch in _CLAUSE_END:
            clause += 1
    return out


def _clean(text: str) -> str:
    return unicodedata.normalize("NFC", text).replace("’", "'").replace("‘", "'")


def _is_negation(word: str) -> bool:
    return word.replace("'", "") in _NEGATIONS or word.endswith("n't")


def _spelled_alike(a: str, b: str) -> bool:
    return a == b or (min(len(a), len(b)) >= 3 and SequenceMatcher(None, a, b).ratio() >= 0.8)


def _word_edits(q: list[str], s: list[str]) -> int:
    """Words to add or drop to turn q into s, when only spelling variants ("hai"/"hain") may stand in."""
    far = 99
    d = [[i + j if not (i and j) else far for j in range(len(s) + 1)] for i in range(len(q) + 1)]
    for i in range(1, len(q) + 1):
        for j in range(1, len(s) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] if _spelled_alike(q[i - 1], s[j - 1]) else far)
    return d[-1][-1]


def find_quote(quote: str, messages: list[str]) -> str | None:
    """The caller's own words matching `quote` (same words in order, spelling variants and one filler
    word allowed), or None. A match the caller negated ("switch on nahi hai") doesn't count."""
    q = [w for w, *_ in _words(_clean(quote or ""))]
    if len(q) < QUOTE_MIN_WORDS:
        return None
    q_negs = sum(map(_is_negation, q))
    best = None
    for message in messages:
        text = _clean(message)
        words = _words(text)
        for size in sorted({len(q) - 1, len(q), len(q) + 1}):
            if size < QUOTE_MIN_WORDS:
                continue
            for i in range(len(words) - size + 1):
                span = words[i:i + size]
                said = [w for w, *_ in span]
                if sum(map(_is_negation, said)) != q_negs:
                    continue
                edits = _word_edits(q, said)
                if edits > (1 if len(q) >= 3 else 0):
                    continue
                first, last = span[0][3], span[-1][3]
                nearby = [w for w, _, _, c in words[max(0, i - 2):i] if c == first] + \
                         [w for w, _, _, c in words[i + size:i + size + 3] if c == last]
                if not q_negs and any(map(_is_negation, nearby)):
                    continue
                if best is None or edits < best[0]:
                    best = (edits, text[span[0][1]:span[-1][2]])
    return best[1] if best else None


def _vocab(texts) -> set[str]:
    """Telling words of some texts, crudely stemmed so "switch"/"switched" and "battery"/"batteries" meet."""
    out = set()
    for text in texts:
        for w, *_ in _words(_clean(text or "")):
            w = w.removesuffix("'s")
            if w in _STOP or w.isdigit() or len(w) < (3 if w.isascii() else 2):
                continue
            out.add(w[:5] if w.isascii() else w)
    return out


def _node_vocab(node: Node) -> set[str]:
    return _vocab([*node.say.values(), *node.help.values(), *(o.label for o in node.outcomes.values())])


def _is_safety_question(node: Node) -> bool:
    return any(o.hazard for o in node.outcomes.values())

PHRASES = {
    "greeting": {
        # RentBasket ecosystem call script, 1. Greeting.
        "hi": "नमस्ते{name}! मैं {company} की FixFirst service assistant बोल रही हूँ। आपकी क्या मदद कर सकती हूँ?",
        "en": "Hello{name}, this is FixFirst, {company}'s service assistant. How can I help you today?",
    },
    "escalated": {
        # 7b. TICKET and 8. Photo and notes ask.
        "hi": "मैंने आपका ticket raise कर दिया है। {promise} आपका ticket number है {code}। मैं details WhatsApp पर भेज रही हूँ। "
              "WhatsApp पर एक link आएगा: problem या display की photo भेज दीजिए, और call में कुछ बताना रह गया हो तो वहाँ लिख दीजिए। "
              "इससे technician तैयारी के साथ आएगा। और कुछ मदद चाहिए?",
        "en": "I have raised a ticket for this. {promise} Your ticket number is {code}. I'm sending the details on WhatsApp. "
              "You'll get a link there: send a photo of the problem or the display, and write anything you missed telling me "
              "on the call. That way the technician comes prepared. Anything else I can help you with?",
    },
    "reopened": {
        "hi": "ये problem आपके पिछले ticket {code} पर ही थी, तो मैंने वही ticket दोबारा खोल दिया है। {promise} "
              "मैं details WhatsApp पर भेज रही हूँ। और कुछ मदद चाहिए?",
        "en": "This is the same problem as your earlier ticket {code}, so I've reopened it. {promise} "
              "I'm sending the details on WhatsApp. Anything else I can help you with?",
    },
    "resolved_tail": {
        "hi": "और कुछ मदद चाहिए?",
        "en": "Anything else I can help you with?",
    },
    "resolved_generic": {
        "hi": "बहुत बढ़िया! मैं ticket close कर रही हूँ। कल हम WhatsApp पर check करेंगे।",
        "en": "Great! I'm closing the ticket. We'll check with you on WhatsApp tomorrow.",
    },
    "anything_else": {
        "hi": "और कुछ मदद चाहिए?",
        "en": "Anything else I can help you with?",
    },
    "safety_goodbye": {
        # Ends with "अलविदा"/"goodbye": Bolna's hang-up prompt (bolna_admin.HANGUP_PROMPT) keys on these words.
        "hi": "ठीक है, आप सुरक्षित रहिए। हमारी team जल्दी से जल्दी आपको call करेगी। ध्यान रखिए, अलविदा!",
        "en": "Alright, please stay safe. Our team will call you as soon as possible. Take care, goodbye!",
    },
    "goodbye": {
        # 9. Closing. "Have a good day" is a hang-up phrase (bolna_admin.HANGUP_PROMPT).
        "hi": "Thank you{name}। {company} choose करने के लिए शुक्रिया। Have a good day!",
        "en": "Thank you{name}. Thanks for choosing {company}. Have a good day!",
    },
    # 2. Identify (number registered) and 3. Number not registered.
    "identity_confirm": {
        "hi": "मैं देख रही हूँ... आप {name} जी बोल रहे हैं? आपका address {area} है ना?",
        "en": "Let me check... am I speaking with {name}? Your address is {area}, right?",
    },
    "identity_confirm_name": {
        "hi": "मैं देख रही हूँ... आप {name} जी बोल रहे हैं?",
        "en": "Let me check... am I speaking with {name}?",
    },
    "unregistered": {
        "hi": "ये number हमारे record में नहीं है। क्या आपके घर में किसी और के नाम, जैसे roommate या family member, के number से booking है?",
        "en": "This number isn't in our records. Is the booking under someone else's number at your home, like a roommate or a family member?",
    },
    "ask_other_number": {
        "hi": "उनका number बता दीजिए।",
        "en": "Please tell me their number.",
    },
    "otp_sent": {
        "hi": "मैं उस number पर एक OTP भेज रही हूँ। Please OTP बता दीजिए।",
        "en": "I'm sending an OTP to that number. Please tell me the OTP.",
    },
    "otp_retry": {
        "hi": "ये OTP match नहीं हुआ। एक बार फिर से बता दीजिए।",
        "en": "That OTP didn't match. Please tell me once more.",
    },
    "otp_ok": {
        "hi": "Thank you, आपका number link हो गया है।",
        "en": "Thank you, your number is now linked.",
    },
    "otp_fail": {
        "hi": "कोई बात नहीं, मैं आपको हमारी team से connect करती हूँ।",
        "en": "No problem, I'll connect you to our team.",
    },
    "number_unknown": {
        "hi": "ये number भी हमारे record में नहीं मिला।",
        "en": "I couldn't find that number in our records either.",
    },
    "ask_problem": {
        "hi": "अब बताइए, किस appliance या सामान में problem है?",
        "en": "Now please tell me, which appliance or item has a problem?",
    },
    # 4. Pick the product (one of that type).
    "product_one": {
        "hi": "आपके पास {product} rent पर है। उसी के बारे में बात कर रहे हैं?",
        "en": "You have a {product} on rent. Is that the one you're calling about?",
    },
    # Misuse possible (any branch): never decide or mention charges.
    "misuse": {
        "hi": "मैं ये सारी details हमारी team को भेज रही हूँ, वो आपको update करेंगे।",
        "en": "I'm sending all these details to our team; they'll update you.",
    },
    # Human handoff lines.
    "handoff_now": {
        "hi": "मैं आपको हमारी service team से connect कर रही हूँ।",
        "en": "I'm connecting you to our service team now.",
    },
    "handoff_soon": {
        "hi": "मैंने आपका ticket raise कर दिया है, आपका ticket number है {code}। हमारी service team जल्दी आपको call करेगी। और कुछ मदद चाहिए?",
        "en": "I have raised a ticket for you; your ticket number is {code}. Our service team will call you shortly. Anything else I can help you with?",
    },
    "handoff_later": {
        "hi": "मैंने आपका ticket raise कर दिया है। हमारी team {when} सुबह 9 बजे के बाद आपको call करेगी। और कुछ मदद चाहिए?",
        "en": "I have raised a ticket for you. Our team will call you {when} after 9 am. Anything else I can help you with?",
    },
    "goodbye_later": {
        "hi": "ठीक है, जब आप तैयार हों तो हमें फिर से call कर लीजिए। धन्यवाद, अलविदा!",
        "en": "Alright, call us again whenever you're ready. Thank you, goodbye!",
    },
    "goodbye_again": {
        "hi": "जी, धन्यवाद। ध्यान रखिए, अलविदा!",
        "en": "Thank you. Take care, goodbye!",
    },
    "no_playbook": {
        "hi": "इस {appliance} के लिए अभी मेरे पास guided steps नहीं हैं, इसलिए मैं technician book कर रही हूँ।",
        "en": "I don't have guided steps for the {appliance} yet, so I'm booking a technician for you.",
    },
    "code_noted": {
        "hi": "धन्यवाद, मैंने code {code} note कर लिया है।",
        "en": "Thanks, I've noted the code {code}.",
    },
    "safety_confirm": {
        "hi": "बस safety के लिए, एक बार बता दीजिए:",
        "en": "Just to be safe, please tell me:",
    },
    "which_appliance": {
        "hi": "ये code किस appliance पर दिख रहा है?",
        "en": "Which appliance is showing this code?",
    },
    "safety_probe": {
        "hi": "बस safety के लिए पूछ रही हूँ: क्या कहीं जलने की smell, चिंगारी, धुआँ, या plug के पास पानी है, या किसी को करंट लगा है?",
        "en": "Just to be safe: is there any burning smell, sparks, smoke, or water near the plug, or did anyone get a shock?",
    },
    "steps_only": {
        "hi": "जी, समझ गई। बस ये बता दीजिए:",
        "en": "Got it. Just tell me this:",
    },
    "repeat": {
        "hi": "माफ़ कीजिए, मैं ठीक से समझ नहीं पाई।",
        "en": "Sorry, I didn't quite catch that.",
    },
    "llm_retry": {
        "hi": "माफ़ कीजिए, एक second, क्या आप फिर से बता सकते हैं?",
        "en": "Sorry, give me a second. Could you say that again?",
    },
    "llm_down": {
        "hi": "माफ़ कीजिए, मेरी तरफ से कुछ technical problem आ रही है। मैंने आपकी request दर्ज कर ली है और हमारी team जल्द आपको call करेगी।",
        "en": "Sorry, I'm having a technical problem on my side. I've logged your request, and our team will call you back shortly.",
    },
    "guarded": {
        "hi": "ये step मैं phone पर नहीं बता सकती, ये technician ही करेंगे। क्या मैं technician book कर दूँ?",
        "en": "That's something only a technician should do, so I can't guide you through it. Shall I book a technician?",
    },
    "water_where": {
        "hi": "क्या पानी किसी plug, switch, board या तारों के पास है, या सिर्फ़ {appliance} से टपक रहा है?",
        "en": "Is the water near a plug, switch, board or wires, or is it only dripping from the {appliance}?",
    },
    "hazard_hold": {
        "hi": "जी। {reminder} हमारी team जल्दी से जल्दी आपको call करेगी।",
        "en": "Yes. {reminder} Our team will call you as soon as possible.",
    },
    "take_your_time": {
        "hi": "ठीक है, आराम से कीजिए। हो जाए तो बताइए।",
        "en": "Sure, take your time. Tell me when it's done.",
    },
    "offer_technician": {
        "hi": "अगर आप चाहें तो मैं अभी technician book कर सकती हूँ।",
        "en": "If you'd like, I can book a technician right now.",
    },
    "wrap_up": {
        "hi": "ठीक है, ध्यान रखिए। {company} को call करने के लिए धन्यवाद!",
        "en": "Alright, take care. Thanks for calling {company}!",
    },
}

SYSTEM_PROMPT = """You are {agent}, the voice assistant on {company}'s FixFirst service line. Customers call when an appliance they rent from {company} stops working. You are speaking on a live phone call.

HOW TO SPEAK
- This is a voice call: reply in one or two short sentences. No lists, markdown, emojis or symbols.
- Reply in the caller's language. For Hindi or Hinglish, write Hindi in Devanagari script and keep English technical words (AC, filter, remote, MCB, error code, switch) in Latin script. For English, use simple Indian English.
- Answer exactly what the caller just said, in one short sentence where you can. Don't repeat their words back, don't restate the whole situation, and don't talk about what you yourself can't do (you are a phone assistant; never say things like "I can't come to your home").
- If the caller's message is only a fragment, cut off mid-sentence ("mera AC", "matlab woh"), reply only "जी, बताइए" (or "Yes, go on") so they can finish. Don't guess or start a new question.
- Never say goodbye yourself ("आपका दिन शुभ हो", "अलविदा", "have a nice day", "goodbye"): the system says the goodbye, and the phone line hangs up when it hears one.
- Never invent dates, times, prices, names or what the technician will do. For the visit, use only the promise below and the ticket details in CALL STATE.
- Pass `language` ("hi" or "en"), matching the caller's latest message, to every tool. Hindi or Hinglish written in Roman script ("AC thanda nahi kar raha") is Hindi: use "hi". Use "en" only when the caller speaks English.

WHAT YOU MAY DO
- Always respond by calling a tool. To talk in your own words (clarify, empathise, answer a question about the call), use the `say` tool.
- Run the approved troubleshooting playbook through the tools. When a tool runs, the system reads the step text to the caller word for word, so do not repeat or paraphrase it.
- NEVER give a physical instruction in your own words (what to press, open, clean, switch, unplug or check). Every instruction must come from a playbook step via the tools. If the caller wants something the playbook doesn't cover, offer a technician.
- You may chat briefly, show empathy, clarify what the caller means, and answer questions about the call.

FLOW
1. Find out which appliance or furniture item (sofa, bed, dining table) has the problem. If the customer rents one of that type, call start_troubleshooting with it (the system confirms the product with the caller). If they rent several of that type, ask which one with `say`, naming them, like "आपके पास दो AC हैं, एक Voltas और एक Lloyd। किस वाले में problem है?". Pass the symptom if the caller already described it. If you're not sure which issue the caller means, confirm it first in one short question, like "मतलब fridge से आगे से पानी आ रहा है, सही समझा?".
2. After each step, call answer_step with the outcome that matches the caller's reply. If the reply is unclear, ask a short question with `say`; if they don't understand the step, call explain_step.
   Only answer a step after the caller has replied to it, with one exception: if CALL STATE shows a step that hasn't been read to the caller yet, and the caller's own words earlier in this call already answer that exact step (for example they said "abhi power cut hai, inverter pe chal raha hai" before being asked about backup power), you may answer it without asking. Then you must pass `caller_said`: the words that answer it, copied exactly from the caller's message, in the same script and spelling (don't translate, transliterate or paraphrase). Words about a different check don't count (a wall switch being on says nothing about the MCB). If in doubt, don't skip: the step will be read to the caller. The system checks the quote, and never skips the safety question.
   For the safety question, choose the no-hazard answer only if the caller actually said there is no smell, sparks, smoke or water. Never assume it from unrelated details; if they answered something else, ask again with `say`.
   If the caller answers the step and also asks a question ("haan kar dunga, par kya isse theek ho jayega?"), answer the step: the next step or ending answers it. That question is not a request for a technician.
   If the caller hasn't finished the step yet (they're about to do it, are still waiting, ask you to hold on, or ask whether they should wait), do NOT answer the step. Reply with `say` and only a short acknowledgement, like "ठीक है, आराम से कीजिए, हो जाए तो बताइए" or "हाँ, तीन minute रुकिए, फिर बताइए". Don't repeat the instruction; they already heard it.
3. As soon as the caller mentions an error code, at any point (even while the safety question is pending), call record_error_code with the code exactly as heard (like "IE", "4C", "E4") and the brand if known. The system still asks the safety question first. Never explain what a code means yourself.
4. If the caller asks for a technician or can't do a step, call escalate_to_technician. If they ask for a person (a human, a manager, customer care), are angry or upset, or the problem isn't covered by any playbook, call handoff_to_human.
5. If the caller says it's working again outside a step, call mark_resolved.

SAFETY COMES FIRST
If the caller mentions anything that could be dangerous (a gas smell, a burning smell, sparks, smoke, fire, an electric shock or current from the appliance, water near plugs, sockets or wires, a breaker or MCB that keeps tripping, a swollen or leaking battery, or someone hurt), call flag_safety_concern immediately, even if unsure. Never continue troubleshooting after that.
Use kind "stop_use" for the other stop-use faults in RentBasket's safety gate: a damaged microwave door, hinge or glass, a microwave that runs with the door open, a washing machine moving violently or flooding the floor, a strong chemical smell from an AC or fridge, or a damaged plug or cable.
These are NOT hazards on their own; keep following the playbook, which covers them:
- "light/display/indicator jal raha hai" means it is ON (lit), not burning. Only "jalne ki smell", "kuch jal raha hai" or "aag" means burning.
- "current/light nahi aa raha" (no power) or a power cut is a playbook fault, not an emergency. A breaker that trips once is a playbook answer; one that keeps tripping is a hazard.
- "AC ki gas khatam / gas bharni hai" is refrigerant, a technician job, not a gas leak.
- A bad smell on its own (old food, musty, "badboo") is not a hazard; the playbook asks about it. Only a burning, smoky, gas or chemical smell is.
- A low, yellow or uneven gas-stove flame, or a burner that won't light, is a stove fault for a technician, unless the caller smells gas, hears hissing, or something caught fire.
- Water leaking from an AC, fridge or washing machine is a playbook fault unless it is near a plug, socket, board or wires.

POSSIBLE MISUSE
If the damage may have come from how the item was used (it was dropped, hit or broken by someone, liquid was poured into it, the customer moved or opened it, a child or pet damaged it) or the caller asks whether they'll be charged, call flag_possible_misuse once, then carry on with the playbook as usual. Never decide or mention charges, fault or blame: the Auditing team reviews it and informs the customer.

THINGS YOU DON'T DO
- Don't discuss prices, deposits, damage charges, rent or refunds: say the {company} team will help with that.
- Don't promise visit times beyond: "{promise}"
- Don't ask for reviews or ratings.

CALL STATE
{state}"""

# Every skip is re-classified blind (the checker doesn't see the model's pick) and must agree: the eval
# caught the model quoting "saare MCB bhi upar hain" (all up) for the "one switch is down" outcome.
SKIP_CHECK_PROMPT = "You classify one answer from a customer-service phone call. Reply with only the letter."
_NOT_ANSWERED = "The caller's words don't answer this question"


@dataclass
class TurnResult:
    speech: str
    hazard: str | None = None


class Brain:
    def __init__(self, session: CallSession, llm: ChatModel, *, tenant: Tenant, settings: Settings,
                 library: PlaybookLibrary, screen: SafetyScreen, tickets: TicketService, docs: DocLibrary | None = None,
                 lookup_customer=None):
        self.s = session
        self.llm = llm
        self.tenant = tenant
        self.settings = settings
        self.library = library
        self.screen = screen
        self.tickets = tickets
        self.docs = docs
        self._lookahead_left = 0   # extra LLM calls left in this turn for skipping answered steps
        self._evidence_from = 0    # index in session.messages where the current problem's words start
        self._timing: dict = {}
        self.lookup_customer = lookup_customer   # async (phone) -> customer id: RentBasket lookup for the OTP number
        self._pending_sends: list = []           # WhatsApp sends to run after the reply when not in the background

    # ---- public API ------------------------------------------------------------------
    def greeting(self) -> str:
        lang = self.s.language
        name = (f" {self.s.first_name} जी" if lang == "hi" else f" {self.s.first_name}") if self.s.first_name else ""
        text = self._phrase("greeting", name=name)
        self.s.messages.append({"role": "assistant", "content": text})
        self.s.say("agent", text)
        return text

    def adopt_greeting(self, text: str) -> str:
        """The call platform already spoke its own welcome line (Bolna); record it as the agent's first turn."""
        self.s.messages.append({"role": "assistant", "content": text})
        self.s.say("agent", text)
        return text

    async def respond(self, user_text: str) -> TurnResult:
        started = time.monotonic()
        text = user_text.strip()
        if not text:
            return TurnResult(speech="")
        self.s.say("user", text)
        self.s.messages.append({"role": "user", "content": text})
        timing = {"llm_ms": 0, "tools": []}
        # The model's language choice wobbles on Roman-script Hinglish, so the caller's words decide when clear.
        self._turn_language = detect_language(text)
        if self._turn_language:
            self.s.language = self._turn_language

        hazard = None if self.s.hazard else self.screen.screen(text, self.s.appliance)
        if hazard:
            speech = await self._hazard(hazard.kind, source="screen", evidence=hazard.matched)
            self.s.messages.append({"role": "assistant", "content": speech})
            result = TurnResult(speech=speech, hazard=hazard.kind)
        elif (self.s.hazard and not self.s.end_requested and _SAFETY_ACTION_Q.search(text.lower())
              and not _ACTION_DONE.search(text.lower())):
            # "par AC to abhi on hai", "kya karun?": answer with that hazard's scripted reminder, not the model.
            speech = self._hazard_hold()
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("safety_reminder", kind=self.s.hazard)
            result = TurnResult(speech=speech, hazard=self.s.hazard)
        elif self.s.hazard and not self.s.end_requested and caller_is_done(text):
            # After the safety script, "theek hai / thank you" ends the call with a short safe goodbye.
            speech = self._phrase("safety_goodbye", minutes=self.tenant.urgent_callback_minutes)
            self.s.end_requested = True
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("goodbye")
            result = TurnResult(speech=speech, hazard=self.s.hazard)
        elif self.s.end_requested and not _MORE.search(text.lower()):
            # The goodbye was said and the caller answers ("ठीक है थैंक यू"): end again, with a hang-up phrase.
            speech = self._phrase("goodbye_again")
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("goodbye")
            result = TurnResult(speech=speech, hazard=self.s.hazard)
        elif (not self.s.closed and not self.s.ticket and not self.s.end_requested and _BYE.search(text.lower())
              and caller_is_done(text)):
            # The caller says bye before the fix is finished: let them go (a reconnect message follows the hang-up).
            speech = self._phrase("goodbye_later")
            self.s.end_requested = True
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("goodbye", early=True)
            result = TurnResult(speech=speech)
        elif self.s.closed and self.s.asked_anything_else and not self.s.end_requested and caller_is_done(text):
            # The problem is closed, we asked "anything else?", and the caller is done: say goodbye. On a
            # phone call the Bolna bridge then tells Bolna to hang up (see bolna.py hangup_answer).
            first = self.s.first_name
            speech = self._phrase("goodbye", name=(f" {first} जी" if self.s.language == "hi" else f" {first}") if first else "")
            self.s.end_requested = True
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("goodbye")
            result = TurnResult(speech=speech)
        elif not self.s.handed_off and human_requested(text):
            speech = await self._handoff("Caller asked for a person", said=text[:200])
            self.s.messages.append({"role": "assistant", "content": speech})
            result = TurnResult(speech=speech)
        elif (gate := await self._gate(text, timing)) is not None:
            result = gate
        elif not self.s.ticket and technician_requested(text):
            stopped = f" (stopped at step '{self.s.run.current}')" if self.s.run and not self.s.run.finished else ""
            speech = await self._escalate(likely_cause=f"Customer asked for a technician{stopped}",
                                          technician_note=None, reason=text[:200])
            self.s.messages.append({"role": "assistant", "content": speech})
            self.s.publish("tool", name="technician_request", arguments={"text": text})
            result = TurnResult(speech=speech)
        else:
            result = await self._llm_turn(timing)
        self._finish_turn(result.speech, started, timing)
        while self._pending_sends:
            await self._pending_sends.pop(0)
        return result

    # ---- before the model: who is calling, and which product (phone calls) -------------
    async def _gate(self, text: str, timing: dict) -> TurnResult | None:
        """RentBasket's call script steps 2 to 4: confirm the caller, link an unregistered number by OTP, confirm
        the product. Phone calls only (the web call page already knows the customer). None: let the model answer."""
        s = self.s
        if (s.channel == "phone" and self.settings.identity_check and s.identity != "done" and not s.ticket
                and not s.closed):
            return await self._identity(text, timing)
        if s.pending_start:
            args, s.pending_start = s.pending_start, None
            if _AFFIRM.search(text.lower()) and not _DENY.search(text.lower()):
                s.confirmed_rentals.add(args["rental_id"])
                out = await self._tool_start_troubleshooting(args, set(s.presented))
                return self._scripted(out.get("speak") or "")
        return None

    async def _identity(self, text: str, timing: dict) -> TurnResult | None:
        s, low = self.s, text.lower()
        if s.identity is None:
            if s.customer:
                s.identity = "confirm"
                first = s.first_name or s.customer.get("name", "")
                area = s.customer.get("area")
                key = "identity_confirm" if area else "identity_confirm_name"
                return self._scripted(self._phrase(key, name=first, area=area))
            s.identity = "ask_other"
            return self._scripted(self._phrase("unregistered"))
        if s.identity == "confirm":
            s.identity = "done"
            if _DENY.search(low) and not _AFFIRM.search(low.split(",")[0]):
                s.not_account_holder = True   # someone else at the same home; carry on
            return None   # the model now answers the problem the caller described (it's in the history)
        if s.identity in ("ask_other", "await_number"):
            number = phone_in(text)
            if number:
                return await self._send_otp(number)
            if s.identity == "ask_other" and _DENY.search(low):
                return self._scripted(await self._handoff("Caller's number isn't registered and no other number is on the booking"))
            s.identity_tries += 1
            if s.identity_tries >= 2:
                return self._scripted(await self._handoff("Couldn't identify the caller's account"))
            if s.identity == "ask_other" and _AFFIRM.search(low):
                s.identity, s.identity_tries = "await_number", 0
            return self._scripted(self._phrase("ask_other_number" if s.identity == "await_number" else "unregistered"))
        if s.identity == "await_otp":
            code, otp = otp_in(text), s.otp or {}
            if code and code == otp.get("code") and time.monotonic() < otp.get("expires", 0):
                if s.caller_phone:
                    link_number(self.tickets.engine, s.caller_phone, otp["customer_id"], otp["phone"])
                s.customer = fetch_customer(self.tickets.engine, otp["customer_id"])
                s.rentals = fetch_rentals(self.tickets.engine, otp["customer_id"])
                s.identity, s.linked_by_otp, s.otp = "done", True, None
                s.publish("number_linked", customer_id=otp["customer_id"])
                ok = self._phrase("otp_ok")
                # Carry on with the problem the caller described at the start, if they did.
                said = [m["content"] for m in s.messages if m["role"] == "user"]
                if len(said) > 3:
                    result = await self._llm_turn(timing)
                    return TurnResult(speech=f"{ok} {result.speech}".strip(), hazard=result.hazard)
                return self._scripted(f"{ok} {self._phrase('ask_problem')}")
            otp["tries"] = otp.get("tries", 0) + 1
            if otp["tries"] >= 2:
                s.identity = "done"
                return self._scripted(f"{self._phrase('otp_fail')} " + await self._handoff("OTP failed twice"))
            return self._scripted(self._phrase("otp_retry"))
        return None

    async def _send_otp(self, number: str) -> TurnResult:
        s = self.s
        cid = await self.lookup_customer(number) if self.lookup_customer else None
        if not cid:
            cid = (fetch_customer_by_phone(self.tickets.engine, number) or {}).get("id")
        if not cid:
            return self._scripted(f"{self._phrase('number_unknown')} " + await self._handoff("Number given for OTP isn't registered"))
        code = f"{secrets.randbelow(10 ** 6):06d}"
        s.otp = {"code": code, "customer_id": cid, "phone": number, "tries": 0, "expires": time.monotonic() + 600}
        sent = await self.tickets.send_otp(s, number, code) if not s.test_call else True
        s.publish("otp_sent", to=f"…{number[-4:]}", sent=sent)
        if not sent:
            s.identity = "done"
            return self._scripted(f"{self._phrase('otp_fail')} " + await self._handoff("Couldn't send the OTP"))
        s.identity = "await_otp"
        return self._scripted(self._phrase("otp_sent"))

    def _scripted(self, speech: str) -> TurnResult:
        self.s.messages.append({"role": "assistant", "content": speech})
        return TurnResult(speech=speech, hazard=self.s.hazard)

    async def _handoff(self, reason: str, said: str | None = None) -> str:
        """A person takes over: a warm transfer to the care number 9am-8pm IST, else a ticket and a callback after
        9am. A ticket is raised either way."""
        s = self.s
        s.handed_off = True
        ticket = await self.tickets.create(s, priority="normal", category="handoff", likely_cause=f"Human handoff: {reason}",
                                           technician_note="Call the customer back about this", reason=said)
        s.resolution = "handoff"
        self._close("escalated")
        s.publish("handoff", reason=reason)
        if self.tenant.in_human_hours():
            if s.channel == "phone":
                s.transfer_requested, s.asked_anything_else = True, False
                return self._phrase("handoff_now")
            return self._phrase("handoff_soon", code=" ".join(ticket["code"].replace("-", "")))
        late = now_ist().hour >= self.tenant.human_hours[1]
        when = ("कल" if late else "आज") if s.language == "hi" else ("tomorrow" if late else "today")
        return self._phrase("handoff_later", when=when)

    async def voice_failure(self, reason: str) -> None:
        """Speech-to-text or text-to-speech is down mid-call: book a callback instead of leaving silence."""
        if self.s.ticket or self.s.hazard:
            return
        await self.tickets.create(self.s, priority="normal", category="system",
                                  likely_cause="Voice service failed during the call; customer needs a callback",
                                  technician_note=None, reason=reason)
        self._close("escalated")

    async def on_hangup(self) -> None:
        if self.s.ended:
            return
        self.s.ended = True
        if self.s.outcome == "in_progress":
            self.s.outcome = "abandoned"
            if self.s.run and not self.s.run.finished:
                name = appliance_name(self.s.appliance, "en")
                self.tickets.schedule_followup(
                    self.s, 0, "disconnected",
                    f"{self.tenant.name} FixFirst: looks like our call about your {name} got cut. "
                    f"Reply here or call us back and we'll continue, or we can book a technician.")
        self.tickets.persist_call(self.s)
        self.s.publish("ended", outcome=self.s.outcome)

    # ---- LLM turn --------------------------------------------------------------------
    async def _llm_turn(self, timing: dict) -> TurnResult:
        presented_before = set(self.s.presented)
        self._lookahead_left, self._timing = LOOKAHEAD_ROUNDS, timing
        try:
            offered = self._tools()
            self._offered = {t["function"]["name"] for t in offered}
            llm_result = await self.llm.complete(self._messages(), offered)
            self.s.llm_failures = 0
        except LLMError as e:
            logger.warning(f"[{self.s.id}] LLM failed: {e}")
            self.s.llm_failures += 1
            self.s.publish("error", message=f"LLM error: {e}")
            if self.s.llm_failures >= 2 and not self.s.ticket:
                await self.tickets.create(self.s, priority="normal", category="system",
                                          likely_cause="Agent could not complete the call (system error)",
                                          reason="Automatic fallback after repeated LLM errors")
                self._close("escalated")
                return TurnResult(speech=self._phrase("llm_down"))
            return TurnResult(speech=self._phrase("llm_retry"))
        timing["llm_ms"] = llm_result.ms

        speech_tool, closing, needs_followup_llm, said, pending = None, None, False, None, None
        tool_messages, prefixes = [], []
        for tc in llm_result.tool_calls:
            timing["tools"].append(tc.name)
            out = await self._execute(tc, presented_before)
            tool_messages.append({"role": "tool", "tool_call_id": tc.id, "content": out.get("result", "ok")})
            if out.get("language") in ("hi", "en"):
                self.s.language = out["language"]
            if out.get("own"):
                said = out["own"]
            if out.get("prefix"):
                prefixes.append(out["prefix"])
            if out.get("closing"):
                closing = out["speak"]
                break
            if out.get("pending"):
                # Reached a step the caller may already have answered; it's read out after the look-ahead.
                pending, speech_tool = out["pending"], None
            elif out.get("speak"):
                speech_tool = out["speak"]
                if pending:
                    self._mark_heard(pending)
                    pending = None
            needs_followup_llm = needs_followup_llm or out.get("needs_llm", False)

        if llm_result.tool_calls:
            self.s.messages.append(llm_result.assistant_message())
            self.s.messages.extend(tool_messages)
            # Tool calls that were skipped by an early closing still need a tool reply.
            answered = {m["tool_call_id"] for m in tool_messages}
            for tc in llm_result.tool_calls:
                if tc.id not in answered:
                    self.s.messages.append({"role": "tool", "tool_call_id": tc.id, "content": "skipped: call is closing"})

        if pending and not closing:
            out = await self._look_ahead(timing, presented_before)
            if out.get("closing"):
                closing = out["speak"]
            else:
                speech_tool = out["speak"]

        own_words = self._guard(" ".join(t for t in (llm_result.content, said) if t) or None)
        scripted = closing or speech_tool
        if needs_followup_llm and not scripted:
            follow = await self._followup_answer(timing)
            own_words = follow or own_words
        if scripted:
            # The LLM may only add a short acknowledgement in front of a scripted step.
            ack = own_words if own_words and len(own_words) <= 40 else ""
            speech = f"{ack} {scripted}".strip()
            self.s.stalls = 0
        elif own_words:
            speech = own_words
            if self.s.run and not self.s.run.finished and not self.s.closed and not self.s.ticket:
                self.s.stalls += 1
                if self.s.stalls >= STALL_LIMIT:
                    speech = f"{speech} {self._phrase('offer_technician')}"
                    self.s.stalls = 0
        elif self.s.misunderstood >= 1 and not self.s.handed_off:
            # The second time the agent can't understand the caller: a person takes over.
            speech = await self._handoff("Agent couldn't understand the caller twice")
            self.s.messages.append({"role": "assistant", "content": speech})
        elif self.s.run and not self.s.run.finished:
            speech = f"{self._phrase('repeat')} {self._present(self.s.run.node)}"
        else:
            speech = self._phrase("repeat")
        if scripted or own_words:
            self.s.messages.append({"role": "assistant", "content": speech})
        if scripted or own_words:
            self.s.misunderstood = 0
        elif not self.s.handed_off:
            self.s.misunderstood += 1
        if prefixes:
            speech = " ".join([*prefixes, speech])
        return TurnResult(speech=speech, hazard=self.s.hazard)

    async def _followup_answer(self, timing: dict) -> str | None:
        try:
            result = await self.llm.complete(self._messages(), [])
        except LLMError:
            return None
        timing["llm_ms"] += result.ms
        return self._guard(result.content)

    async def _look_ahead(self, timing: dict, presented_before: set[str]) -> dict:
        """Extra rounds, with only answer_step on offer, to pass steps the caller already answered
        unasked. Returns what to speak: the step finally reached, or the closing script."""
        rounds, out = 0, None
        while self._lookahead_left > 0:
            self._lookahead_left -= 1
            rounds += 1
            run = self.s.run
            steps_before = len(run.path)
            try:
                self._offered = {"answer_step"}
                result = await self.llm.complete(self._messages(), [self._lookahead_tool(run.node)])
            except LLMError as e:
                logger.info(f"[{self.s.id}] look-ahead LLM failed: {e}")
                break
            timing["llm_ms"] += result.ms
            tc = next((c for c in result.tool_calls if c.name == "answer_step"), None)
            if not tc:
                break
            timing["tools"].append("answer_step")
            step = await self._execute(tc, presented_before)
            if len(run.path) > steps_before:
                # Keep successful skips in the history; "ask the caller" rounds would only crowd it.
                self.s.messages.append(LLMResult(tool_calls=[tc]).assistant_message())
                self.s.messages.append({"role": "tool", "tool_call_id": tc.id, "content": step.get("result", "ok")})
            if step.get("speak"):     # closed, rejected, or reached a step with no rounds left
                out = step
                break
            if not step.get("pending") or step.get("ask"):
                break
        timing["lookahead"] = timing.get("lookahead", 0) + rounds
        if out:
            return out
        return {"speak": self._present(self.s.run.node)}

    def _lookahead_tool(self, node: Node) -> dict:
        options = "; ".join(f"{o.id} = {o.label}" for o in node.outcomes.values())
        # In the eval the model mostly quoted the latest message even when the answer came first, so list them all.
        said = [m["content"] for m in self.s.messages[self._evidence_from:]
                if m["role"] == "user" and isinstance(m.get("content"), str)][-8:]
        listing = " | ".join(f'"{t[:240]}"' for t in said)
        # caller_said comes before outcome so the model writes the evidence before it picks.
        return _tool("answer_step",
                     f"The next step '{node.id}' has NOT been read to the caller yet: \"{node.text('en')}\" "
                     f"If the caller's own words anywhere in this call (not only their latest message) already answer this "
                     f"exact step, copy the shortest exact part of their words that answers it into caller_said, then "
                     f"choose the outcome those words mean. Otherwise choose {ASK_CALLER} and the step will be read to the "
                     f"caller. Options: {ASK_CALLER} = not answered yet; {options}. "
                     f"Everything the caller has said, oldest first: {listing}",
                     {"caller_said": {"type": "string",
                                      "description": "The caller's words that answer this step, copied exactly from their "
                                                     f"message (same script, not translated). Empty with {ASK_CALLER}."},
                      "outcome": {"type": "string", "enum": [ASK_CALLER, *node.outcomes]},
                      "language": {"type": "string", "enum": ["hi", "en"],
                                   "description": "Language of the caller's latest message"}},
                     ["caller_said", "outcome", "language"])

    async def _skip_confirmed(self, node: Node, outcome: str, quote: str) -> bool:
        """Classify the caller's words once more without the model's pick; the skip stands only if both
        agree. Uses the turn's extra-call budget; no budget, an error or a different pick means asking."""
        if self._lookahead_left <= 0:
            return False
        self._lookahead_left -= 1
        timing = self._timing
        timing["lookahead"] = timing.get("lookahead", 0) + 1
        options = [*node.outcomes, None]
        labels = [node.outcomes[o].label for o in node.outcomes] + [_NOT_ANSWERED]
        question = (f"The agent's question: \"{node.text('en')}\"\nThe caller said: \"{quote}\"\n"
                    "Which option matches what the caller said?\n"
                    + "\n".join(f"{chr(65 + i)}) {label}" for i, label in enumerate(labels)))
        try:
            result = await self.llm.complete([{"role": "system", "content": SKIP_CHECK_PROMPT},
                                              {"role": "user", "content": question}], [])
        except LLMError as e:
            logger.info(f"[{self.s.id}] skip check failed: {e}")
            return False
        timing["llm_ms"] = timing.get("llm_ms", 0) + result.ms
        timing.setdefault("tools", []).append("check_skip")
        m = re.match(r"\W*([A-Z])\b", (result.content or "").strip().upper())
        picked = options[ord(m.group(1)) - 65] if m and ord(m.group(1)) - 65 < len(options) else None
        if picked != outcome:
            logger.info(f"[{self.s.id}] skip check picked {picked!r}, model picked {outcome!r} at '{node.id}'")
        return picked == outcome

    def _mark_heard(self, node_id: str) -> None:
        """A step reached this turn was left unread because another tool spoke after it; count it as read."""
        run = self.s.run
        if run and run.current == node_id and node_id not in self.s.presented and not run.node.terminal:
            self._present(run.node)

    # ---- tools -----------------------------------------------------------------------
    def _tools(self) -> list[dict]:
        lang = {"type": "string", "enum": ["hi", "en"], "description": "Language of the caller's latest message"}
        tools = [_tool("say", "Say something in your own words: clarify, empathise or answer a question about the call. "
                              "Never a physical instruction.",
                       {"text": {"type": "string", "description": "One or two short spoken sentences"}, "language": lang},
                       ["text", "language"])]
        appliances = sorted({r["appliance"] for r in self.s.rentals} | {pb.appliance for pb in self.library.playbooks.values()})
        symptoms = sorted({sym for pb in self.library.playbooks.values() for sym in pb.symptoms} | {"unknown"})
        symptom_help = " | ".join(f"{pb.appliance}: " + ", ".join(f"{k} = {v}" for k, v in pb.symptoms.items())
                                  for pb in self.library.playbooks.values())
        start_params = {
            "appliance": {"type": "string", "enum": appliances},
            "symptom": {"type": "string", "enum": symptoms,
                        "description": f"What the caller already described (else 'unknown'). Options per appliance: {symptom_help}"},
            "language": lang,
        }
        if self.s.rentals:
            start_params["rental_id"] = {"type": "string", "enum": [r["id"] for r in self.s.rentals],
                                         "description": "Which rented unit (see CALL STATE)"}
        if not self.s.ticket:
            tools.append(_tool("start_troubleshooting", "Start the guided playbook for an appliance.", start_params,
                               ["appliance", "symptom", "language"]))
        else:
            tools.append(_tool("add_note", "Add something the caller mentions (like another faulty appliance) to the booked ticket.",
                               {"note": {"type": "string", "description": "Short note in English"}, "language": lang},
                               ["note", "language"]))
        run = self.s.run
        if run and not run.finished and not self.s.closed:
            node = run.node
            options = "; ".join(f"{o.id} = {o.label}" for o in node.outcomes.values())
            tools.append(_tool("answer_step", f"Record the caller's answer to the current step '{node.id}'. Options: {options}",
                               {"outcome": {"type": "string", "enum": list(node.outcomes)},
                                "caller_said": {"type": "string",
                                                "description": "Required only for a step not yet read to the caller: the "
                                                               "caller's earlier words that answer it, copied exactly"},
                                "language": lang},
                               ["outcome", "language"]))
            tools.append(_tool("explain_step", "Explain or repeat the current step in simpler words.",
                               {"language": lang}, ["language"]))
        tools.append(_tool("record_error_code", "Record an error code the caller read from the display.",
                           {"brand": {"type": "string"}, "code": {"type": "string"}, "language": lang},
                           ["code", "language"]))
        if not self.s.ticket:
            tools.append(_tool("escalate_to_technician", "Book a technician visit.",
                               {"reason": {"type": "string", "description": "Short reason in English"},
                                "caller_requested": {"type": "boolean"}, "language": lang},
                               ["reason", "language"]))
        if not self.s.handed_off:
            tools.append(_tool("handoff_to_human", "Hand the call to a person: the caller asks for a human, manager or "
                                                   "customer care (not just a technician visit), is angry or upset, or "
                                                   "the problem isn't covered by any playbook.",
                               {"reason": {"type": "string", "description": "Short reason in English"}, "language": lang},
                               ["reason", "language"]))
        if not self.s.audit_required:
            tools.append(_tool("flag_possible_misuse", "The damage may have come from how the item was used, or the caller "
                                                       "asks about charges. The Auditing team reviews it; never mention "
                                                       "charges or blame.",
                               {"evidence": {"type": "string", "description": "The caller's words, short"}, "language": lang},
                               ["evidence", "language"]))
        if not self.s.hazard:
            tools.append(_tool("flag_safety_concern", "The caller mentioned something possibly dangerous.",
                               {"kind": {"type": "string", "enum": list(HAZARD_KINDS)},
                                "evidence": {"type": "string"}, "language": lang},
                               ["kind", "language"]))
        if self.s.appliance and self.s.outcome == "in_progress":
            tools.append(_tool("mark_resolved", "The caller says the appliance works again.",
                               {"how": {"type": "string"}, "language": lang}, ["language"]))
        if self.settings.enable_doc_qa and self.docs:
            tools.append(_tool("lookup_docs", "Search the manuals for an informational question (not for instructions).",
                               {"query": {"type": "string"}, "language": lang}, ["query", "language"]))
        return tools

    async def _execute(self, tc: ToolCall, presented_before: set[str]) -> dict:
        args = tc.arguments
        lang = getattr(self, "_turn_language", None) or (
            args.get("language") if args.get("language") in ("hi", "en") else self.s.language)
        self.s.language = lang
        self.s.publish("tool", name=tc.name, arguments=args)
        offered = getattr(self, "_offered", None)
        if offered is not None and tc.name not in offered:
            # Live Bolna call: the model booked "a technician" after the urgent safety ticket existed, when
            # escalate_to_technician wasn't on offer. Only tools offered this turn may run.
            logger.info(f"[{self.s.id}] ignored {tc.name}: not offered this turn")
            return {"result": f"error: {tc.name} is not available now"}
        try:
            handler = getattr(self, f"_tool_{tc.name}", None)
            if not handler:
                return {"result": f"error: unknown tool {tc.name}"}
            out = await handler(args, presented_before)
            out.setdefault("language", lang)
            return out
        except PlaybookError as e:
            logger.info(f"[{self.s.id}] rejected {tc.name}: {e}")
            node = self.s.run.node if self.s.run else None
            speak = None
            if node and not node.terminal:
                heard = node.id in presented_before
                speak = f"{self._phrase('repeat')} {self._present(node)}" if heard else self._present(node)
            return {"result": f"rejected: {e}", "speak": speak}

    async def _tool_start_troubleshooting(self, args: dict, _presented: set[str]) -> dict:
        if self.s.ticket:
            raise PlaybookError("a ticket is already booked on this call; use add_note for another problem")
        if self.s.closed:   # a second problem after a phone fix
            self.s.closed, self.s.outcome, self.s.resolution, self.s.presented = False, "in_progress", None, set()
            self._evidence_from = max(0, len(self.s.messages) - 1)   # the first problem's answers don't carry over
        appliance = args.get("appliance")
        rental = self.s.rental_by_id(args.get("rental_id"))
        if not rental:
            matches = self.s.rentals_of(appliance)
            rental = matches[0] if len(matches) == 1 else None
        if rental:
            appliance = rental["appliance"]
        if (self.s.channel == "phone" and self.settings.identity_check and rental and not args.get("rental_id")
                and rental["id"] not in self.s.confirmed_rentals):
            # Call script 4: "Aapke paas {brand} {product} rent par hai. Usi ke baare mein baat kar rahe hain?"
            self.s.pending_start = {**args, "rental_id": rental["id"]}
            product = " ".join(p for p in (rental.get("brand"), rental["label"]) if p)
            return {"result": "asked the caller to confirm the product; wait for their answer",
                    "speak": self._phrase("product_one", product=product)}
        self.s.rental, self.s.appliance = rental, appliance
        playbook = self.library.for_appliance(appliance)
        if not playbook:
            prefix = self._phrase("no_playbook", appliance=appliance_name(appliance, self.s.language))
            closing = await self._escalate(likely_cause=f"No playbook for {appliance}; customer reported a problem",
                                           technician_note=None, reason=None)
            return {"result": "no playbook; technician booked", "speak": f"{prefix} {closing}", "closing": True}
        symptom = args.get("symptom")
        if self.s.pending_code and symptom in (None, "unknown") and "error_code" in playbook.symptoms:
            symptom = "error_code"
        self.s.run = PlaybookRun(playbook)
        symptom = None if symptom == "unknown" else symptom
        self.s.publish("playbook", playbook=playbook.id, title=playbook.title, rental=rental)
        triage = playbook.node(playbook.triage_node) if playbook.entry == playbook.triage_node else None
        if triage and symptom in triage.outcomes and triage.outcomes[symptom].hazard:
            # The issue the caller named is itself a safety case (KB MW-01 sparking): straight to the alert.
            self.s.run.advance(symptom)
            speech = await self._hazard(triage.outcomes[symptom].hazard, source="playbook",
                                        evidence=triage.outcomes[symptom].label)
            return {"result": "hazard handled", "speak": speech, "closing": True}
        node = self.s.run.start(symptom)
        if node.terminal:   # an issue that is always a ticket (KB "ACTION = TICKET"): book it now
            return await self._arrive(node)
        return {"result": f"started {playbook.id}; now at step {node.id}", "speak": self._present(node)}

    async def _tool_say(self, args: dict, _presented: set[str]) -> dict:
        return {"result": "said", "own": (args.get("text") or "").strip()}

    async def _tool_answer_step(self, args: dict, presented_before: set[str]) -> dict:
        run = self._require_run()
        node, outcome = run.node, args.get("outcome", "")
        quote = None
        if run.current not in presented_before:
            if outcome == ASK_CALLER:
                self._lookahead_left = 0
                return {"result": f"step '{node.id}' will be read to the caller", "pending": node.id, "ask": True}
            quote = self._caller_evidence(node, outcome, args.get("caller_said"))
            if not await self._skip_confirmed(node, outcome, quote):
                raise PlaybookError(f"couldn't confirm that {quote!r} means '{outcome}'; step '{node.id}' has to be asked")
        if _is_safety_question(node) and outcome in node.outcomes and not node.outcomes[outcome].hazard and not self._safety_denied():
            # The model may not infer "no hazard": the caller has to have said so.
            self.s.safety_rejects += 1
            return {"result": "rejected: the caller hasn't said there's no hazard; asked again",
                    "speak": f"{self._phrase('safety_confirm')} {node.text(self.s.language)}"}
        if (outcome in node.outcomes and node.outcomes[outcome].hazard == "water_electric"
                and not self._caller_named_electrics()):
            # "AC se paani leak ho raha hai" answers the safety question with the fault itself: water from the
            # appliance is a playbook fault unless it's near a plug, switch, board or wires. Ask, don't alarm.
            self.s.water_clarified = True
            return {"result": "rejected: the caller mentioned water but nothing electrical; asked where the water is",
                    "speak": self._phrase("water_where", appliance=appliance_name(self.s.appliance, self.s.language))}
        result = run.advance(outcome, caller_said=quote)
        if quote:
            logger.info(f"[{self.s.id}] skipped step '{node.id}' -> {outcome}: caller said {quote!r}")
            self.s.publish("step_skipped", id=node.id, outcome=outcome, label=node.outcomes[outcome].label, caller_said=quote)
        if result.hazard:
            speech = await self._hazard(result.hazard, source="playbook", evidence=run.path[-1]["label"])
            return {"result": f"hazard {result.hazard}", "speak": speech, "closing": True}
        return await self._arrive(result.node, defer=self._should_look_ahead(result.node))

    def _caller_evidence(self, node: Node, outcome: str, caller_said: str | None) -> str:
        """A step the caller hasn't heard may only be answered with their own words as evidence,
        and never if it's a safety question. Returns the caller's actual words."""
        if _is_safety_question(node):
            raise PlaybookError(f"'{node.id}' is a safety question; it has to be asked")
        if outcome not in node.outcomes:
            raise PlaybookError(f"'{outcome}' is not a valid answer at '{node.id}'; valid: {', '.join(node.outcomes)}")
        if node.outcomes[outcome].hazard:
            raise PlaybookError(f"'{outcome}' at '{node.id}' is a hazard; it has to be asked")
        if not (caller_said or "").strip():
            raise PlaybookError(f"step '{node.id}' hasn't been asked yet; pass caller_said with the caller's own words, or ask")
        users = [m["content"] for m in self.s.messages[self._evidence_from:]
                 if m["role"] == "user" and isinstance(m.get("content"), str)]
        quote = find_quote(caller_said, users)
        if not quote:
            raise PlaybookError(f"the caller didn't say {caller_said!r} on this call; step '{node.id}' has to be asked")
        if not _vocab([quote]) & self._telling_words(node):
            # Real words, but about something else: they'd be misleading evidence in the handoff.
            raise PlaybookError(f"{quote!r} isn't about step '{node.id}'; it has to be asked")
        return quote

    def _telling_words(self, node: Node) -> set[str]:
        """Words of `node` that steps already passed don't share: "switch" from the wall-switch
        answer says nothing about the MCB."""
        run = self.s.run
        passed = set().union(*(_node_vocab(run.playbook.node(p["node"])) for p in run.path)) if run.path else set()
        return _node_vocab(node) - passed

    def _should_look_ahead(self, node: Node | None) -> bool:
        """Worth an extra LLM round to see whether the caller already answered `node`? Only when rounds
        are left, it isn't a safety question, and the caller used one of its telling words (_telling_words)."""
        if (not node or node.terminal or self._lookahead_left <= 0 or _is_safety_question(node)
                or node.id in self.s.presented):   # a step heard before (the playbook looped back) is asked again
            return False
        caller = _vocab(m["content"] for m in self.s.messages[self._evidence_from:]
                        if m["role"] == "user" and isinstance(m.get("content"), str))
        return bool(self._telling_words(node) & caller)

    async def _tool_explain_step(self, args: dict, _presented: set[str]) -> dict:
        node = self._require_run().node
        help_text = node.help_text(self.s.language)
        return {"result": "explained", "speak": help_text or self._present(node)}

    async def _tool_record_error_code(self, args: dict, _presented: set[str]) -> dict:
        code = "".join(ch for ch in str(args.get("code", "")).upper() if ch.isalnum())
        self.s.error_code = {"brand": (args.get("brand") or "").strip() or None, "code": code,
                             "meaning": None, "source": None}
        self._lookup_code()
        self.s.publish("error_code", **self.s.error_code)
        noted = self._phrase("code_noted", code=" ".join(code))
        run = self.s.run
        if not run or run.finished:
            # No playbook yet: keep the code and route it once the appliance is known.
            self.s.pending_code = True
            return {"result": "code noted; find out the appliance and call start_troubleshooting with symptom error_code",
                    "speak": f"{noted} {self._phrase('which_appliance')}"}
        entry = run.playbook.node(run.playbook.entry)
        if run.current == entry.id and any(o.hazard for o in entry.outcomes.values()):
            # The safety question always comes first; the code is routed once it's answered.
            self.s.pending_code = True
            if "error_code" in run.playbook.symptoms:
                run.pending_symptom = "error_code"
            return {"result": "code noted; asking the safety question first", "speak": f"{noted} {self._present(run.node)}"}
        return await self._route_code(noted)

    def _lookup_code(self) -> dict | None:
        ec = self.s.error_code or {}
        brand = ec.get("brand") or (self.s.rental or {}).get("brand") or ""
        hit = self.library.lookup_code(self.s.appliance or "", brand, ec.get("code", "")) if brand and ec else None
        if ec:
            ec.update({"brand": brand or None, "meaning": hit["meaning"] if hit else None,
                       "source": hit["source"] if hit else None})
        return hit

    async def _route_code(self, noted: str) -> dict:
        hit = self._lookup_code()
        code = (self.s.error_code or {}).get("code", "")
        brand = (self.s.error_code or {}).get("brand") or ""
        run = self.s.run
        if hit and run and hit["route"] in run.playbook.routes and hit["route"] != "escalate":
            node = run.jump(hit["route"], f"{brand} {code}: {hit['meaning']}")
            arrived = await self._arrive(node)
            return {**arrived, "speak": f"{noted} {arrived['speak']}".strip(), "result": f"code routed to {node.id}"}
        # Unknown brand/code, AC codes, or codes that always need a technician.
        if run and "esc_error_code" in run.playbook.nodes and not run.finished:
            node = run.jump_to("esc_error_code", f"error code {code}")
            arrived = await self._arrive(node)
            return {**arrived, "speak": f"{noted} {arrived['speak']}".strip()}
        closing = await self._escalate(likely_cause=f"Error code {code} shown ({brand or 'brand unknown'})",
                                       technician_note="Look up the code for this model before the visit", reason=None)
        return {"result": "code recorded; technician booked", "speak": f"{noted} {closing}", "closing": True}

    async def _tool_escalate_to_technician(self, args: dict, _presented: set[str]) -> dict:
        run = self.s.run
        likely = "Customer asked for a technician" if args.get("caller_requested") else (args.get("reason") or "Needs a technician")
        if run and not run.finished:
            likely = f"{likely} (stopped at step '{run.current}')"
        closing = await self._escalate(likely_cause=likely, technician_note=None, reason=args.get("reason"))
        return {"result": "technician booked", "speak": closing, "closing": True}

    async def _tool_handoff_to_human(self, args: dict, _presented: set[str]) -> dict:
        speech = await self._handoff(args.get("reason") or "Caller needs a person")
        return {"result": "handed to a person", "speak": speech, "closing": True}

    async def _tool_flag_possible_misuse(self, args: dict, _presented: set[str]) -> dict:
        first = not self.s.audit_required
        self.tickets.flag_audit(self.s)
        self.s.publish("misuse", evidence=args.get("evidence") or "")
        return {"result": "flagged for the Auditing team; carry on with the playbook (never mention charges)",
                "prefix": self._phrase("misuse") if first else None}

    async def _tool_add_note(self, args: dict, _presented: set[str]) -> dict:
        note = (args.get("note") or "").strip()
        if self.s.ticket and note:
            self.tickets.add_note(self.s.ticket, note)
            self.s.publish("note", note=note)
        return {"result": "noted on the ticket; tell the caller the technician will look at it too"}

    async def _tool_flag_safety_concern(self, args: dict, _presented: set[str]) -> dict:
        kind = args.get("kind") if args.get("kind") in HAZARD_KINDS else "electrical"
        last = next((m["content"] for m in reversed(self.s.messages) if m["role"] == "user"), "") or ""
        if not self.s.safety_probed and not self.s.water_clarified and not _DANGER_WORD.search(last.lower()):
            # The caller said nothing dangerous-sounding; confirm once instead of raising a false alarm.
            self.s.safety_probed = True
            self.s.publish("safety_probe", kind=kind, evidence=args.get("evidence") or "")
            return {"result": "asked a safety question first; if the caller confirms danger, flag again",
                    "speak": self._phrase("safety_probe")}
        if kind == "water_electric" and not self._caller_named_electrics() and not self.s.water_clarified:
            self.s.water_clarified = True
            return {"result": "asked where the water is; flag again if it's near a plug, switch, board or wires",
                    "speak": self._phrase("water_where", appliance=appliance_name(self.s.appliance, self.s.language))}
        speech = await self._hazard(kind, source="llm", evidence=args.get("evidence") or "")
        return {"result": f"hazard {kind} handled", "speak": speech, "closing": True}

    async def _tool_mark_resolved(self, args: dict, _presented: set[str]) -> dict:
        node = None
        if self.s.run and "resolved" in self.s.run.playbook.nodes and not self.s.run.finished:
            node = self.s.run.jump_to("resolved", args.get("how") or "caller says it works again")
        if node:
            return await self._arrive(node)
        speech = await self._resolve("fixed_on_call", "")
        return {"result": "resolved", "speak": speech, "closing": True}

    async def _tool_lookup_docs(self, args: dict, _presented: set[str]) -> dict:
        hits = self.docs.search(args.get("query", ""), k=3, appliance=self.s.appliance) if self.docs else []
        self.s.publish("docs", query=args.get("query"), hits=[{k: h[k] for k in ("doc", "location", "url")} for h in hits])
        if not hits:
            return {"result": "No matching passage in the manuals. Say you don't have that information and offer a technician.",
                    "needs_llm": True}
        passages = "\n\n".join(f"[{h['title']} — {h['location']}]\n{h['text']}" for h in hits)
        return {"result": "Answer ONLY with facts from these passages, in one or two sentences, and give no "
                          f"physical instructions:\n{passages}", "needs_llm": True}

    # ---- outcomes --------------------------------------------------------------------
    async def _arrive(self, node: Node, defer: bool = False) -> dict:
        """Speak a new node, or finish the call if it's terminal. `defer` leaves a step unread for the
        look-ahead, which may find the caller already answered it."""
        if node.id == "error_code" and (self.s.pending_code or self.s.error_code):
            self.s.pending_code = False
            return await self._route_code("")
        if node.kind == "resolve":
            speech = await self._resolve(node.resolution or "fixed_on_call", node.text(self.s.language))
            return {"result": f"resolved ({node.resolution})", "speak": speech, "closing": True}
        if node.kind == "followup":
            text = node.text(self.s.language)
            name = appliance_name(self.s.appliance, "en")
            ticket = await self.tickets.create(self.s, priority="normal", category="selffix",
                                               likely_cause=f"{self._kb_issue() or 'Self-fix'} in progress (customer finishing the steps)",
                                               status="SELF_FIX_IN_PROGRESS", notify=False)
            if node.whatsapp:   # T2 selffix_steps_<issue>
                self._background(self.tickets.send_selffix_steps(self.s, node.whatsapp, ticket))
            self.tickets.schedule_followup(self.s, node.followup_minutes or 30, "check_back",
                                           f"{self.tenant.name} FixFirst: checking in on your {name}. Is it working now? "
                                           f"Reply YES, or NO and we'll book a technician.")
            self._close("followup")
            return {"result": "follow-up scheduled", "speak": f"{text} {self._phrase('anything_else')}", "closing": True}
        if node.kind == "escalate" and node.handoff:
            speech = await self._handoff(node.likely_cause or "Issue not in the KB")
            return {"result": "handed to a person", "speak": speech, "closing": True}
        if node.kind == "escalate":
            prefix = node.text(self.s.language)
            closing = await self._escalate(likely_cause=node.likely_cause or "Needs a technician",
                                           technician_note=node.technician_note, reason=None)
            return {"result": "technician booked", "speak": f"{prefix} {closing}".strip(), "closing": True}
        if defer:
            return {"result": f"now at step {node.id}, not read to the caller yet", "pending": node.id}
        return {"result": f"now at step {node.id}", "speak": self._present(node)}

    def _present(self, node: Node | None) -> str:
        if not node:
            return ""
        self.s.presented.add(node.id)
        self.s.publish("step", id=node.id, kind=node.kind, text=node.text(self.s.language),
                       playbook=self.s.run.playbook.id if self.s.run else None,
                       number=len(self.s.run.path) + 1 if self.s.run else None)
        return node.text(self.s.language)

    async def _hazard(self, kind: str, *, source: str, evidence: str) -> str:
        self.s.hazard = kind
        self.s.publish("safety", kind=kind, label=self.screen.label(kind), source=source, evidence=evidence)
        await self.tickets.create(self.s, priority="urgent", category="safety",
                                  likely_cause=f"SAFETY: {self.screen.label(kind)}",
                                  technician_note=f"URGENT: call back within {self.tenant.urgent_callback_minutes} min and "
                                                  f"visit the same day; detected by {source}: '{evidence}'",
                                  reason=None)
        self._close("safety")
        return self.screen.script(kind, self.s.language, self.tenant)

    async def _escalate(self, *, likely_cause: str, technician_note: str | None, reason: str | None) -> str:
        ticket = await self.tickets.create(self.s, priority="normal", category="technician",
                                           likely_cause=likely_cause, technician_note=technician_note, reason=reason)
        self._close("escalated")
        code = " ".join(ticket["code"].replace("-", ""))
        promise = self.tenant.normal_visit_promise.get(self.s.language, "")
        return self._phrase("reopened" if ticket.get("reopened") else "escalated", code=code, promise=promise)

    async def _resolve(self, resolution: str, text: str) -> str:
        self.s.resolution = resolution
        name = appliance_name(self.s.appliance, "en")
        # Self-fix worked: a RESOLVED_REMOTE ticket ("Main ticket close kar rahi hoon"), checked on WhatsApp at 24 h.
        await self.tickets.create(self.s, priority="normal", category="selffix",
                                  likely_cause=f"{self._kb_issue() or 'Problem'} fixed by the customer on the call",
                                  status="RESOLVED_REMOTE", notify=False)
        text = text or self._phrase("resolved_generic")
        self.tickets.schedule_followup(
            self.s, self.tenant.followup_hours * 60, "still_working",
            f"Hi {self.s.first_name or 'there'}, your {name} was fixed on the call with {self.tenant.name} FixFirst. "
            f"Is it still working fine? Reply YES, or NO and we'll book a technician.")
        self._close("resolved")
        return f"{text} {self._phrase('resolved_tail')}".strip()

    def _kb_issue(self) -> str | None:
        """The KB issue the caller is on (from the triage answer), like "AC-04 Weak airflow"."""
        run = self.s.run
        first = next((p for p in (run.path if run else []) if p["node"] == run.playbook.triage_node), None) if run else None
        label = (first or {}).get("label", "")
        m = re.match(r"([A-Z]{2}-\d\d [^(]*)", label)
        return m.group(1).strip() if m else None

    def _background(self, coro) -> None:
        """WhatsApp sends during a call: don't make the caller wait on Meta's API (tickets.notify_in_background)."""
        if self.tickets.notify_in_background:
            task = asyncio.create_task(coro)
            self.tickets._pending.add(task)
            task.add_done_callback(self.tickets._pending.discard)
        else:
            self._pending_sends.append(coro)

    def _close(self, outcome: str) -> None:
        self.s.outcome = outcome
        self.s.closed = True
        # Resolved, follow-up and technician closings all end with "anything else?" (the safety script doesn't).
        self.s.asked_anything_else = outcome != "safety"
        self.s.publish("outcome", outcome=outcome, resolution=self.s.resolution)
        self.tickets.persist_call(self.s)

    # ---- helpers ---------------------------------------------------------------------
    def _caller_named_electrics(self) -> bool:
        """Does the caller's latest message mention a plug, switch, socket, board or wiring?"""
        last = next((m["content"].lower() for m in reversed(self.s.messages)
                     if m["role"] == "user" and isinstance(m.get("content"), str)), "")
        return bool(self.screen.electric_re.search(last) or _SWITCHGEAR.search(last))

    def _safety_denied(self) -> bool:
        """Did the caller actually deny a hazard (now, or explicitly earlier in the call)?"""
        users = [m["content"].lower() for m in self.s.messages if m["role"] == "user" and isinstance(m.get("content"), str)]
        if not users:
            return False
        last = users[-1]
        if self.s.water_clarified and not self._caller_named_electrics():
            return True   # asked where the water is; it isn't near anything electrical
        if _STRONG_DENIAL.search(last):
            return True
        if _DENIAL.search(last) and (len(last.split()) <= 6 or _HAZARD_WORD.search(last)):
            return True
        if self.s.safety_rejects >= 2 and _DENIAL.search(last):
            return True   # asked twice already: don't trap the caller in a loop
        return any(_DENIAL.search(u) and _HAZARD_WORD_STRICT.search(u) for u in users[:-1])

    def _require_run(self) -> PlaybookRun:
        if not self.s.run:
            raise PlaybookError("no playbook is running; call start_troubleshooting first")
        return self.s.run

    def _guard(self, text: str | None) -> str | None:
        if not text:
            return None
        text = re.sub(r"[*#_`>\[\]]+", "", text).strip()
        bad = unsafe_instruction(text)
        if bad and not self.s.hazard:
            logger.warning(f"[{self.s.id}] guard blocked: {bad!r} in {text!r}")
            self.s.publish("guard", blocked=bad, text=text)
            return self._phrase("guarded")
        off_script = physical_instruction(text)
        if self.s.hazard and (bad or off_script):
            # The safety script has been read; any further instruction is that hazard's scripted reminder.
            self.s.publish("guard_hazard", text=text)
            return self._hazard_hold()
        node = self.s.run.node if self.s.run and not self.s.run.finished and not self.s.closed else None
        # The safety question is a question, not a step being carried out, so it is never "echoed".
        if (off_script and node and not node.terminal and node.id in self.s.presented
                and node.id != self.s.run.playbook.entry):
            words = _vocab([off_script])
            if words and len(words & _node_vocab(node)) >= 0.6 * len(words):
                # The model repeated the step the caller already heard (usually while they're doing it):
                # not an improvised instruction, but no need to read the whole step again either.
                self.s.publish("guard_echo", text=text)
                return self._phrase("take_your_time")
        if off_script:
            # Instructions only come from playbook steps: re-read the current step instead.
            logger.warning(f"[{self.s.id}] guard blocked an off-script instruction: {off_script!r}")
            self.s.publish("guard", blocked=off_script, text=text)
            if node and not node.terminal:
                return f"{self._phrase('steps_only')} {node.text(self.s.language)}"
            return self._phrase("offer_technician")
        if len(text) > 400:
            cut = max(text.rfind(p, 0, 400) for p in (". ", "। ", "? ", "! "))
            text = text[: cut + 1] if cut > 0 else text[:400]
        return text

    def _hazard_hold(self) -> str:
        reminder = self.screen.reminder(self.s.hazard, self.s.language, self.tenant)
        return self._phrase("hazard_hold", reminder=reminder, minutes=self.tenant.urgent_callback_minutes)

    def _phrase(self, key: str, **kw) -> str:
        template = PHRASES[key]["hi" if self.s.language == "hi" else "en"]
        return template.format(agent=self.tenant.agent_name, company=self.tenant.name, **kw)

    def _state(self) -> str:
        lines = []
        cust = self.s.customer
        if cust:
            lines.append(f"Customer: {cust['name']} ({cust.get('area') or 'area unknown'}), prefers "
                         f"{'Hindi' if cust.get('language') == 'hi' else 'English'}. The caller may be someone else "
                         f"in the home; that's fine.")
            lines.append("Rented appliances:")
            for r in self.s.rentals:
                lines.append(f"- {r['id']}: {r['label']} [{r['appliance']}]" + (f", {r['brand']}" if r.get("brand") else ""))
        else:
            lines.append(f"Unknown caller (phone {self.s.caller_phone or 'unknown'}); no rental record. Ask which appliance.")
        lines.append("Guided playbooks exist for: " + ", ".join(sorted(pb.appliance for pb in self.library.playbooks.values()))
                     + ". For anything else, start_troubleshooting books a technician.")
        run = self.s.run
        if run and not run.finished and not self.s.closed:
            node = run.node
            lines.append(f"Active playbook: {run.playbook.id}. Current step '{node.id}' (already read to the caller: "
                         f"{'yes' if node.id in self.s.presented else 'no'}): \"{node.text('en')}\"")
            lines.append("Valid answer_step outcomes: " + "; ".join(f"{o.id} = {o.label}" for o in node.outcomes.values()))
        else:
            lines.append("No active playbook step.")
        if self.s.error_code:
            lines.append(f"Error code recorded: {self.s.error_code['code']} ({self.s.error_code.get('brand') or 'brand unknown'}).")
        if self.s.hazard:
            lines.append(f"SAFETY ALERT ({self.s.hazard}) is active: the safety instructions were read and an URGENT "
                         f"ticket is raised; the team will call as soon as possible. Don't troubleshoot or "
                         "give instructions (questions about what to do with the appliance get a scripted reminder "
                         "automatically). Reply briefly and calmly, confirm the team will call, and say goodbye.")
        if self.s.audit_required:
            lines.append("Possible misuse is flagged for the Auditing team. Never mention charges, fault or blame.")
        if self.s.handed_off:
            lines.append("The call has been handed to a person (or a callback is promised). Reply briefly and kindly.")
        if self.s.ticket:
            lines.append(f"Ticket {self.s.ticket['code']} ({self.s.ticket['status']}) is booked. The call is wrapping up: "
                         "reassure, answer questions about next steps, and say goodbye. If the caller mentions another "
                         "problem, record it with add_note and say the technician will check it too.")
        elif self.s.closed:
            lines.append(f"This problem is closed ({self.s.outcome}). Say goodbye unless the caller has another problem.")
        return "\n".join(lines)

    def _messages(self) -> list[dict]:
        system = SYSTEM_PROMPT.format(agent=self.tenant.agent_name, company=self.tenant.name,
                                      promise=self.tenant.normal_visit_promise.get("en", ""), state=self._state())
        history = self.s.messages[-HISTORY_LIMIT:]
        while history and history[0]["role"] == "tool":   # never start mid tool exchange
            history = history[1:]
        return [{"role": "system", "content": system}, *history]

    def _finish_turn(self, speech: str, started: float, timing: dict) -> None:
        total_ms = int((time.monotonic() - started) * 1000)
        entry = {"turn": len(self.s.latency) + 1, "brain_ms": total_ms, "llm_ms": timing["llm_ms"],
                 "tools": timing["tools"]}
        if timing.get("lookahead"):
            entry["lookahead_rounds"] = timing["lookahead"]
        self.s.latency.append(entry)
        if speech:
            self.s.say("agent", speech, latency=entry)
        self.tickets.persist_call(self.s)


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": properties,
                                                            "required": required}}}
