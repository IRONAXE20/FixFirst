"""RentBasket's Customer Service Ecosystem (29 Oct 2026): the voice call script, the ticket lifecycle, the
handoff, misuse and the customer's link."""
import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from fixfirst.db import followups, linked_numbers, outbox, tickets
from fixfirst.llm import LLMResult, call
from fixfirst.playbooks import get_library


def run(coro):
    return asyncio.run(coro)


def rows(engine, table):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(table))]


def kb(appliance, node, lang="hi"):
    return get_library().for_appliance(appliance).node(node).text(lang)


# ---- call script: identify, pick the product -------------------------------------------------
def test_known_caller_is_confirmed_by_name_and_area_then_the_product(env):
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="weak_airflow", language="hi")]),
    ], channel="phone", phone="+91 90000 00001")
    first = run(brain.respond("mere AC mein hawa kam aa rahi hai")).speech
    assert first == brain._phrase("identity_confirm", name="Ananya", area="Sector 52, Gurgaon")
    assert llm.requests == []                      # scripted: the model hasn't run yet
    product = run(brain.respond("haan ji")).speech  # the model now takes the problem from the first turn
    assert product == brain._phrase("product_one", product="Voltas Split AC 1.5 ton (bedroom)")
    assert run(brain.respond("haan wahi")).speech == kb("ac", "ac04_near")
    assert session.identity == "done" and session.rental["id"] == "R11"


def test_unregistered_caller_links_a_roommates_number_by_otp(env):
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("say", text="जी, बताइए किस appliance में problem है?", language="hi")]),
    ], customer=None, channel="phone", phone="+91 98111 22233")
    assert run(brain.respond("fridge kharab hai")).speech == brain._phrase("unregistered")
    assert run(brain.respond("haan, mere roommate ke number se hai")).speech == brain._phrase("ask_other_number")
    assert run(brain.respond("nau zero zero zero zero zero zero zero zero do")).speech == brain._phrase("otp_sent")
    otp = next(m for m in rows(env.engine, outbox) if m["kind"] == "T10a_otp_verify")
    code = otp["body"].split()[0]
    assert otp["to"] == "9000000002"
    assert run(brain.respond("123")).speech == brain._phrase("otp_retry") if code != "123" else True
    speech = run(brain.respond(" ".join(code))).speech
    assert speech.startswith(brain._phrase("otp_ok"))
    assert session.customer["id"] == "C2" and session.linked_by_otp
    assert rows(env.engine, linked_numbers)[0]["customer_id"] == "C2"
    # The next call from the same number is recognised straight away.
    assert env.store.create(channel="phone", caller_phone="+91 98111 22233").customer["id"] == "C2"


def test_otp_failing_twice_goes_to_a_person(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([], customer=None, channel="phone", phone="+91 98111 22233")
    run(brain.respond("AC kharab hai"))
    run(brain.respond("9000000001"))
    run(brain.respond("111111"))
    speech = run(brain.respond("222222")).speech
    assert speech.startswith(brain._phrase("otp_fail")) and session.handed_off
    assert session.ticket["category"] == "handoff" and "सुबह 9 बजे के बाद" in speech


# ---- handoff, misuse -------------------------------------------------------------------------
def test_asking_for_a_person_in_hours_transfers_the_call(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: True)
    brain, session, llm = env.make([], channel="phone", phone="+91 90000 00001")
    speech = run(brain.respond("mujhe kisi insaan se baat karni hai")).speech
    assert speech == brain._phrase("handoff_now") and session.transfer_requested and llm.requests == []
    assert session.ticket["status"] == "TICKET_RAISED" and session.ticket["category"] == "handoff"


def test_after_hours_handoff_promises_a_callback_after_9am(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("handoff_to_human", reason="caller is angry", language="hi")]),
    ])
    speech = run(brain.respond("bahut ho gaya, teen baar complaint kar chuka hoon"))
    assert "सुबह 9 बजे के बाद" in speech.speech and not session.transfer_requested


def test_issue_not_in_the_kb_goes_to_a_person(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="other", language="hi")]),
    ])
    run(brain.respond("AC ka remote kho gaya hai"))
    assert session.handed_off and session.ticket["summary"]["likely_cause"].startswith("Human handoff: AC issue not in")


def test_two_failed_understandings_go_to_a_person(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([LLMResult(), LLMResult()])
    assert run(brain.respond("hmm")).speech == brain._phrase("repeat")
    run(brain.respond("hmm hmm"))
    assert session.handed_off


def test_possible_misuse_is_flagged_for_audit_and_never_mentions_charges(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("flag_possible_misuse", evidence="dropped the microwave", language="hi"),
                              call("start_troubleshooting", appliance="microwave", symptom="no_power", language="hi")]),
    ], customer="C3")
    speech = run(brain.respond("microwave gir gaya tha, ab on nahi ho raha")).speech
    assert speech.startswith(brain._phrase("misuse"))
    assert session.ticket["audit_required"] is True
    assert rows(env.engine, tickets)[0]["audit_required"] is True
    assert "charge" not in speech.lower() and "paisa" not in speech.lower()


# ---- the ticket lifecycle ----------------------------------------------------------------------
def test_self_fix_on_whatsapp_is_a_self_fix_ticket_with_the_steps(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="not_near", language="hi")]),
    ])
    run(brain.respond("hawa kam aa rahi hai"))
    speech = run(brain.respond("abhi ghar par nahi hoon")).speech
    assert speech.startswith(kb("ac", "steps_on_whatsapp"))
    assert session.selffix_ticket["status"] == "SELF_FIX_IN_PROGRESS" and session.ticket is None
    steps = next(m for m in rows(env.engine, outbox) if m["kind"] == "selffix_AC-04")
    assert "Wash them with plain water" in steps["body"]
    # v1 templates: no approved Utility template carries steps until T2, so it's logged, not sent.
    assert steps["status"] == "no_template" and rows(env.engine, tickets)[0]["channels_used"] == "voice_only"


def test_without_t2_live_the_agent_books_a_ticket_instead_of_promising_whatsapp_steps(env, monkeypatch):
    from fixfirst.tickets import TicketService
    monkeypatch.setattr(TicketService, "can_send_steps", property(lambda self: False))
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="not_near", language="hi")]),
    ])
    run(brain.respond("hawa kam aa rahi hai"))
    speech = run(brain.respond("abhi ghar par nahi hoon")).speech
    assert "steps" not in speech and session.ticket["status"] == "TICKET_RAISED"
    assert session.ticket["summary"]["likely_cause"].startswith("AC-04")


def test_customer_ticket_message_uses_the_utility_template(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("fridge thanda nahi kar raha"))
    msg = next(m for m in rows(env.engine, outbox) if m["audience"] == "customer")
    assert msg["body"].startswith("Hi Ananya, your RentBasket service request FF-") and "[button: " in msg["body"]


def test_fridge_front_leak_steps_always_go_on_whatsapp(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="leak_front", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="not_near", language="hi")]),
    ])
    assert run(brain.respond("fridge ke aage se paani aa raha hai")).speech == kb("fridge", "fr04_plug")
    run(brain.respond("nahi, plug se door hai"))
    assert any(m["kind"] == "selffix_FR-04" for m in rows(env.engine, outbox))


def test_same_problem_within_7_days_reopens_the_ticket(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    first = session.ticket["code"]
    env.tickets.set_status(first, "FIXED")
    brain2, session2, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    speech = run(brain2.respond("AC phir se thanda nahi kar raha")).speech
    assert session2.ticket["code"] == first and session2.ticket["status"] == "REOPENED"
    assert speech.startswith(brain2._phrase("reopened", code=" ".join(first.replace("-", "")), promise="").split("{")[0][:20])
    assert len(rows(env.engine, tickets)) == 1


def test_an_old_fix_isnt_reopened(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    env.tickets.set_status(session.ticket["code"], "CLOSED")
    with env.engine.begin() as c:
        c.execute(update(tickets).values(updated_at=tickets.c.updated_at - timedelta(days=8)))
    brain2, session2, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    run(brain2.respond("AC thanda nahi kar raha"))
    assert session2.ticket["code"] != session.ticket["code"]


def test_fixed_by_the_team_schedules_the_24h_check(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    env.tickets.set_status(session.ticket["code"], "REPLACED")
    check = next(f for f in rows(env.engine, followups) if f["kind"] == "check_24h")
    assert session.ticket["code"] in check["body"]


def test_urgent_safety_is_an_urgent_ticket_with_the_1h_sla(env):
    brain, session, _ = env.make([])
    speech = run(brain.respond("AC se jalne ki smell aa rahi hai")).speech
    assert session.ticket["status"] == "URGENT"
    assert "मिनट" not in speech   # "जल्दी से जल्दी", not a minute count
    team = next(m for m in rows(env.engine, outbox) if m["audience"] == "team")
    assert "call back within 60 min and visit today" in team["body"]


def test_ecosystem_templates_send_t1_with_the_customer_link_button(env):
    env.settings.whatsapp_templates = "ecosystem"
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R23", symptom="not_cooling", language="en")]),
    ], customer="C2", language="en")
    run(brain.respond("my fridge isn't cooling"))
    t1 = next(m for m in rows(env.engine, outbox) if m["kind"] == "T1_technician_booked")
    assert "Welcome to Rentbasket" in t1["body"] and "+91 9958187021" in t1["body"] and "/u/" in t1["body"]


def test_a_yes_in_devanagari_moves_on_to_the_number(env, monkeypatch):
    # Live call 29 Oct: "यस।" wasn't understood, and the next "हाँ…" was counted as a second failure (handoff).
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([], customer=None, channel="phone", phone="+91 98111 22233")
    assert run(brain.respond("मेरा एसी ठीक से ठंडा नहीं कर रहा।")).speech == brain._phrase("unregistered")
    assert run(brain.respond("यस।")).speech == brain._phrase("ask_other_number")
    assert run(brain.respond("नौ शून्य शून्य शून्य शून्य शून्य शून्य शून्य शून्य एक")).speech == brain._phrase("otp_sent")
    assert not session.handed_off


def test_unclear_answers_still_hand_off_after_two_tries(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([], customer=None, channel="phone", phone="+91 98111 22233")
    run(brain.respond("AC kharab hai"))
    run(brain.respond("matlab"))
    run(brain.respond("pata nahi"))
    assert session.handed_off


def test_numbers_in_devanagari_digits_and_english_words_in_hindi_script():
    from fixfirst.brain import phone_in
    assert phone_in("९००००००००१") == "9000000001"
    assert phone_in("नाइन ज़ीरो ज़ीरो ज़ीरो ज़ीरो ज़ीरो ज़ीरो ज़ीरो ज़ीरो वन") == "9000000001"
