import asyncio

import pytest
from sqlalchemy import select

from fixfirst.brain import (LOOKAHEAD_ROUNDS, _node_vocab, _vocab, caller_is_done, detect_language, find_quote,
                            technician_requested)

from fixfirst.config import KNOWLEDGE_DIR
from fixfirst.db import customers, followups, outbox, tickets
from fixfirst.llm import LLMError, LLMResult, call
from fixfirst.playbooks import PlaybookLibrary, get_library

from conftest import SOP


def run(coro):
    return asyncio.run(coro)


def ac_text(node_id, lang="hi"):
    """A step of the archived SOP AC playbook (the engine tests' long flow)."""
    return SOP.for_appliance("ac").node(node_id).text(lang)


def kb_text(appliance, node_id, lang="hi"):
    """A step of the live RentBasket KB playbook."""
    return get_library().for_appliance(appliance).node(node_id).text(lang)


def rows(engine, table):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(table))]


def test_greeting_is_the_rentbasket_script(env):
    brain, session, _ = env.make([])
    text = brain.greeting()
    assert "FixFirst service assistant" in text and text.endswith("आपकी क्या मदद कर सकती हूँ?")
    assert session.transcript[0]["role"] == "agent"


def test_gas_smell_skips_the_llm_and_books_urgent_ticket(env):
    brain, session, llm = env.make([])
    result = run(brain.respond("kitchen mein gas ki badbu aa rahi hai"))
    assert result.hazard == "gas"
    assert "1906" in result.speech and "regulator" in result.speech
    assert llm.requests == []
    assert session.outcome == "safety" and session.ticket["priority"] == "urgent"
    sent = rows(env.engine, outbox)
    assert {m["audience"] for m in sent} == {"team", "customer"}
    assert all(m["status"] in ("dry_run", "no_recipient") for m in sent)


def test_full_phone_fix_flow(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="remote_blank", language="hi")]),
        LLMResult(content="बढ़िया!", tool_calls=[call("answer_step", outcome="fixed", language="hi")]),
    ])
    assert run(brain.respond("AC on hi nahi ho raha")).speech == ac_text("safety_check")
    assert run(brain.respond("nahi, aisa kuch nahi")).speech == ac_text("ac07_remote")
    assert run(brain.respond("remote ki screen blank hai")).speech == ac_text("ac07_batteries")
    final = run(brain.respond("haan chal gaya")).speech
    assert final.startswith("बढ़िया!") and ac_text("resolved") in final and final.endswith(brain._phrase("anything_else"))
    assert session.selffix_ticket["status"] == "RESOLVED_REMOTE" and session.ticket is None
    assert session.outcome == "resolved" and session.ticket is None
    assert [p["node"] for p in session.run.path] == ["safety_check", "triage", "ac07_remote", "ac07_batteries"]
    assert rows(env.engine, followups)[0]["kind"] == "still_working"
    assert len(session.latency) == 4


def test_answer_to_an_unheard_step_is_rejected(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_cooling", language="hi"),
                              call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    result = run(brain.respond("AC thanda nahi kar raha"))
    assert result.speech == ac_text("safety_check")
    assert session.run.current == "safety_check"


def test_invalid_outcome_repeats_the_step(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_cooling", language="en")]),
        LLMResult(tool_calls=[call("answer_step", outcome="made_up", language="en")]),
    ], language="en")
    run(brain.respond("my AC isn't cooling"))
    again = run(brain.respond("hmm"))
    assert again.speech.endswith(ac_text("safety_check", "en")) and again.speech != ac_text("safety_check", "en")


def test_guard_blocks_improvised_repair_advice(env):
    brain, session, _ = env.make([LLMResult(content="Open the back cover and check the capacitor.")], language="en")
    result = run(brain.respond("what else can I try?"))
    assert "technician" in result.speech and "capacitor" not in result.speech
    assert any(e["type"] == "guard" for e in session.events)


def test_repeated_llm_failure_falls_back_to_a_ticket(env):
    brain, session, _ = env.make([LLMError("timeout"), LLMError("timeout")], language="en")
    first = run(brain.respond("my fridge is not cooling"))
    assert "again" in first.speech and session.ticket is None
    second = run(brain.respond("my fridge is not cooling"))
    assert session.ticket and session.ticket["category"] == "system"
    assert "technical" in second.speech


def test_washer_code_is_recorded_after_the_safety_question_and_never_interpreted(env):
    env.library = SOP
    # RentBasket SOP WM-11: record the code exactly; don't guess its meaning from another model.
    washer = SOP.for_appliance("washer")
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", rental_id="R12", symptom="error_code", language="hi")]),
        LLMResult(tool_calls=[call("record_error_code", brand="Whirlpool", code="F05", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    assert run(brain.respond("machine pe F05 aa raha hai")).speech == washer.node("safety_check").text("hi")
    noted = run(brain.respond("Whirlpool ki hai")).speech
    assert "F 0 5" in noted and noted.endswith(washer.node("safety_check").text("hi"))
    run(brain.respond("nahi, paani nahi hai plug ke paas"))
    assert session.outcome == "escalated" and session.error_code["meaning"] is None
    assert session.ticket["summary"]["likely_cause"].startswith("WM-11 WM_ERROR_CODE")


def test_unknown_code_is_recorded_not_interpreted(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", rental_id="R32", symptom="error_code", language="en")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="en")]),
        LLMResult(tool_calls=[call("record_error_code", brand="Whirlpool", code="F05", language="en")]),
    ], customer="C3", language="en")
    run(brain.respond("my washing machine shows F05"))
    run(brain.respond("no, nothing like that"))
    result = run(brain.respond("it's a Whirlpool, F05"))
    assert session.outcome == "escalated" and session.error_code["meaning"] is None
    team = next(m for m in rows(env.engine, outbox) if m["audience"] == "team")
    assert "F05 (not interpreted)" in team["body"]
    assert "F F 1" in result.speech


def test_escalation_sends_photo_link_and_spoken_ticket_number(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="Caller wants a technician", caller_requested=True, language="en")]),
    ], customer="C2", language="en")
    result = run(brain.respond("Just send a technician please"))
    ticket = rows(env.engine, tickets)[0]
    assert ticket["priority"] == "normal" and ticket["code"].startswith("FF-")
    assert " ".join(ticket["code"].replace("-", "")) in result.speech
    customer_msg = next(m for m in rows(env.engine, outbox) if m["audience"] == "customer")
    token = next(c["link_token"] for c in rows(env.engine, customers) if c["id"] == "C2")
    assert token and f"/u/{token}" in customer_msg["body"]   # the customer's one persistent link


def test_appliance_without_playbook_books_technician(env):
    # RentBasket's SOP covers AC, fridge, washing machine and microwave only.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="inverter", rental_id="R24", symptom="unknown", language="en")]),
    ], customer="C2", language="en")
    result = run(brain.respond("my inverter backup is very low"))
    assert "inverter" in result.speech and session.outcome == "escalated"


def test_kb_ticket_issue_is_booked_at_once_without_any_fix(env):
    # RentBasket KB: AC-01 "Not cooling" is ACTION = TICKET: no steps, no fix suggested.
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
    ])
    speech = run(brain.respond("AC thanda nahi kar raha")).speech
    assert session.outcome == "escalated" and len(llm.requests) == 1
    assert speech.startswith("मैंने आपका ticket raise कर दिया है") and speech.endswith(brain._phrase("anything_else"))
    assert session.ticket["summary"]["likely_cause"].startswith("AC-01 Not cooling")


def test_kb_ac_filter_self_fix(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="near", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="done", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="done", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="done", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="fixed", language="hi")]),
    ])
    assert run(brain.respond("hawa kam aa rahi hai")).speech == kb_text("ac", "ac04_near")
    assert run(brain.respond("haan, AC ke paas hi hoon")).speech == kb_text("ac", "ac04_off")
    assert run(brain.respond("band kar diya")).speech == kb_text("ac", "ac04_filters")
    assert run(brain.respond("nikal liye")).speech == kb_text("ac", "ac04_wash")
    assert run(brain.respond("dho ke sukha diye")).speech == kb_text("ac", "ac04_refit")
    final = run(brain.respond("haan ab hawa theek aa rahi hai")).speech
    assert kb_text("ac", "resolved") in final and session.outcome == "resolved"
    assert session.selffix_ticket["status"] == "RESOLVED_REMOTE"   # "Main ticket close kar rahi hoon"


def test_kb_ac_burnt_socket_is_an_urgent_safety_ticket(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="power", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="burnt", language="hi")]),
    ])
    assert run(brain.respond("AC on nahi ho raha")).speech == kb_text("ac", "ac03_socket")
    run(brain.respond("haan socket kaala ho gaya hai"))
    assert session.hazard == "electrical" and session.ticket["priority"] == "urgent"


def test_kb_microwave_sparking_and_noise_is_a_stop_use_urgent_ticket(env):
    # KB MW-01 covers "awaaz aa rahi hai / loud noise" too, which the hazard screen alone wouldn't catch.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="microwave", rental_id="R34", symptom="sparking_noise",
                                   language="en")]),
    ], customer="C3", language="en")
    speech = run(brain.respond("my microwave is making a loud noise")).speech
    assert session.hazard == "stop_use" and session.ticket["priority"] == "urgent"
    assert "stop using the appliance" in speech


def test_kb_furniture_issue_is_a_ticket(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="furniture", symptom="bed_loose", language="hi")]),
    ])
    run(brain.respond("bed hil raha hai, screws dheele hain"))
    assert session.outcome == "escalated" and session.ticket["summary"]["likely_cause"].startswith("BD-01")


def test_long_preamble_is_dropped_in_front_of_a_script(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(content="Sure, first you should check whether the filters are clean because dust blocks air flow.",
                  tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_cooling", language="en")]),
    ], language="en")
    result = run(brain.respond("AC not cooling"))
    assert result.speech == ac_text("safety_check", "en")


def test_llm_can_flag_a_hazard_the_screen_missed(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("flag_safety_concern", kind="electrical", evidence="tingling from the fridge door", language="en")]),
    ], language="en")
    result = run(brain.respond("the fridge door feels a bit tingly when I hold it"))
    assert session.hazard == "electrical" and session.ticket["priority"] == "urgent"
    assert "main switch" in result.speech


def test_hangup_mid_playbook_schedules_a_reconnect_message(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_cooling", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    run(brain.on_hangup())
    assert session.outcome == "abandoned"
    assert rows(env.engine, followups)[0]["kind"] == "disconnected"


def test_another_problem_after_a_ticket_is_noted(env):
    brain, session, llm = env.make([
        LLMResult(content="I've added that, the technician will check the fridge too.",
                  tool_calls=[call("add_note", note="Fridge also not cooling", language="en")]),
    ], language="en")
    run(brain.respond("send someone for the AC"))          # explicit request: booked without the LLM
    assert session.ticket and llm.requests == []
    run(brain.respond("also my fridge isn't cold"))
    assert session.ticket["summary"]["notes"] == ["Fridge also not cooling"]


@pytest.mark.parametrize("text,expected", [
    ("Please just send a technician, I don't have time", True),
    ("filter wala mujhse nahi hoga, koi aadmi bhej do", True),
    ("technician chahiye", True),
    ("I want to talk to a human", True),
    ("टेक्नीशियन भेज दीजिए", True),
    ("technician ne bola tha gas khatam hai", False),
    ("mujhe technician nahi chahiye, main khud kar lunga", False),
    ("agar theek nahi hua to technician bhejna", False),
    ("AC ke liye koi solution chahiye", False),
])
def test_technician_request_detection(text, expected):
    assert technician_requested(text) is expected


def test_no_hazard_is_not_assumed_from_unrelated_answers(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    run(brain.respond("AC start nahi ho raha"))
    again = run(brain.respond("remote ki screen theek hai, par AC kuch respond nahi kar raha bilkul bhi"))
    assert session.run.current == "safety_check" and again.speech.startswith("बस safety")
    run(brain.respond("nahi, aisa kuch nahi hai"))
    assert session.run.current == "ac07_remote"


def test_say_tool_speaks_the_models_own_words(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("say", text="Sure, which AC is it, bedroom or living room?", language="en")]),
    ], language="en")
    assert run(brain.respond("my AC has a problem")).speech == "Sure, which AC is it, bedroom or living room?"


def test_code_heard_before_the_playbook_is_routed_after_safety(env):
    env.library = SOP
    washer = SOP.for_appliance("washer")
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("record_error_code", code="F05", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", rental_id="R12", symptom="unknown", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    assert "F 0 5" in run(brain.respond("F05 likha aa raha hai")).speech
    assert run(brain.respond("washing machine pe")).speech == washer.node("safety_check").text("hi")
    run(brain.respond("nahi, kuch nahi"))
    assert session.outcome == "escalated" and session.run.current == "esc_error_code"
    assert session.error_code["brand"] == "Whirlpool" and session.error_code["meaning"] is None


def test_off_script_say_is_replaced_by_the_current_step(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("say", text="अब एक बार check कीजिए कि AC का plug ठीक से लगा है।", language="hi")]),
    ])
    run(brain.respond("AC on nahi ho raha"))
    speech = run(brain.respond("kya karun?")).speech
    assert speech.endswith(ac_text("safety_check")) and "check कीजिए" not in speech
    assert any(e["type"] == "guard" for e in session.events)


def test_clear_denial_is_accepted_without_a_loop(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    run(brain.respond("AC on hi nahi ho raha"))
    run(brain.respond("Nahi, kuch bhi nahi hai, bilkul normal hai."))
    assert session.run.current == "ac07_remote"


def test_llm_hazard_flag_without_danger_words_asks_first(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("flag_safety_concern", kind="electrical", evidence="power trips", language="hi")]),
        LLMResult(tool_calls=[call("flag_safety_concern", kind="electrical", evidence="burning smell", language="hi")]),
    ])
    probe = run(brain.respond("AC on karte hi poore ghar ki light chali jaati hai"))
    assert session.hazard is None and "safety" in probe.speech
    run(brain.respond("haan thodi si jalne ki smell bhi aa rahi thi"))
    assert session.hazard == "electrical"


@pytest.mark.parametrize("text,lang", [
    ("machine pe PE error aa raha hai", "hi"),
    ("AC on karte hi poore ghar ki light chali jaati hai", "hi"),
    ("एसी ठंडा नहीं कर रहा", "hi"),
    ("My fridge is not cooling and the light is off", "en"),
    ("LG", None),
    ("ok", None),
])
def test_language_detection(text, lang):
    assert detect_language(text) == lang


def test_roman_hinglish_gets_hindi_scripts_even_if_the_model_says_en(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_starting", language="en")]),
    ], language="en")
    speech = run(brain.respond("AC on hi nahi ho raha hai")).speech
    assert speech == ac_text("safety_check", "hi")


# ---- answering steps the caller already covered ----------------------------------------
def _ac_to_wall_switch(first_words):
    """An AC call walked to the wall-switch step (AC-07), with `first_words` said up front."""
    return first_words, [
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="remote_ok", language="hi")]),
    ], ["nahi, aisa kuch nahi", "remote ki screen theek hai, par AC respond nahi kar raha"]


def test_step_the_caller_already_answered_is_skipped_with_their_words(env):
    env.library = SOP
    first, script, replies = _ac_to_wall_switch("AC start nahi ho raha. Abhi power cut hai, ghar inverter pe chal raha hai.")
    brain, session, llm = env.make([
        *script,
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="on_backup", caller_said="power cut hai, ghar inverter pe chal raha hai",
                                   language="hi")]),
        LLMResult(content="A) Power cut, house is on inverter or backup"),   # the blind re-check agrees
    ])
    run(brain.respond(first))
    for reply in replies:
        run(brain.respond(reply))
    assert session.run.current == "ac07_power" and len(llm.requests) == 3   # no look-ahead where nothing was said
    final = run(brain.respond("wall switch on hai")).speech
    # The power-cut question is never read out: the caller had already said it.
    assert ac_text("ac07_house_power") not in final and ac_text("resolved_power_cut") in final
    assert session.outcome == "resolved" and session.resolution == "no_fault_power_cut"
    assert "ac07_house_power" not in session.presented
    skipped = session.run.path[-1]
    assert skipped["node"] == "ac07_house_power" and skipped["skipped"]
    assert skipped["caller_said"] == "power cut hai, ghar inverter pe chal raha hai"
    assert 'answered earlier: "power cut hai, ghar inverter pe chal raha hai"' in skipped["label"]
    look, check = llm.requests[-2], llm.requests[-1]
    assert [t["function"]["name"] for t in look["tools"]] == ["answer_step"]
    assert look["tools"][0]["function"]["parameters"]["properties"]["outcome"]["enum"][0] == "ask_caller"
    assert check["tools"] == [] and "A) Power cut, house is on inverter or backup" in check["messages"][-1]["content"]
    assert "on_backup" not in check["messages"][-1]["content"]   # the checker doesn't see the model's pick
    assert session.latency[-1]["lookahead_rounds"] == 2   # one look-ahead round + one skip check
    assert sum(1 for t in session.transcript if t["role"] == "user") == 4   # 5 without the skip


def test_skip_the_check_disagrees_with_is_asked(env):
    env.library = SOP
    # Seen live (on the old MCB step): "saare MCB bhi upar hain" (all up) taken as "one switch is down".
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="remote_ok", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="on_backup", caller_said="house mein light aa rahi hai", language="hi")]),
        LLMResult(content="B"),   # the blind re-check: "Normal power is on"
    ])
    run(brain.respond("AC start nahi ho raha"))
    run(brain.respond("nahi, aisa kuch nahi"))
    run(brain.respond("remote ki screen theek hai"))
    speech = run(brain.respond("Haan, wall switch on hai. Aur house mein light aa rahi hai.")).speech
    assert speech == ac_text("ac07_house_power") and session.run.current == "ac07_house_power"
    assert "B) Normal power is on" in llm.requests[-1]["messages"][-1]["content"]
    assert not any(p.get("skipped") for p in session.run.path)


def test_made_up_quote_is_rejected_and_the_step_is_asked(env):
    env.library = SOP
    first, script, replies = _ac_to_wall_switch("AC start nahi ho raha, ghar mein inverter laga hai")
    brain, session, llm = env.make([
        *script,
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="on_backup", caller_said="power cut hai, inverter pe chal raha hai",
                                   language="hi")]),
    ])
    run(brain.respond(first))
    for reply in replies:
        run(brain.respond(reply))
    speech = run(brain.respond("wall switch on hai")).speech
    assert speech == ac_text("ac07_house_power")
    assert session.run.current == "ac07_house_power" and "ac07_house_power" in session.presented
    assert not any(p.get("skipped") for p in session.run.path)
    assert len(llm.requests) == 5   # the look-ahead ran once and its answer was refused


def test_real_words_about_another_step_are_not_evidence(env):
    env.library = SOP
    # Seen live: the caller had mentioned the wall switch, but the model quoted their power-cut sentence.
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="remote_ok", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", language="hi",
                                   caller_said="Abhi power cut hai toh ghar inverter par chal raha hai")]),
    ])
    run(brain.respond("AC start nahi ho raha, wall switch on hai"))
    run(brain.respond("nahi, aisa kuch nahi"))
    speech = run(brain.respond("Screen par sab dikh raha hai. Abhi power cut hai toh ghar inverter par chal raha hai.")).speech
    assert speech == ac_text("ac07_power") and session.run.current == "ac07_power"
    assert len(llm.requests) == 4 and not any(p.get("skipped") for p in session.run.path)


def test_look_ahead_can_decline_and_the_step_is_read(env):
    env.library = SOP
    first, script, replies = _ac_to_wall_switch("AC start nahi ho raha. Abhi power cut hai, ghar inverter pe chal raha hai.")
    brain, session, _ = env.make([
        *script,
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="ask_caller", caller_said="", language="hi")]),
    ])
    run(brain.respond(first))
    for reply in replies:
        run(brain.respond(reply))
    assert run(brain.respond("wall switch on hai")).speech == ac_text("ac07_house_power")
    assert session.run.current == "ac07_house_power" and session.outcome == "in_progress"


def test_safety_question_is_never_skipped(env):
    env.library = SOP
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi"),
                              call("answer_step", outcome="no_hazard", caller_said="koi smell ya spark nahi hai", language="hi")]),
    ])
    speech = run(brain.respond("AC start nahi ho raha, koi smell ya spark nahi hai")).speech
    assert speech == ac_text("safety_check") and session.run.current == "safety_check"
    assert not session.run.path


def test_no_look_ahead_onto_a_safety_question(env, tmp_path):
    # The archived geyser playbook is the one that asks a question before its safety check.
    (tmp_path / "kb" / "playbooks").mkdir(parents=True)
    (tmp_path / "kb" / "sources.yaml").write_text((KNOWLEDGE_DIR / "sources.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "kb" / "playbooks" / "geyser.yaml").write_text(
        (KNOWLEDGE_DIR / "archive" / "geyser.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    library = PlaybookLibrary(tmp_path / "kb")
    geyser = library.for_appliance("geyser")
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="geyser", symptom="unknown", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="electric", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", caller_said="taps se shock bhi nahi lagta", language="hi")]),
    ])
    brain.library = library
    said = "electric wala hai. Switch ke paas paani nahi hai, taps se shock bhi nahi lagta"
    run(brain.respond("geyser garam paani nahi de raha"))
    speech = run(brain.respond(said)).speech
    assert speech == geyser.node("safety_check").text("hi") and session.run.current == "safety_check"
    # The caller's words ("taps", "shock") match the safety question, yet no look-ahead round runs for it.
    assert _node_vocab(geyser.node("safety_check")) - _node_vocab(geyser.node("geyser_type")) & _vocab([said])
    assert len(llm.requests) == 2


def test_look_ahead_stops_after_two_rounds(env):
    env.library = SOP
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="remote_ok", caller_said="remote ki screen theek hai", language="hi")]),
        LLMResult(content="C"),
        LLMResult(tool_calls=[call("answer_step", outcome="already_on", caller_said="wall switch bhi on hai", language="hi")]),
        LLMResult(content="B"),
    ])
    run(brain.respond("AC start nahi ho raha"))
    speech = run(brain.respond("nahi, aisa kuch nahi. Remote ki screen theek hai, wall switch bhi on hai, light bhi hai")).speech
    # Round 1 skips the remote step, round 2 confirms it; then the budget is spent and the wall switch is read out.
    assert speech == ac_text("ac07_power") and session.run.current == "ac07_power"
    assert len(llm.requests) == 2 + LOOKAHEAD_ROUNDS and len(llm.responses) == 2
    assert session.latency[-1]["lookahead_rounds"] == LOOKAHEAD_ROUNDS
    assert [p["node"] for p in session.run.path if p.get("skipped")] == ["ac07_remote"]
    assert "ac07_remote" not in session.presented
    # The technician handoff shows which answers came from the caller's earlier words.
    run(brain.respond("technician bhej do"))
    tried = [t["answer"] for t in session.ticket["summary"]["steps_tried"]]
    assert "Remote screen works but the AC doesn't respond (answered earlier: \"Remote ki screen theek hai\")" in tried


def test_unasked_step_without_caller_words_is_rejected(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi"),
                              call("answer_step", outcome="remote_ok", language="hi")]),
    ])
    run(brain.respond("AC start nahi ho raha"))
    assert run(brain.respond("nahi, aisa kuch nahi")).speech == ac_text("ac07_remote")
    assert session.run.current == "ac07_remote"


@pytest.mark.parametrize("quote,said,expected", [
    ("power cut hai, ghar inverter pe chal raha hai", "AC start nahi ho raha. Abhi power cut hai, ghar inverter pe chal raha hai.",
     "power cut hai, ghar inverter pe chal raha hai"),
    ("MCB bhi sab upar hai", "MCB sab upar hain", "MCB sab upar hain"),                # spelling variant, one filler word
    ("it’s on inverter", "No, it's on inverter backup right now", "it's on inverter"),  # the "no" is another clause
    ("अभी बिजली नहीं है", "हाँ, अभी बिजली नहीं है, inverter पर चल रहा है", "अभी बिजली नहीं है"),
    ("wall switch on hai", "wall switch on nahi hai", None),                            # the caller negated it
    ("switch on hai", "switch off hai", None),
    ("power cut", "there is no power cut", None),
    ("बिजली है", "अभी बिजली नहीं है", None),
    ("inverter pe chal raha hai", "ghar mein inverter laga hai", None),
    ("inverter", "inverter pe hai", None),                                               # too short to be evidence
])
def test_find_quote(quote, said, expected):
    assert find_quote(quote, [said]) == expected


def test_repeating_a_heard_step_gets_a_wait_phrase_not_the_step_again(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_starting", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
        LLMResult(tool_calls=[call("say", text="ठीक है, remote को AC की तरफ करके ON दबाइए", language="hi")]),
    ])
    run(brain.respond("AC on nahi ho raha"))
    run(brain.respond("nahi, aisa kuch nahi hai"))
    assert session.run.current == "ac07_remote" and "ac07_remote" in session.presented
    speech = run(brain.respond("ruko, main remote dhoond rahi hoon")).speech
    assert speech == brain._phrase("take_your_time")
    assert not any(e["type"] == "guard" for e in session.events)


def test_water_from_the_appliance_is_not_a_water_near_plug_alarm(env):
    env.library = SOP
    # Live Bolna test: "se paani leak kar raha hai" answered the safety question and set off the water alarm.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="water_leak", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="hazard_water", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="no_hazard", language="hi")]),
    ])
    run(brain.respond("mere AC se paani aa raha hai"))
    speech = run(brain.respond("se paani leak kar raha hai")).speech
    assert session.hazard is None and session.run.current == "safety_check"
    assert speech == brain._phrase("water_where", appliance="AC")
    run(brain.respond("sirf AC se tapak raha hai"))
    assert session.hazard is None and session.run.current != "safety_check"


def test_water_near_the_plug_still_triggers(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="water_leak", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="hazard_water", language="hi")]),
    ])
    run(brain.respond("AC se paani aa raha hai"))
    run(brain.respond("haan neeche wale board ke paas bhi geela hai"))
    assert session.hazard == "water_electric"


def test_after_a_safety_alert_instructions_become_the_hold_line(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("say", text="कृपया AC और उसके plug से दूर रहिए", language="hi")]),
    ])
    run(brain.respond("AC se chingari nikal rahi hai"))
    assert session.hazard == "electrical"
    speech = run(brain.respond("hello meri awaaz aa rahi hai?")).speech
    assert speech == brain._hazard_hold() and "main switch" in speech
    assert "safety check" not in speech


def test_tools_not_offered_this_turn_are_not_run(env):
    # Live call: after the urgent safety ticket, the model called escalate_to_technician (not on offer).
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="caller asked", caller_requested=True, language="hi")]),
    ])
    run(brain.respond("AC se chingari nikal rahi hai"))
    ticket = session.ticket
    speech = run(brain.respond("technician bhej do")).speech
    assert session.ticket is ticket and session.ticket["category"] == "safety"
    assert ticket["code"].replace("-", "") not in speech.replace(" ", "")


def test_model_water_flag_asks_where_the_water_is_first(env):
    env.library = SOP
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("flag_safety_concern", kind="water_electric", evidence="paani hai", language="hi")]),
        LLMResult(tool_calls=[call("flag_safety_concern", kind="water_electric", evidence="plug ke paas", language="hi")]),
    ])
    run(brain.respond("AC thanda nahi kar raha"))
    speech = run(brain.respond("haan paani hai yahan par")).speech
    assert session.hazard is None and speech == brain._phrase("water_where", appliance="AC")
    run(brain.respond("plug ke paas"))
    assert session.hazard == "water_electric"


@pytest.mark.parametrize("text,done", [
    ("nahi bas, thank you", True), ("नहीं, धन्यवाद", True), ("no that's all", True), ("bas itna hi", True),
    ("ok bye", True), ("theek hai shukriya", True),
    ("haan, fridge bhi kharab hai", False), ("nahi, technician kab aayega?", False), ("AC nahi chal raha", False),
    ("ek aur problem hai", False), ("मुश्किल नहीं हो जाएगा", False), ("नहीं", True), ("no", True),
    ("filter nikalna aasan nahi hai", False),
])
def test_caller_is_done(text, done):
    assert caller_is_done(text) is done


def test_anything_else_then_goodbye_ends_the_call(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="caller asked", caller_requested=True, language="hi")]),
    ])
    speech = run(brain.respond("mera AC kharab hai, kya karun")).speech
    assert speech.endswith(brain._phrase("anything_else"))
    goodbye = run(brain.respond("nahi bas, dhanyavaad")).speech
    assert goodbye == brain._phrase("goodbye", name=" Ananya जी") and session.end_requested


def test_another_problem_after_anything_else_is_not_a_goodbye(env):
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="caller asked", caller_requested=True, language="hi")]),
        LLMResult(tool_calls=[call("add_note", note="Fridge also leaking", language="hi")]),
    ])
    run(brain.respond("AC kharab hai"))
    run(brain.respond("haan, fridge bhi leak kar raha hai"))
    assert not session.end_requested


def test_safety_call_ends_with_a_safe_goodbye(env):
    brain, session, _ = env.make([])
    run(brain.respond("AC se jalne ki smell aa rahi hai"))
    assert session.hazard == "electrical"
    speech = run(brain.respond("theek hai"))
    assert speech.speech == brain._phrase("safety_goodbye", minutes=env.tenant.urgent_callback_minutes)
    assert session.end_requested


@pytest.mark.parametrize("question", ["par AC to abhi on hai mera", "पर एसी तो अभी ऑन है मेरा", "ab kya karun?", "plug nikal doon?"])
def test_action_questions_after_an_alert_get_the_hazard_reminder(env, question):
    brain, session, llm = env.make([])
    run(brain.respond("AC se jalne ki smell aa rahi hai"))
    n = len(llm.requests)
    speech = run(brain.respond(question)).speech
    assert speech == brain._hazard_hold() and len(llm.requests) == n   # scripted, no model call
    assert "main switch" in speech and "plug" in speech


def test_gas_reminder_never_says_to_switch_anything_off():
    from fixfirst.config import Tenant
    from fixfirst.safety import get_screen
    for lang in ("hi", "en"):
        text = get_screen().reminder("gas", lang, Tenant.load()).lower()
        assert "main switch" not in text and ("मत" in text or "don't" in text)


def test_only_the_scripted_goodbyes_contain_the_hang_up_phrases():
    # Bolna hangs up when the agent's last line contains one of these; the greeting and questions must not.
    from fixfirst.brain import PHRASES
    marks = ("आपका दिन शुभ हो", "अलविदा", "have a nice day", "have a good day", "goodbye")
    for key, texts in PHRASES.items():
        for text in texts.values():
            has = any(m in text.lower() for m in marks)
            assert has == (key in ("goodbye", "safety_goodbye", "goodbye_again", "goodbye_later")), key


def test_after_the_goodbye_more_thanks_get_a_short_goodbye_not_the_model(env):
    # Live call: "ठीक है थैंक यू" after the goodbye went to the model, whose reply had no hang-up phrase.
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="caller asked", caller_requested=True, language="hi")]),
    ])
    run(brain.respond("mera AC kharab hai, kya karun"))
    run(brain.respond("nahi bas, dhanyavaad"))
    n = len(llm.requests)
    speech = run(brain.respond("ठीक है थैंक यू")).speech
    assert speech == brain._phrase("goodbye_again") and len(llm.requests) == n and session.end_requested


def test_bye_in_the_middle_of_a_self_fix_lets_the_caller_go(env):
    # Eval F03: the caller wanted to finish later and kept saying "okay, bye"; the agent kept chatting.
    brain, session, llm = env.make([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", rental_id="R11", symptom="weak_airflow", language="en")]),
    ], language="en")
    run(brain.respond("the air from my AC is very weak"))
    n = len(llm.requests)
    speech = run(brain.respond("Okay, I will call back later. Bye."))
    assert speech.speech == brain._phrase("goodbye_later") and session.end_requested and len(llm.requests) == n


def test_a_plain_smell_gets_a_safety_question_not_an_alarm(env):
    # Eval F12: "There's a smell coming from the microwave" was flagged as a fire by the model.
    brain, session, _ = env.make([
        LLMResult(tool_calls=[call("flag_safety_concern", kind="fire", evidence="smell from the microwave", language="en")]),
    ], customer="C3", language="en")
    speech = run(brain.respond("There's a smell coming from the microwave")).speech
    assert session.hazard is None and speech == brain._phrase("safety_probe")

