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
    wrong = "000000" if code != "000000" else "111111"
    assert run(brain.respond(wrong)).speech == brain._phrase("otp_retry")
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


def test_a_dropped_appliance_books_the_technician_for_audit(env):
    # Live call 30 Oct: "बच्चे ने गिरा दिया है" got "Microwave में क्या problem है?" again, and no ticket.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="microwave", symptom="unknown", language="hi")]),
        LLMResult(tool_calls=[call("flag_possible_misuse", evidence="child dropped it", physically_damaged=True,
                                   language="hi"),
                              call("say", text="ठीक है, Microwave में क्या problem है?", language="hi")]),
    ], customer="C3")
    run(brain.respond("microwave kaam nahi kar raha"))
    speech = run(brain.respond("bachche ne gira diya hai")).speech
    assert speech.startswith(f"{brain._phrase('misuse')} {brain._phrase('damaged')}")
    assert "क्या problem" not in speech
    assert session.ticket["status"] == "TICKET_RAISED" and session.ticket["audit_required"] is True
    assert "Physical damage" in session.ticket["summary"]["likely_cause"]


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


def test_same_appliance_within_30_days_of_a_fix_reopens_the_ticket_as_high_alert(env):
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
    assert len(rows(env.engine, tickets)) == 1 and rows(env.engine, tickets)[0]["high_alert"]


def test_an_old_fix_isnt_reopened(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    env.tickets.set_status(session.ticket["code"], "CLOSED")
    with env.engine.begin() as c:
        c.execute(update(tickets).values(updated_at=tickets.c.updated_at - timedelta(days=31)))
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


def test_a_number_and_otp_said_in_pieces_are_put_together(env, monkeypatch):
    # Live call 29 Oct: "99।" then "249।" each counted as a failed try, and the call went to a ticket.
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, _ = env.make([], customer=None, channel="phone", phone="+91 98111 22233")
    run(brain.respond("मेरा एसी ठंडा नहीं कर रहा।"))
    run(brain.respond("हाँ किसी और के नंबर से है।"))
    assert run(brain.respond("90।")).speech == brain._phrase("go_on")
    assert run(brain.respond("000।")).speech == brain._phrase("go_on")
    assert run(brain.respond("00002")).speech == brain._phrase("otp_sent")
    code = next(m for m in rows(env.engine, outbox) if m["kind"] == "T10a_otp_verify")["body"].split()[0]
    assert run(brain.respond(code[:3])).speech == brain._phrase("go_on")
    assert run(brain.respond(code[3:])).speech.startswith(brain._phrase("otp_ok"))
    assert session.customer["id"] == "C2" and not session.handed_off


def test_ecosystem_templates_send_named_parameters():
    # The ecosystem templates were approved with named variables ({{name}}…); the v1 ones are positional.
    import pytest

    from fixfirst.whatsapp import body_parameters
    p = body_parameters("technician_booked", ["Hardik", "Split AC", "FF-1009", "+91 9958187021", "https://x/u/t"])
    assert [x["parameter_name"] for x in p] == ["name", "product", "ticket_id", "care_number", "photo_link"]
    assert body_parameters("selffix_steps_ac04", ["Hardik"])[0]["parameter_name"] == "name"
    assert "parameter_name" not in body_parameters("fixfirst_service_request", ["a", "b", "c", "d"])[0]
    assert "parameter_name" not in body_parameters("otp_verify", ["123456"])[0]
    with pytest.raises(ValueError):
        body_parameters("urgent_safety", ["Hardik"])


def test_a_number_linked_by_otp_gets_t10b_on_the_ecosystem_set(env):
    env.settings.whatsapp_templates = "ecosystem"
    brain, session, _ = env.make([], customer=None, channel="phone", phone="+91 98111 22233")
    run(brain.respond("fridge kharab hai"))
    run(brain.respond("haan, mere roommate ke number se hai"))
    run(brain.respond("nau zero zero zero zero zero zero zero zero do"))
    code = next(m for m in rows(env.engine, outbox) if m["kind"] == "T10a_otp_verify")["body"].split()[0]
    run(brain.respond(code))
    linked = [m for m in rows(env.engine, outbox) if m["kind"] == "T10b_number_linked"]
    assert linked and "+91 9811122233" in linked[0]["body"] and linked[0]["to"] == "+91 98111 22233"


def test_a_second_appliance_on_the_call_goes_on_the_same_ticket(env):
    # Live call 30 Oct: AC ticket booked, then "washing machine bhi…" was only "noted". RentBasket: one ticket per
    # customer, one line per appliance.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", rental_id="R12", symptom="not_washing", language="hi")]),
    ])
    first = run(brain.respond("मेरा एसी ठीक से कूलिंग नहीं कर रहा।")).speech
    assert first.endswith(brain._phrase("anything_else"))
    code = session.ticket["code"]
    second = run(brain.respond("मेरी वाशिंग मशीन भी ढंग से कपड़े नहीं धो रही।")).speech
    assert second == brain._phrase("added", code=" ".join(code.replace("-", "")),
                                   promise=env.tenant.normal_visit_promise["hi"])
    [t] = rows(env.engine, tickets)
    assert t["code"] == code and t["status"] == "TICKET_RAISED" and not t["high_alert"]
    assert [i["appliance"] for i in t["summary"]["items"]] == ["ac", "washer"]
    t1 = [m for m in rows(env.engine, outbox) if m["kind"].startswith("ticket_") and m["audience"] == "customer"
          or m["kind"] == "T1_technician_booked"]
    assert len(t1) == 2   # one customer message per appliance booked, same ticket code
    team = [m for m in rows(env.engine, outbox) if m["audience"] == "team"][-1]["body"]
    assert "Appliances on this ticket:" in team


def test_one_appliance_fixed_on_the_call_and_one_booked_is_partially_resolved(env):
    brain, session, _ = env.make(
        [LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")])]
        + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in ("near", "done", "done", "done", "fixed")]
        + [LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="not_cooling", language="hi")])])
    for said in ("AC mein hawa kam aa rahi hai", "haan paas hoon", "band kar diya", "nikal diye", "dho diye",
                 "haan ab theek hai", "fridge bhi thanda nahi kar raha"):
        run(brain.respond(said))
    [t] = rows(env.engine, tickets)
    assert t["status"] == "PARTIALLY_RESOLVED"
    assert {i["appliance"]: i["result"] for i in t["summary"]["items"]} == {"ac": "self_fixed", "fridge": "booked"}


def test_a_new_appliance_on_a_later_call_joins_the_open_ticket_without_an_alert(env):
    brain, session, _ = _ac(env, "not_cooling")
    run(brain.respond("AC thanda nahi kar raha"))
    brain2, session2, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="not_cooling", language="hi")])])
    run(brain2.respond("fridge thanda nahi kar raha"))
    [t] = rows(env.engine, tickets)
    assert len(t["summary"]["items"]) == 2 and not t["high_alert"]


def test_a_no_to_another_question_or_cut_off_does_not_end_the_call(env):
    # Live call 30 Oct: the agent asked "आपका मतलब AC से है, सही समझा?" and "नहीं मैं बोल रहा था कि।" got the goodbye.
    from fixfirst.brain import caller_is_done
    assert not caller_is_done("नहीं मैं बोल रहा था कि।")
    assert caller_is_done("नहीं, धन्यवाद") and caller_is_done("बस इतना ही")
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("say", text="जी, आपका मतलब AC से है, सही समझा?", language="hi")]),
        LLMResult(tool_calls=[call("say", text="जी, बताइए किस appliance की बात कर रहे हैं?", language="hi")]),
    ])
    run(brain.respond("मेरा एसी ठीक से कूलिंग नहीं कर रहा।"))
    run(brain.respond("फेज भी ढंग से कूलिंग नहीं कर रहा।"))
    assert not session.end_requested
    run(brain.respond("नहीं"))
    assert not session.end_requested


def _ac(env, *steps):
    return env.make([LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom=steps[0],
                                                 language="hi")])]
                    + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in steps[1:]])


def test_calling_again_about_an_open_ticket_adds_to_it_as_high_alert(env):
    # RentBasket: one ticket per appliance; chasing an unfinished ticket makes it high alert for the team.
    brain, session, _ = _ac(env, "not_cooling")
    run(brain.respond("AC thanda nahi kar raha"))
    first = session.ticket["code"]
    env.tickets.set_status(first, "SCHEDULED")
    sent_before = len([m for m in rows(env.engine, outbox) if m["audience"] == "customer"])
    brain2, session2, _ = _ac(env, "not_cooling")
    speech = run(brain2.respond("AC abhi tak thanda nahi kar raha")).speech
    assert speech == brain2._phrase("already_open", code=" ".join(first.replace("-", "")))
    [t] = rows(env.engine, tickets)
    assert t["code"] == first and t["high_alert"] and t["status"] == "SCHEDULED" and len(t["summary"]["repeat_calls"]) == 1
    team = [m for m in rows(env.engine, outbox) if m["audience"] == "team"][-1]
    assert team["body"].startswith("HIGH ALERT")
    assert len([m for m in rows(env.engine, outbox) if m["audience"] == "customer"]) == sent_before


def test_a_self_fix_that_helps_partly_is_partially_resolved_and_booked(env):
    brain, session, _ = _ac(env, "weak_airflow", "near", "done", "done", "done", "partly")
    for said in ("AC mein hawa kam aa rahi hai", "haan paas hoon", "band kar diya", "nikal diye", "dho diye",
                 "thoda better hai par abhi bhi kam hai"):
        speech = run(brain.respond(said)).speech
    assert session.ticket["status"] == "PARTIALLY_RESOLVED"
    assert speech.endswith(brain._phrase("anything_else")) and "थोड़ा फर्क" in speech
    # The next call about the same AC goes on that ticket, as high alert.
    brain2, session2, _ = _ac(env, "weak_airflow")
    run(brain2.respond("AC mein abhi bhi hawa kam hai"))
    [t] = [t for t in rows(env.engine, tickets) if t["status"] not in ("RESOLVED_REMOTE",)]
    assert t["code"] == session.ticket["code"] and t["high_alert"]


def test_a_phone_call_sends_one_whatsapp_for_all_its_appliances_after_the_goodbye(env):
    # Live call 30 Oct: AC then washing machine sent two near-identical technician_booked messages.
    env.settings.whatsapp_templates, env.settings.identity_check = "ecosystem", False
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", rental_id="R12", symptom="not_washing", language="hi")]),
    ], channel="phone", phone="+91 90000 00001")
    run(brain.respond("मेरा एसी ठीक से कूलिंग नहीं कर रहा।"))
    run(brain.respond("मेरी वाशिंग मशीन भी ढंग से कपड़े नहीं धो रही।"))
    t1 = lambda: [m for m in rows(env.engine, outbox) if m["kind"] == "T1_technician_booked"]
    assert t1() == []                                   # held while the caller may report more
    run(brain.respond("बस, धन्यवाद"))
    [msg] = t1()
    assert "Split AC 1.5 ton (bedroom) and Fully automatic top-load washing machine 6.5 kg" in msg["body"]
    assert len([m for m in rows(env.engine, outbox) if m["audience"] == "team"]) == 1
    run(brain.on_hangup())
    assert len(t1()) == 1                               # not sent again at the hang-up


def test_a_hang_up_before_the_goodbye_still_sends_the_held_whatsapp(env):
    env.settings.identity_check = False
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ], channel="phone", phone="+91 90000 00001")
    run(brain.respond("AC thanda nahi kar raha"))
    assert not [m for m in rows(env.engine, outbox) if m["audience"] == "customer"]
    run(brain.on_hangup())
    assert [m for m in rows(env.engine, outbox) if m["audience"] == "customer"]


def test_an_appliance_started_after_a_ticket_joins_it_when_the_caller_hangs_up(env):
    # Live call 31 Oct: AC booked, then "फ्रिज भी खराब है", the fridge's first question, and a hang-up: the
    # WhatsApp said only "your AC" and the fridge never reached the ticket.
    env.settings.whatsapp_templates, env.settings.identity_check = "ecosystem", False
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="unknown", language="hi")]),
    ], channel="phone", phone="+91 90000 00001")
    run(brain.respond("AC thanda nahi kar raha"))
    run(brain.respond("fridge bhi kharab hai"))
    run(brain.on_hangup())
    [t] = rows(env.engine, tickets)
    assert [i["appliance"] for i in t["summary"]["items"]] == ["ac", "fridge"]
    [msg] = [m for m in rows(env.engine, outbox) if m["kind"] == "T1_technician_booked"]
    assert " and " in msg["body"] and session.outcome == "escalated"


def test_no_after_the_models_own_anything_more_question_gets_the_goodbye(env):
    # Live call 31 Oct: "क्या आप माइक्रोवेव के बारे में कुछ और बताना चाहते हैं?" "नहीं बस।" got no goodbye.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="microwave broken", language="hi")]),
        LLMResult(tool_calls=[call("say", text="क्या आप माइक्रोवेव के बारे में कुछ और बताना चाहते हैं?", language="hi")]),
    ], customer="C3")
    run(brain.respond("microwave kharab hai"))
    run(brain.respond("mebals"))
    run(brain.respond("नहीं बस।"))
    assert session.end_requested


def test_damage_said_before_the_appliance_is_known_still_books_the_visit(env):
    # Live call 31 Oct: "मेरे माइक्रोवेव को बच्चे ने गिरा दिया है" in one sentence: the flag came before the
    # microwave was started, so it ran the triage and handed off instead.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("flag_possible_misuse", evidence="child dropped the microwave", physically_damaged=True,
                                   language="hi"),
                              call("start_troubleshooting", appliance="microwave", symptom="unknown", language="hi")]),
    ], customer="C3")
    speech = run(brain.respond("mere microwave ko bachche ne gira diya hai")).speech
    assert speech.startswith(f"{brain._phrase('misuse')} {brain._phrase('damaged')}")
    assert session.ticket["audit_required"] and "Physical damage" in session.ticket["summary"]["likely_cause"]
    assert not session.handed_off and session.damage is None


def test_a_new_problem_on_an_appliance_that_only_had_self_fix_steps_books_its_visit(env):
    # Live call 30 Oct: AC filter steps sent to WhatsApp, fridge booked, then "AC भी कूलिंग नहीं कर रहा" was only
    # noted, so the AC line still said "customer doing the self-fix".
    brain, session, _ = env.make(
        [LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")])]
        + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in ("near", "done", "done", "later")]
        + [LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="not_cooling", language="hi")]),
           LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")])])
    for said in ("AC se hawa kam aa rahi hai", "haan saamne hoon", "kar diya", "nikal liye", "baad mein dhounga",
                 "fridge mein cooling nahi ho rahi", "AC bhi theek se cooling nahi kar raha"):
        speech = run(brain.respond(said)).speech
    [t] = rows(env.engine, tickets)
    assert {i["appliance"]: i["result"] for i in t["summary"]["items"]} == {"ac": "booked", "fridge": "booked"}
    assert speech.endswith(brain._phrase("anything_else"))


def test_a_note_is_spoken_back_to_the_caller(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("add_note", note="Also makes a noise", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    assert run(brain.respond("aur awaaz bhi karta hai")).speech == brain._phrase("noted")


def test_a_plain_no_with_a_danda_ends_the_call():
    # Live call 30 Oct: "नहीं।" after "और कुछ मदद चाहिए?" looped: the danda counted as a Devanagari letter.
    from fixfirst.brain import caller_is_done
    assert caller_is_done("नहीं।") and caller_is_done("नहीं") and caller_is_done("ना।")


def _selffix_phone_call(env, outcomes):
    env.settings.whatsapp_templates, env.settings.identity_check = "ecosystem", False
    return env.make(
        [LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")])]
        + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in outcomes],
        channel="phone", phone="+91 90000 00001")


def test_the_caller_can_carry_on_after_the_steps_went_to_whatsapp_and_then_none_are_sent(env):
    # Live calls 30 Oct: after "steps WhatsApp पर भेज रही हूँ", "फ़िल्टर साफ़ हो गए, अब आगे क्या?" got "समझ नहीं पाई",
    # and the steps went out although the AC was then fixed on the call.
    brain, session, _ = _selffix_phone_call(env, ("near", "done", "done", "later", "done", "fixed"))
    for said in ("AC se hawa kam aa rahi hai", "haan", "ho gaya", "ho gaya", "baad mein",
                 "filter saaf ho gaye, ab aage kya karna hai?"):
        speech = run(brain.respond(said)).speech
    assert speech == get_library().for_appliance("ac").node("ac04_refit").text("hi")
    run(brain.respond("haan ab theek chal raha hai"))
    run(brain.respond("नहीं।"))
    assert session.end_requested
    [t] = rows(env.engine, tickets)
    assert t["status"] == "RESOLVED_REMOTE"
    assert not [m for m in rows(env.engine, outbox) if m["kind"].startswith("T2_")]   # fixed: no steps sent
    assert not [f for f in rows(env.engine, followups) if f["kind"] == "check_back" and f["status"] == "pending"]


def test_steps_still_go_on_whatsapp_after_the_goodbye_when_the_caller_does_it_later(env):
    brain, session, _ = _selffix_phone_call(env, ("near", "done", "done", "later"))
    for said in ("AC se hawa kam aa rahi hai", "haan", "ho gaya", "ho gaya", "baad mein sukhaunga"):
        run(brain.respond(said))
    assert not [m for m in rows(env.engine, outbox) if m["kind"].startswith("T2_")]
    run(brain.respond("नहीं।"))
    assert [m for m in rows(env.engine, outbox) if m["kind"] == "T2_selffix_AC-04"]


def test_fixing_one_appliance_while_another_is_booked_doesnt_say_the_ticket_is_closed(env):
    # Live call 30 Oct: AC booked, washer fixed on the call, and the agent said "मैं ticket close कर रही हूँ".
    brain, session, _ = env.make(
        [LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", rental_id="R13", symptom="not_cooling", language="hi")]),
         LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")])]
        + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in ("near", "done", "done", "done", "fixed")])
    for said in ("fridge thanda nahi kar raha", "AC mein hawa kam hai", "haan", "kar diya", "nikal diye", "dho diye"):
        run(brain.respond(said))
    speech = run(brain.respond("haan ab theek hai")).speech
    assert "ticket close" not in speech and speech.startswith(brain._phrase("resolved_other", others="Single door fridge 190 L"))
    [t] = rows(env.engine, tickets)
    assert t["status"] == "PARTIALLY_RESOLVED"


def test_a_spark_after_a_fix_keeps_the_fix_on_the_ticket_line(env):
    # Live call 30 Oct: AC fixed on the call, then "socket से चिंगारी": the line became URGENT and "fixed" was lost.
    brain, session, _ = env.make(
        [LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")])]
        + [LLMResult(tool_calls=[call("answer_step", outcome=o, language="hi")]) for o in ("near", "done", "done", "done", "fixed")])
    for said in ("AC mein hawa kam hai", "haan", "kar diya", "nikal diye", "dho diye", "haan ab theek hai",
                 "aur AC ke socket se chingari aa rahi hai"):
        run(brain.respond(said))
    [t] = rows(env.engine, tickets)
    [line] = t["summary"]["items"]
    assert t["status"] == "URGENT" and line["result"] == "urgent" and line["history"][0]["result"] == "self_fixed"
    team = [m for m in rows(env.engine, outbox) if m["audience"] == "team"][-1]["body"]
    assert "earlier: fixed on the call" in team


def test_no_anything_else_after_a_short_go_on(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("say", text="जी, बताइए।", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    assert run(brain.respond("aur ek mera")).speech == "जी, बताइए।"


def test_going_to_the_appliance_in_a_minute_waits_and_offers_whatsapp(env):
    # User, 30 Oct: "दो मिनट में जाता हूँ" should get "पहुँच कर बता दीजिए, या WhatsApp पर भेज दूँ", not the steps on WhatsApp.
    ac = get_library().for_appliance("ac")
    brain, session, _ = _selffix_phone_call(env, ("going", "near"))
    run(brain.respond("AC se hawa kam aa rahi hai"))
    assert run(brain.respond("नहीं, दो मिनट में चला जाता हूँ")).speech == ac.node("ac04_wait").text("hi")
    assert not session.closed
    assert run(brain.respond("आ गया AC के पास")).speech == ac.node("ac04_off").text("hi")


def test_after_hours_handoff_then_a_tv_and_an_unlisted_ac_issue_all_go_on_one_ticket(env, monkeypatch):
    # Live call 30 Oct, 9 pm: "किसी इंसान से बात" got the after-hours callback, then "TV भी खराब" and "AC का remote"
    # were only "noted" in words, and the second handoff repeated "ticket raise कर दिया".
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: False)
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="tv", symptom="unknown", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="unknown", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="other", language="hi")]),
    ])
    first = run(brain.respond("किसी इंसान से बात हो सके"))
    assert "ticket raise" in first.speech and session.handed_off
    tools = {t["function"]["name"]: t for t in brain._tools()}
    assert "tv" in tools["start_troubleshooting"]["function"]["parameters"]["properties"]["appliance"]["enum"]
    run(brain.respond("मेरा TV भी खराब है"))
    run(brain.respond("और AC का remote काम नहीं कर रहा"))
    last = run(brain.respond("remote ke buttons kaam nahi karte")).speech
    assert last.startswith(brain._phrase("handoff_later_added", when="कल").split("।")[0]) or "जोड़ दिया" in last
    [t] = rows(env.engine, tickets)
    assert {i.get("appliance") for i in t["summary"]["items"]} >= {"tv", "ac"}
