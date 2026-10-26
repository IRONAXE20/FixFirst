"""Deterministic hazard screen and safety scripts (knowledge/safety/hazards.yaml).

Runs on every caller turn before the LLM. It is tuned to over-trigger rather than miss:
a false alarm costs an urgent ticket, a miss can cost a life.
"""
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from .config import KNOWLEDGE_DIR, Tenant

HAZARD_KINDS = ("fire", "gas", "injury", "electrical", "battery", "water_electric", "stop_use")
_DEVANAGARI = "ऀ-ॿ"
# Sentences end at these; commas don't, so "no smell, sparks or smoke" keeps its "no".
_PUNCT = re.compile(r"[.!?;:।॥()\[\]\"“”‘’…\n]+")
_STRIP = ",'\"“”‘’"
# A verb between a hazard word and a negator means the hazard is being affirmed:
# "chingari aa rahi hai, AC nahi chal raha" is a spark report, not "no sparks".
_AFFIRM = {"aa", "aaya", "aayi", "aata", "aati", "raha", "rahi", "rahe", "hai", "hain", "ho", "hota", "hoti",
           "nikal", "nikla", "nikli", "nikalta", "lag", "laga", "lagi", "lagta", "lagti", "de", "deta", "deti",
           "dikh", "dikha", "dikhi", "is", "are", "was", "were", "there's", "theres", "coming", "comes", "came",
           "smells", "smelled", "got", "getting"}
_HAZARD_ING = {"burning", "sparking", "smoking", "leaking", "melting", "tingling"}


@dataclass
class Hazard:
    kind: str
    label: str
    matched: str


@dataclass
class _Pattern:
    kind: str
    regex: re.Pattern
    source: str


class SafetyScreen:
    def __init__(self, path: Path | None = None):
        cfg = yaml.safe_load((path or KNOWLEDGE_DIR / "safety" / "hazards.yaml").read_text(encoding="utf-8"))
        self.priority: list[str] = cfg["priority"]
        self.hazards: dict = cfg["hazards"]
        lex = cfg["lexicon"]
        self.patterns: list[_Pattern] = []
        for kind in HAZARD_KINDS:
            entry = lex.get(kind, {})
            for p in entry.get("latin", []):
                self.patterns.append(_Pattern(kind, re.compile(rf"(?<![a-z]){p}(?![a-z])"), p))
            for p in entry.get("devanagari", []):
                stem = p.endswith("*")
                body = re.escape(p.rstrip("*"))
                tail = "" if stem else rf"(?![{_DEVANAGARI}])"
                self.patterns.append(_Pattern(kind, re.compile(rf"(?<![{_DEVANAGARI}]){body}{tail}"), p))
            for p in entry.get("devanagari_regex", []):
                self.patterns.append(_Pattern(kind, re.compile(rf"(?<![{_DEVANAGARI}]){p}"), p))
        self.fuzzy = [(w, "electrical") for w in lex["electrical"].get("fuzzy", [])]
        gas = lex["gas"]
        self.refrigerant_ctx = re.compile(gas["refrigerant_context"])
        self.weak_flame_ctx = re.compile(lex["fire"]["weak_flame_context"])
        self.smell_words = re.compile(gas["smell_words"])
        we = lex["water_electric"]
        self.water_re = re.compile(we["water"])
        self.electric_re = re.compile(we["electric"])
        neg = cfg["negation"]
        self.not_negation = sorted(neg["not_negation"], key=len, reverse=True)
        # English negates before the noun ("no sparks"); Hindi after it ("spark nahi hai").
        self.neg_before = {"no", "not", "never", "without", "nothing", "don't", "dont", "doesn't", "doesnt",
                           "didn't", "didnt", "isn't", "isnt", "aren't", "arent", "haven't", "havent"}
        self.neg_after = {w for w in neg["words"] if w not in self.neg_before and " " not in w}
        self.clause_break = re.compile(
            r"(?<![a-z" + _DEVANAGARI + r"])(" + "|".join(re.escape(w) for w in neg["clause_breaks"]) +
            r")(?![a-z" + _DEVANAGARI + r"])")

    # ---- public API ------------------------------------------------------------------
    def screen(self, text: str, appliance: str | None = None) -> Hazard | None:
        """Return the highest-priority hazard mentioned (and not negated) in a caller turn."""
        found: dict[str, str] = {}
        for sentence in self._sentences(text):
            for clause in self._clauses(sentence):
                for kind, matched in self._scan_clause(clause, appliance):
                    found.setdefault(kind, matched)
            if "water_electric" not in found:
                hit = self._water_electric(sentence)
                if hit:
                    found["water_electric"] = hit
        for kind in self.priority:
            if kind in found:
                return Hazard(kind=kind, label=self.hazards[kind]["label"], matched=found[kind])
        return None

    def script(self, kind: str, language: str, tenant: Tenant) -> str:
        text = self.hazards[kind]["script"]["hi" if language == "hi" else "en"]
        nums = tenant.emergency_numbers
        return " ".join(text.split()).format(
            national=nums.get("national", "112"), fire=nums.get("fire", "101"), lpg=nums.get("lpg", "1906"),
            callback_minutes=tenant.urgent_callback_minutes, company=tenant.name)

    def reminder(self, kind: str, language: str, tenant: Tenant) -> str:
        """The short scripted reminder of what to do, for questions asked after the script was read."""
        text = self.hazards[kind]["reminder"]["hi" if language == "hi" else "en"]
        nums = tenant.emergency_numbers
        return " ".join(text.split()).format(national=nums.get("national", "112"), fire=nums.get("fire", "101"),
                                             lpg=nums.get("lpg", "1906"))

    def label(self, kind: str) -> str:
        return self.hazards[kind]["label"]

    # ---- internals -------------------------------------------------------------------
    def _normalize(self, text: str) -> str:
        text = text.lower().replace("’", "'")
        for phrase in self.not_negation:
            text = text.replace(phrase, " unsure ")
        return text

    def _sentences(self, text: str) -> list[str]:
        return [s.strip() for s in _PUNCT.split(self._normalize(text)) if s.strip()]

    def _clauses(self, sentence: str) -> list[str]:
        parts = self.clause_break.split(sentence)
        return [p.strip() for i, p in enumerate(parts) if i % 2 == 0 and p.strip()]

    def _negated(self, clause: str, start: int, end: int) -> bool:
        """English negates before the hazard ("no burning smell, sparks or smoke"), Hindi after it
        ("smell, chingari kuch nahi hai"). Either scan stops at an affirming verb."""
        raw = clause[:start].split()[-8:]
        before = [w.strip(_STRIP) for w in raw]
        for i in range(len(before) - 1, -1, -1):
            w = before[i]
            if w in self.neg_before:
                return True
            # Hindi "na X, na Y" (neither/nor) negates forward; "hai na" (no comma between) is a question tag.
            if w in ("na", "न") and (i == 0 or raw[i - 1].endswith(",")
                                    or before[i - 1] not in ("hai", "hain", "tha", "thi", "ho", "na")
                                    or "na" in before[:i - 1] or "न" in before[:i - 1]):   # "na sparks hain na smoke"
                return True
            if w in _AFFIRM or (w.endswith("ing") and w not in _HAZARD_ING):
                break
        after = [w.strip(_STRIP) for w in clause[end:].split()][:8]
        for w in after:
            if w in self.neg_after:
                return True
            if w in _AFFIRM:
                break
        return False

    def _scan_clause(self, clause: str, appliance: str | None):
        for p in self.patterns:
            for m in p.regex.finditer(clause):
                if self._negated(clause, m.start(), m.end()):
                    continue
                if p.kind == "gas" and self._is_refrigerant_talk(clause, appliance):
                    continue
                if p.kind == "fire" and self._is_weak_flame(clause, m.group(0)):
                    continue
                if p.kind == "gas" and appliance == "inverter" and re.search(r"egg|and[ae]", m.group(0)):
                    # A rotten-egg smell next to an inverter is the battery, not LPG.
                    yield "battery", m.group(0)
                    break
                yield p.kind, m.group(0)
                break
        tokens = [t.strip(_STRIP) for t in clause.split()]
        for i, tok in enumerate(tokens):
            for word, kind in self.fuzzy:
                if len(tok) >= 6 and tok != word and _within_one_edit(tok, word):
                    start = clause.find(tok)
                    if not self._negated(clause, start, start + len(tok)):
                        yield kind, tok

    def _is_weak_flame(self, clause: str, matched: str) -> bool:
        """A low, yellow or uneven stove flame is a stove fault, not a fire."""
        return matched.startswith(("flame", "lapat", "लपट")) and bool(self.weak_flame_ctx.search(clause))

    def _is_refrigerant_talk(self, clause: str, appliance: str | None) -> bool:
        if self.smell_words.search(clause):
            return False
        return bool(self.refrigerant_ctx.search(clause)) or appliance in ("ac", "fridge")

    def _water_electric(self, sentence: str) -> str | None:
        """A water word and an electrical word in the same comma-separated part; negation is read
        from the whole clause, so "no smoke or water near the plug" stays negated."""
        clauses = [(sentence.find(c), c) for c in self._clauses(sentence)]
        offset = 0
        for part in sentence.split(","):
            electric = self.electric_re.search(part)
            if electric:
                for m in self.water_re.finditer(part):
                    pos = offset + m.start()
                    owner = next(((s, c) for s, c in clauses if s <= pos < s + len(c)), None)
                    if owner and not self._negated(owner[1], pos - owner[0], pos - owner[0] + len(m.group(0))):
                        return f"{m.group(0)} … {electric.group(0)}"
            offset += len(part) + 1
        return None


def _within_one_edit(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) <= 1
    if len(a) > len(b):
        a, b = b, a
    i = j = diff = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diff += 1
            if diff > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


@lru_cache
def get_screen() -> SafetyScreen:
    return SafetyScreen()


# ---- free-text guard -------------------------------------------------------------------
# The LLM may chat, clarify and acknowledge, but physical instructions must come from
# playbook scripts. This catches the dangerous ones if it improvises anyway.
_UNSAFE_INSTRUCTIONS = [
    r"(open|remove|unscrew|take off|pry)\b.{0,25}\b(cover|panel|back|casing|body|board|pcb|lid of the (unit|compressor))",
    r"(cover|panel|casing|back|body)\b.{0,15}\b(khol|kholo|kholiye|kholein|nikal|utaar|utar)",
    r"\b(screw ?driver|pechkas|pana|plier)",
    r"(touch|join|connect|cut|strip|twist)\b.{0,15}\b(wire|wires|terminal|capacitor)",
    r"(taar|tar|wire)\b.{0,15}\b(jodo|jodiye|chhuo|chhuiye|kaato|kaatiye|nikaal)",
    r"\b(refill|recharge|top ?up)\b.{0,15}\b(gas|refrigerant)",
    r"\b(bypass|short)\b.{0,15}\b(switch|fuse|sensor|thermostat|mcb)",
    r"(reset|press)\b.{0,20}\b(thermostat|cut-?out)\b.{0,20}\b(button|inside)",
    r"(tape|insulation tape|cello ?tape)\b",
    r"(कवर|पैनल)\s*(खोल|निकाल)",
    r"(तार)\s*(जोड़|छू|काट)",
    r"पेचकस",
]
_UNSAFE_RE = [re.compile(p, re.IGNORECASE) for p in _UNSAFE_INSTRUCTIONS]


# Any instruction about a part of the appliance or the home's wiring, in the model's own words.
# Playbook steps are spoken from the scripts; the model's free text may ask and explain, not instruct.
_IMPERATIVE = re.compile(
    r"[\u0900-\u097F]+(?:िए|ीजिए|ाइए|इए)(?![\u0900-\u097F])"
    r"|\b[a-z]+(?:iye|ijiye|aiye)\b"
    r"|\b(?:check|press|push|open|unplug|plug in|switch (?:it )?(?:on|off)|turn (?:it )?(?:on|off)|clean|wash|remove|reset|restart|hold|tighten|wipe|lift|replace|insert|reconnect)\b",
    re.IGNORECASE)
_IMPERATIVE_OK = {"बताइए", "बोलिए", "रुकिए", "सुनिए", "कहिए", "चाहिए", "bataiye", "boliye", "rukiye", "suniye", "kahiye", "chahiye"}
_ASKING = re.compile(r"बता|bata|tell me|let me know", re.IGNORECASE)
_PART = re.compile(
    r"\b(?:plug|socket|switch|mcb|breaker|remote|filters?|button|tap|hose|pipe|panel|batter(?:y|ies)|power|mode|"
    r"temperature|door|lid|knob|stabili[sz]er|drain|cable|wires?|fuse|settings?|cover|stool)\b"
    r"|प्लग|सॉकेट|स्विच|बटन|रिमोट|फ़िल्टर|फिल्टर|नल|पाइप|पैनल|बैटरी|ढक्कन|दरवाज़|तार|स्टेबलाइज़र", re.IGNORECASE)


def physical_instruction(text: str) -> str | None:
    """Return the sentence if free LLM text tells the caller to do something to the appliance."""
    for sentence in re.split(r"[।.!?\n]+", text):
        if not _PART.search(sentence) or _ASKING.search(sentence):
            continue
        for m in _IMPERATIVE.finditer(sentence):
            if m.group(0).lower() not in _IMPERATIVE_OK:
                return sentence.strip()
    return None


def unsafe_instruction(text: str) -> str | None:
    """Return the offending fragment if free LLM text gives a disallowed physical instruction."""
    for rx in _UNSAFE_RE:
        m = rx.search(text)
        if m:
            return m.group(0)
    return None
