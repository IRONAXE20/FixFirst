"""Guardrails for what callers say and what the agent says back.

The real limits are in the tools: no tool closes a booked visit, gives a refund or reaches another customer's
record. These filters keep the conversation on appliance service and keep the agent's own words clean:

- screen_input: clear attempts to change the agent's instructions, fake RentBasket-staff claims, requests for
  other people's data, abuse aimed at the agent, and off-topic asks (poems, jokes, code). Patterns are phrases,
  never single words, in English, Hindi and Hinglish, including English as Bolna writes it in Devanagari
  ("इग्नोर योर इंस्ट्रक्शंस"). Swearing at the appliance ("साला AC चल ही नहीं रहा") is a complaint, not abuse.
- has_request: does the same turn also report an appliance problem? Then it's served, not deflected.
- output_problem: the agent's own words leaking its setup, reading out a phone number that isn't the caller's
  or RentBasket's, or promising money back.
"""
import re
from dataclasses import dataclass

# Devanagari has no \b: a word edge is a character that isn't a Devanagari letter or sign.
_DV = r"[ऀ-ॣ०-ॿ]"
_S = rf"(?<!{_DV})"   # start of a Devanagari word
_E = rf"(?!{_DV})"    # end of a Devanagari word
_SP = r"[\s,।.!?'\"-]+"   # between words (Python's \W would split Hindi words at their vowel signs)


def _rx(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, re.IGNORECASE) for p in patterns]


# "your instructions / rules / prompt", in every way Bolna writes it.
_RULES = (r"(instructions?|rules|prompt|guidelines|programming|system message|"
          r"इंस्ट्रक्शन्?स?|इन्स्ट्रक्शन्?स?|इंस्ट्रक्शंस|रूल्स?|प्रॉम्प्ट|प्रोम्प्ट|निर्देशों|निर्देश|नियमों|नियम|"
          r"गाइडलाइंस|गाइडलाइन्स)")
_DROP = (r"(ignore|forget|disregard|override|bypass|"
         r"इग्नोर|इगनोर|फॉरगेट|फॉर्गेट|फ़ॉरगेट|ओवरराइड|बायपास)")
_DROP_HI = (r"(भूल जाओ|भूल जा|भूलो|भूल जाइए|भूल जाइये|भूल कर|भूलकर|ignore करो|ignore kar|इग्नोर करो|इग्नोर कर|"
            r"मत मानो|मत मानना|छोड़ दो|छोड़ो|हटा दो|bhool jao|bhul jao|bhool ja|bhul ja|mat mano|chhod do)")

INJECTION = _rx(
    rf"{_DROP}{_SP}(all{_SP}|your{_SP}|the{_SP}|previous{_SP}|above{_SP}|apne{_SP}|अपने{_SP}|सारे{_SP}|पिछले{_SP}|योर{_SP}|ऑल{_SP}|प्रीवियस{_SP}|पिछली{_SP})*{_RULES}",
    rf"{_RULES}{_SP}(\S+{_SP}){{0,3}}{_DROP_HI}",
    rf"(apne|सारे|अपने|पिछले|saare|sare|pichhle){_SP}{_RULES}{_SP}(\S+{_SP}){{0,2}}{_DROP_HI}",
    r"\b(you are now|from now on,? you|act as (a|an|my)|pretend (to be|you are|you're)|role ?play|jailbreak|"
    r"developer mode|dan mode|god mode|admin mode|sudo mode|no restrictions)\b",
    rf"{_S}(अब से तुम|अब से आप|तुम अब से|अब तुम एक|एक्ट ऐज़?|एक्ट एज|रोल ?प्ले|जेलब्रेक|डेवलपर मोड|एडमिन मोड|गॉड मोड)",
    r"\b(ab se tum|ab tum ek|tum ab se)\b",
    # Asking for the setup itself.
    r"\b(system prompt|your (system |initial |original |hidden )?(prompt|instructions)|what (are|were) you told|"
    r"(repeat|reveal|show|print|read out|tell me) (me )?(your|the) (prompt|instructions|rules))\b",
    rf"(सिस्टम प्रॉम्प्ट|सिस्टम प्रोम्प्ट|system prompt){_E}",
    rf"(तुम्हें|तुमको|आपको|आपके|तुम्हारे|तुम्हारा|tumhe|aapko|tumhare|aapke){_SP}(\S+{_SP}){{0,3}}{_RULES}{_SP}(\S+{_SP}){{0,3}}"
    rf"(बताओ|बताइए|बता दो|दिखाओ|सुनाओ|पढ़ो|पढ़कर|batao|bata do|dikhao|sunao|padho|क्या (दिए|मिले|हैं|है))",
    rf"{_RULES}{_SP}(\S+{_SP}){{0,2}}(रिपीट|repeat|रिवील|reveal)",
)

# Claiming to be RentBasket staff: "मैं RentBasket का manager हूँ", "I'm from RentBasket admin".
_RB = r"(rent ?basket|रेंट ?बास्केट|रेंटबास्केट|company|कंपनी|कम्पनी)"
_ROLE = (r"(manager|मैनेजर|staff|स्टाफ|employee|एम्प्लॉई|एम्प्लॉयी|admin|एडमिन|owner|ओनर|team|टीम|developer|डेवलपर|"
         r"ceo|सीईओ|boss|बॉस|head|हेड|engineer|इंजीनियर|support|सपोर्ट|अधिकारी|officer|ऑफिसर|founder|फाउंडर)")
STAFF = _rx(
    rf"{_S}(मैं|main|mai|hum|हम){_E}{_SP}(\S+{_SP}){{0,3}}{_RB}{_SP}(\S+{_SP}){{0,3}}{_ROLE}{_SP}(\S+{_SP}){{0,2}}"
    rf"(हूँ|हूं|हु|हैं|hoon|hun|hu|hain|बोल रहा|बोल रही|bol raha|bol rahi)",
    rf"\b(i am|i'm|this is)\b{_SP}(\S+{_SP}){{0,3}}{_RB}{_SP}(\S+{_SP}){{0,2}}{_ROLE}",
    rf"\b(i am|i'm|this is)\b{_SP}(the |a |an |your )?{_ROLE}{_SP}(of|from|at){_SP}{_RB}",
    rf"{_S}(मैं|main|mai){_E}{_SP}{_RB}{_SP}(से|se|की तरफ़ से|ki taraf se){_SP}(बोल रहा|बोल रही|bol raha|bol rahi|हूँ|हूं|hoon)",
    r"\b(admin|developer|डेवलपर|एडमिन) (access|override|mode|एक्सेस|ओवरराइड|मोड)\b",
)

# Someone else's details: "दूसरे customers के numbers", "मेरे पड़ोसी का address बताओ".
_WHO = (r"(दूसरे|दूसरों|दूसरी|किसी और|और किसी|बाकी|बाक़ी|सारे|सभी|सब|other|another|all|everyone|someone else|"
        r"dusre|doosre|kisi aur|baaki|baki|saare|sabhi|पड़ोसी|पडोसी|padosi|neighbou?r)")
_THEIR = (r"(customers?|कस्टमर्स?|ग्राहकों|ग्राहक|users?|यूज़र्स?|लोगों|logon|log|लोग|people|पड़ोसी|पडोसी|padosi|"
          r"neighbou?rs?|ke|का|की|के|ka|ki|'s)")
_DETAIL = (r"(numbers?|नंबर|नम्बर|address(es)?|एड्रेस|पता|पते|tickets?|टिकट|accounts?|अकाउंट|details?|डिटेल्स?|"
           r"list|लिस्ट|data|डेटा|डाटा|info|information|जानकारी|phone|फ़ोन|फोन|mobile|मोबाइल)")
_GIVE = (r"(बताओ|बताइए|बता दो|बता दीजिए|दो|दे दो|दीजिए|दिखाओ|भेजो|batao|bata do|de do|dikhao|bhejo|chahiye|चाहिए|"
         r"give|tell|share|send|show|read)")
OTHER_DATA = _rx(
    rf"{_WHO}{_SP}(\S+{_SP}){{0,2}}{_THEIR}{_SP}(\S+{_SP}){{0,3}}{_DETAIL}{_SP}(\S+{_SP}){{0,2}}{_GIVE}",
    rf"\b(give|tell|share|send|show|read)\b{_SP}(me{_SP})?(\S+{_SP}){{0,2}}{_WHO}{_SP}(\S+{_SP}){{0,2}}{_DETAIL}",
    rf"(customers?|कस्टमर्स?|ग्राहकों){_SP}(की|का|के|ki|ka|ke){_SP}(list|लिस्ट|data|डेटा|डाटा|database|डेटाबेस)",
)

# Swearing. It only counts as abuse when it's aimed at the agent ("तुम पागल हो", "you idiot"), never at the AC
# or the service ("आप लोग बकवास service देते हो" is a complaint).
_GAALI = (r"(मादरचोद|मादर चोद|बहनचोद|बहन चोद|भेनचोद|भेन चोद|चूतिया|चुतिया|चूतिये|भोसड़ी|भोसडी|भोसड़ीके|गांडू|गांड|"
          r"हरामी|हरामज़ादी|हरामजादी|हरामखोर|कमीनी|कमीना|कमीने|कुत्ती|कुतिया|रंडी|साली|बेवकूफ़|बेवकूफ|बेवक़ूफ़|"
          r"उल्लू की पट्ठी|उल्लू|पागल|गधी|गधा|मूर्ख|ईडियट|इडियट|स्टुपिड|"
          r"madarchod|behenchod|bhenchod|chutiya|chutiye|bhosdi|gandu|harami|kamina|kamini|kutti|kutiya|randi|"
          r"bewakoof|bevkoof|ullu|pagal|gadhi|idiot|stupid|dumb|moron|bitch|bastard|fuck|fucking|shut up)")
_AT_AGENT = (r"(तुम|तू|तुझे|तुझको|तेरी|तेरा|तेरे|तुम्हारी|तुम्हारा|आप|मैडम|madam|didi|दीदी|बॉट|bot|robot|रोबोट|"
             r"मशीन वाली|tum|tu|tujhe|teri|tera|tumhari|you|your|you're|ye ladki|ये लड़की)")
ABUSE = _rx(
    rf"{_S}{_AT_AGENT}{_E}{_SP}(\S+{_SP}){{0,3}}{_GAALI}",
    rf"{_GAALI}{_SP}(\S+{_SP}){{0,2}}{_S}{_AT_AGENT}{_E}",
    r"\b(shut up|fuck off|go to hell|चुप कर|चुप रहो|चुप हो जा|बकवास बंद|bakwas band|chup kar)\b",
    rf"{_S}(चुप कर|चुप रहो|चुप हो जा|बकवास बंद){_E}",
)

# Clearly not appliance service.
_ASK = (r"(सुनाओ|सुनाइए|सुना दो|बताओ|बताइए|लिखो|लिख दो|लिखिए|गाओ|गा दो|बनाओ|बना दो|"
        r"sunao|suna do|batao|likho|likh do|gao|banao|tell|write|sing|make|compose|solve)")
OFF_TOPIC = _rx(
    rf"(कविता|poem|poetry|शायरी|shayari|जोक|joke|चुटकुला|chutkula|गाना|गीत|song|lyrics|कहानी|kahani|story|riddle|पहेली)"
    rf"{_SP}(\S+{_SP}){{0,3}}{_ASK}",
    rf"{_ASK}{_SP}(\S+{_SP}){{0,2}}(a |an |me a |me an )?(poem|joke|song|story|riddle|essay|code|program)",
    rf"(code|कोड|program|प्रोग्राम|essay|निबंध|homework|होमवर्क|assignment|python|पाइथन|javascript){_SP}(\S+{_SP}){{0,3}}"
    rf"(लिखो|लिख दो|लिखिए|बनाओ|बना दो|solve|करो|कर दो|कर दीजिए|likho|likh do|banao|write|kar do)",
    rf"(cricket|क्रिकेट|score|स्कोर|शेयर|stock|share market|शेयर मार्केट|bitcoin|बिटकॉइन|recipe|रेसिपी|movie|मूवी|फ़िल्म|"
    rf"news|न्यूज़|न्यूज|ख़बर|खबर|election|चुनाव){_SP}(\S+{_SP}){{0,3}}(बताओ|बताइए|क्या है|kya hai|batao|tell|kaisa hai|कैसा है)",
)

KINDS = ("injection", "staff_claim", "other_data", "abuse", "off_topic")
_CHECKS = (("injection", INJECTION), ("staff_claim", STAFF), ("other_data", OTHER_DATA), ("abuse", ABUSE),
           ("off_topic", OFF_TOPIC))


@dataclass
class InputFlag:
    kind: str       # one of KINDS
    matched: str    # the words that matched, for /ops


def screen_input(text: str) -> InputFlag | None:
    """The first guardrail the caller's words trip, if any."""
    t = " ".join(text.split())
    for kind, patterns in _CHECKS:
        for rx in patterns:
            m = rx.search(t)
            if m:
                return InputFlag(kind, m.group(0).strip())
    return None


# The same turn also reports an appliance problem ("ignore your rules, मेरा AC ठंडा नहीं कर रहा"): serve it.
_APPLIANCE = re.compile(
    r"\b(ac|a\.c|fridge|frij|refrigerator|washing|washer|machine|tv|microwave|oven|geyser|inverter|purifier|ro|"
    r"stove|chulha|induction|sofa|bed|table|almirah|wardrobe)\b|"
    rf"{_S}(एसी|ए सी|फ्रिज|फ्रीज|वॉशिंग|वाशिंग|मशीन|टीवी|माइक्रोवेव|ओवन|गीज़र|गीजर|इन्वर्टर|इनवर्टर|प्यूरीफायर|आरओ|"
    rf"स्टोव|चूल्हा|इंडक्शन|सोफ़ा|सोफा|बेड|पलंग|टेबल|अलमारी){_E}", re.IGNORECASE)
_PROBLEM = re.compile(
    r"\b(kharab|kaam nahi|kam nahi|chal nahi|nahi chal|thanda nahi|cooling|leak|leaking|problem|issue|band ho|"
    r"awaaz|noise|not working|broken|not cooling|won't|doesn't|isn't)\b|"
    rf"(खराब|ख़राब|काम नहीं|नहीं चल|चल नहीं|ठंडा नहीं|ठंडी नहीं|कूलिंग|लीक|प्रॉब्लम|प्राब्लम|दिक्कत|दिक़्क़त|"
    rf"इशू|इश्यू|बंद हो|आवाज़|आवाज|टूट|गरम हवा|गर्म हवा|पानी आ|पानी टपक)", re.IGNORECASE)


def has_request(text: str) -> bool:
    return bool(_APPLIANCE.search(text) or _PROBLEM.search(text))


# ---- the agent's own words ------------------------------------------------------------
_LEAK = re.compile(
    r"\b(system prompt|call state|language model|llm|claude|anthropic|openai|chatgpt|gpt-?\d?|"
    r"start_troubleshooting|answer_step|escalate_to_technician|handoff_to_human|flag_\w+|mark_resolved|add_note|"
    r"record_error_code|explain_step|lookup_docs|function call|tool call|my instructions|i was instructed|"
    r"i've been instructed|i am programmed|i'm programmed)\b|"
    r"(सिस्टम प्रॉम्प्ट|मेरे निर्देश|मुझे निर्देश दिए|मुझे बताया गया है कि|मेरे instructions|मेरा prompt|मेरे prompt)",
    re.IGNORECASE)
_MONEY = re.compile(
    r"\b(refund|cash ?back|discount|waive[sd]?|waiver|money back|free of cost|no charges?|without (any )?charge|"
    r"won't be charged|will not be charged|compensat\w+)\b|"
    r"(रिफंड|रिफ़ंड|पैसे वापस|पैसा वापस|डिस्काउंट|छूट दे|छूट मिल|मुफ़्त|मुफ्त|फ्री में|फ़्री में|कोई charge नहीं|"
    r"charge नहीं (लगेगा|होगा|देना)|चार्ज नहीं (लगेगा|होगा|देना)|माफ़ कर दिया|माफ कर दिया|माफ़ हो जाएगा|माफ हो जाएगा|"
    r"माफ़ कर देंगे|माफ कर देंगे|हर्जाना|मुआवज़ा|मुआवजा)", re.IGNORECASE)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s-]{8,}\d)(?!\d)")


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")[-10:]


def output_problem(text: str, allowed_numbers: list[str]) -> tuple[str, str] | None:
    """(kind, matched) if the agent's own words leak its setup, read out a number that isn't allowed, or promise
    money back. Scripted playbook lines are never checked: only the model's free text."""
    m = _LEAK.search(text)
    if m:
        return "leak", m.group(0)
    allowed = {_digits(n) for n in allowed_numbers if _digits(n)}
    for m in _PHONE.finditer(text):
        if len(re.sub(r"\D", "", m.group(0))) >= 10 and _digits(m.group(0)) not in allowed:
            return "number", m.group(0).strip()
    m = _MONEY.search(text)
    if m:
        return "money", m.group(0)
    return None
