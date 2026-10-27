"""Offline demo brain: keyword rules that drive the same playbooks when no Sarvam key is set.

It exists so the call page, tickets, WhatsApp outbox and ops view can be clicked through
before the key arrives. It is not the agent: it understands little beyond simple English
and Hinglish keywords, and it is used for typed calls only (voice always needs Sarvam).
"""
import re

from .llm import LLMResult, ToolCall

YES = r"\b(yes|yeah|yep|haan|han|ha|ji|done|ho gaya|ho gayi|kar diya|kar di|fixed|working|chal gaya|chal gayi|chalu|aa rahi|aa raha|theek|ok|okay|sahi|started|cold now|better)\b"
NO = r"\b(no|nope|nahi|nahin|nai|not|still|abhi bhi|phir bhi|same)\b"
APPLIANCES = [
    ("ac", r"\b(ac|a\.c|air ?condition\w*|aircon)\b"),
    ("washer", r"\b(washing|washer|machine)\b"),
    ("fridge", r"\b(fridge|refrigerator|frij|freezer)\b"),
    ("geyser", r"\b(geyser|gizer|water heater|hot water|garam paani)\b"),
    ("inverter", r"\b(inverter|battery)\b"),
    ("tv", r"\b(tv|television)\b"),
    ("microwave", r"\b(microwave|oven)\b"),
    ("stove", r"\b(stove|chulha|burner)\b"),
]
SYMPTOMS = [
    ("error_code", r"\b(code|error|display pe|shows? [a-z]?\d|[a-z]\d\b|\b(ie|oe|ue|4c|5c|dl|dc|pe|cl)\b)"),
    ("water_leak", r"\b(leak\w*|tapak\w*|drip\w*|dripping)\b"),
    ("not_draining", r"\b(drain\w*|paani nahi nikal|not emptying)\b"),
    ("not_filling", r"\b(not filling|paani nahi aa|no water|fill)\b"),
    ("weak_airflow", r"\b(low air|air is (very )?low|air'?s (very )?low|weak air\w*|hawa kam)\b"),
    ("shaking", r"\b(shak\w*|hil rahi|vibrat\w*|walking)\b"),
    ("not_spinning", r"\b(spin|ghoom\w*|not rotat\w*)\b"),
    ("no_heat", r"\b(not heating|garam nahi|no heat|doesn'?t heat)\b"),
    ("not_cooling", r"\b(not cool\w*|cooling|thand\w*|warm|garam|cold)\b"),
    ("not_starting", r"\b(not (turning|switching) on|won'?t (start|turn on)|on nahi|start nahi|chalu nahi|dead|band pada|not starting|no power)\b"),
]
TECHNICIAN = r"\b(technician|mechanic|send (someone|a person)|aadmi bhej\w*|koi bhej\w*|person|human|nahi hoga|can'?t do)\b"


class KeywordLLM:
    label = "offline demo brain"

    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMResult:
        names = {t["function"]["name"]: t["function"] for t in tools}
        text = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "") or ""
        low = text.lower()
        lang = "hi" if re.search(r"[ऀ-ॿ]|\b(nahi|haan|kar|raha|rahi|hai|mein|ki|ka)\b", low) else "en"

        def tool(name, **args):
            return LLMResult(tool_calls=[ToolCall(id=f"demo_{name}", name=name, arguments={**args, "language": lang})])

        if "escalate_to_technician" in names and re.search(TECHNICIAN, low):
            return tool("escalate_to_technician", reason=text[:120], caller_requested=True)
        if "answer_step" in names:
            fn = names["answer_step"]
            outcomes = fn["parameters"]["properties"]["outcome"]["enum"]
            node = re.search(r"current step '([^']+)'", fn["description"])
            if node and node.group(1) in ("error_code",) and "record_error_code" in names:
                code = _code(text)
                if code:
                    return tool("record_error_code", brand=_brand(low), code=code)
            choice = _pick(low, outcomes, fn["description"])
            if choice:
                return tool("answer_step", outcome=choice)
            return LLMResult(content="माफ़ कीजिए, थोड़ा और बताइए?" if lang == "hi" else "Sorry, could you tell me a bit more?")
        if "record_error_code" in names and _code(text) and re.search(r"\b(lg|samsung|whirlpool|ifb|bosch|voltas|daikin)\b", low):
            return tool("record_error_code", brand=_brand(low), code=_code(text))
        if "start_troubleshooting" in names:
            appliance = next((a for a, rx in APPLIANCES if re.search(rx, low)), None)
            if appliance:
                symptom = next((s for s, rx in SYMPTOMS if re.search(rx, low)), "unknown")
                props = names["start_troubleshooting"]["parameters"]["properties"]
                if symptom not in props["symptom"]["enum"]:
                    symptom = "unknown"
                return tool("start_troubleshooting", appliance=appliance, symptom=symptom)
            return LLMResult(content="किस appliance में problem है: AC, washing machine, fridge या कुछ और?" if lang == "hi"
                             else "Which appliance is giving trouble: the AC, washing machine, fridge or something else?")
        return LLMResult(content="ठीक है। और कुछ?" if lang == "hi" else "Alright. Anything else I can help with?")


def _pick(low: str, outcomes: list[str], description: str) -> str | None:
    labels = dict(re.findall(r"(\w+) = ([^;]+)", description))
    yes, no = bool(re.search(YES, low)), bool(re.search(NO, low))
    # Word overlap with the outcome labels handles specific answers ("remote is blank").
    words = set(re.findall(r"[a-z]{3,}", low))
    best, score = None, 0
    for o in outcomes:
        label_words = set(re.findall(r"[a-z]{3,}", (labels.get(o, "") + " " + o.replace("_", " ")).lower()))
        overlap = len(words & label_words)
        if overlap > score:
            best, score = o, overlap
    if score >= 2:
        return best
    prefer = (["no_hazard", "not_fixed", "no", "all_ok", "ok", "hose_ok", "already_open", "already_correct",
               "already_on", "all_up", "drain_ok", "light_on", "steady", "no_ice", "correct", "fan_spinning_clear"]
              if no and not yes else
              ["fixed", "yes", "started", "done", "was_wrong", "was_off", "was_closed", "was_kinked", "fixed_something",
               "one_down", "can_reach", "try", "cleared", "blocked_cleared", "will_defrost"] if yes else [])
    return next((o for o in prefer if o in outcomes), best if score else None)


KNOWN_CODES = {"ie", "oe", "ue", "ub", "4c", "4e", "5c", "5e", "dl", "dc", "de", "cl", "tcl", "pf", "fe", "e3", "vs", "us"}
COMMON_WORDS = {"LG", "AC", "TV", "OK", "MY", "IS", "IT", "THE", "AND", "ON", "RO", "IFB", "HI", "NO", "SO", "AN", "AT"}


def _code(text: str) -> str | None:
    """Prefer what the caller typed in capitals or with digits (IE, 4C, F05); 'pe' only after 'code/error'."""
    for tok in re.findall(r"\b[A-Za-z]{0,3}\d{0,2}[A-Za-z]?\b", text):
        if not tok or tok.upper() in COMMON_WORDS or len(tok) > 4:
            continue
        if (tok.isupper() and len(tok) >= 2) or (any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok)):
            return tok.upper()
    low = text.lower()
    for tok in re.findall(r"[a-z0-9]+", low):
        if tok in KNOWN_CODES:
            return tok.upper()
    if re.search(r"\b(code|error)\s+pe\b|\bpe\s+(code|error)\b", low):
        return "PE"
    return None


def _brand(low: str) -> str:
    m = re.search(r"\b(lg|samsung|whirlpool|ifb|bosch|voltas|daikin|godrej|haier|panasonic|blue ?star)\b", low)
    return m.group(1) if m else ""
