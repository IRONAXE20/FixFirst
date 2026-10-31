"""Claude client conversion, RentBasket customer lookup and the Bolna custom-LLM bridge (no network)."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import fixfirst.server as server
from fixfirst import rentbasket
from fixfirst.bolna import call_vars, sse_chunks, webhook_token
from fixfirst.claude_llm import CALL_STARTED, ClaudeChat, convert
from fixfirst.config import Settings
from fixfirst.llm import LLMError, LLMResult, ScriptedLLM, call

API_DATA = {
    "mobile_no": "9810000000", "name": "Test Customer",
    "product_ids": ["1024", "1153", "1108", "2069", "11551"],
    "product_names": ['SMART LED 32" - 2011', "Fridge 190 Ltr - 2016", "Window AC - 2009",
                      "Wooden Single Bed 6x3 Basic - 2017", "Fully Automatic Washing M/c - 2025"],
    "address": ["Flat 12, Tower B, Sector 45, Gurgaon, Haryana", "Somewhere else"],
    "complaints": [],
}


# ---- Claude ------------------------------------------------------------------------------
def _history():
    tc = call("start_troubleshooting", appliance="ac", symptom="unknown", language="hi")
    return [
        {"role": "system", "content": "SYSTEM"},
        {"role": "assistant", "content": "नमस्ते!"},
        {"role": "user", "content": "AC kharab hai"},
        LLMResult(tool_calls=[tc, call("record_error_code", code="E4", language="hi")]).assistant_message(),
        {"role": "tool", "tool_call_id": tc.id, "content": "started"},
        {"role": "tool", "tool_call_id": "call_record_error_code_2", "content": "noted"},
        {"role": "assistant", "content": "Safety check?"},
        {"role": "user", "content": "nahi"},
    ]


def test_convert_maps_openai_history_to_messages_api():
    system, msgs, used = convert(_history())
    assert system == "SYSTEM"
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "user", "assistant", "user"]
    assert msgs[0]["content"][0]["text"] == CALL_STARTED   # the greeting can't open the conversation
    tool_uses = [b for b in msgs[3]["content"] if b["type"] == "tool_use"]
    assert [b["name"] for b in tool_uses] == ["start_troubleshooting", "record_error_code"]
    assert tool_uses[0]["input"]["appliance"] == "ac"
    # Both results go back in ONE user message, before anything else.
    assert [b["type"] for b in msgs[4]["content"]] == ["tool_result", "tool_result"]
    assert used == {"start_troubleshooting", "record_error_code"}


class _FakeMessages:
    def __init__(self, response):
        self.response, self.kwargs = response, None

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def _claude(response):
    chat = ClaudeChat(api_key="test", model="claude-haiku-5-5")
    chat.client = SimpleNamespace(messages=_FakeMessages(response))
    return chat


def _response(*blocks, stop_reason="tool_use"):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason)


def test_claude_forces_a_tool_and_parses_calls():
    tool = SimpleNamespace(type="tool_use", id="toolu_1", name="say", input={"text": "Ji", "language": "hi"})
    chat = _claude(_response(tool))
    tools = [{"type": "function", "function": {"name": "say", "description": "d", "parameters": {"type": "object"}}},
             {"type": "function", "function": {"name": "answer_step", "description": "d", "parameters": {"type": "object"}}}]
    result = asyncio.run(chat.complete(_history(), tools))
    kw = chat.client.messages.kwargs
    assert kw["tool_choice"] == {"type": "any"} and kw["model"] == "claude-haiku-5-5"
    assert "temperature" not in kw and kw["tools"][0]["input_schema"] == {"type": "object"}
    assert result.tool_calls[0].name == "say" and result.tool_calls[0].arguments["text"] == "Ji"


def test_claude_single_tool_is_named_and_no_tools_still_defines_history_tools():
    chat = _claude(_response(SimpleNamespace(type="text", text="B"), stop_reason="end_turn"))
    one = [{"type": "function", "function": {"name": "answer_step", "parameters": {"type": "object"}}}]
    asyncio.run(chat.complete(_history(), one))
    assert chat.client.messages.kwargs["tool_choice"] == {"type": "tool", "name": "answer_step"}
    result = asyncio.run(chat.complete(_history(), []))
    kw = chat.client.messages.kwargs
    assert kw["tool_choice"] == {"type": "none"} and {t["name"] for t in kw["tools"]} == {"start_troubleshooting", "record_error_code"}
    assert result.content == "B"


def test_claude_refusal_is_an_llm_error():
    chat = _claude(_response(stop_reason="refusal"))
    with pytest.raises(LLMError):
        asyncio.run(chat.complete([{"role": "user", "content": "hi"}], []))


def test_brain_provider_prefers_claude_and_ignores_bolna_keys():
    assert Settings(anthropic_api_key="k").brain_provider == "anthropic"
    assert Settings(sarvam_api_key="bn-123").brain_provider is None
    assert Settings(sarvam_api_key="sk_1").brain_provider == "sarvam"
    assert Settings(sarvam_api_key="sk_1", anthropic_api_key="k", llm_provider="sarvam").brain_provider == "sarvam"


# ---- RentBasket --------------------------------------------------------------------------
@pytest.mark.parametrize("name,appliance", [
    ("Window AC", "ac"), ("Split AC - 1.5 Ton", "ac"), ("Fridge 190 Ltr", "fridge"), ("Double Door Fridge", "fridge"),
    ("Fully Automatic Washing M/c", "washer"), ("Semi Automatic Washing Machine", "washer"), ("OTG 28 L", "otg"),
    ('SMART LED 32"', "tv"), ("Inverter with Double Battery", "inverter"), ("Water Purifier - Under-the-Counter (UTC)", "water_purifier"),
    ("Gas Stove - 3 Burner", "stove"), ("Microwave (Solo) 20 L", "microwave"), ("Induction Cooker", "induction"),
    ("Wooden Almirah 6.5x3 ft", "furniture"), ("3-Seater Fabric Sofa", "furniture"),
])
def test_product_names_map_to_appliances(name, appliance):
    assert rentbasket.appliance_for(name) == appliance


def test_parse_strips_the_year_and_keeps_the_address():
    customer, rows = rentbasket.parse(API_DATA, "9810000000", "hi")
    assert customer["id"] == "RB9810000000" and customer["phone"] == "+91 9810000000"
    assert customer["area"] == "Sector 45, Gurgaon, Haryana"
    labels = {r["label"]: (r["appliance"], r["details"]["year"]) for r in rows}
    assert labels["Window AC"] == ("ac", "2009") and labels['SMART LED 32"'] == ("tv", "2011")


def test_mobile_number_normalisation():
    assert rentbasket.mobile10("+91 98100 00000") == "9810000000"
    assert rentbasket.mobile10("12345") is None


# ---- Bolna bridge ------------------------------------------------------------------------
def test_call_vars_skip_unsubstituted_placeholders():
    prompt = "FIXFIRST_CALL call_ref=abc-123 from=+919810000000 to={to_number} customer_phone={customer_phone}"
    assert call_vars(prompt) == {"call_ref": "abc-123", "from": "+919810000000"}


def test_sse_chunks_are_openai_deltas_ending_in_done():
    chunks = list(sse_chunks("पहला वाक्य। Second one.", "fixfirst"))
    assert chunks[-1] == "data: [DONE]\n\n"
    bodies = [json.loads(c[6:]) for c in chunks[:-1]]
    text = "".join(b["choices"][0]["delta"].get("content", "") for b in bodies)
    assert text.strip() == "पहला वाक्य। Second one." and bodies[-1]["choices"][0]["finish_reason"] == "stop"


@pytest.fixture
def phone(tmp_path, monkeypatch, request):
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")

    async def fake_fetch(_settings, mobile):
        return API_DATA if mobile == "9810000000" else None

    monkeypatch.setattr(rentbasket, "fetch", fake_fetch)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'p.db').as_posix()}", whatsapp_mode="dry_run",
                        public_base_url="http://test", fixfirst_llm_key="ff_test",
                        identity_check=getattr(request, "param", False))   # the call-script tests turn it on
    llm = ScriptedLLM([])
    app = server.create_app(settings, llm_factory=lambda: llm)
    with TestClient(app) as c:
        c.llm = llm
        yield c


AUTH = {"Authorization": "Bearer ff_test"}
SYSTEM = "FIXFIRST_CALL call_ref=exec-1 from=+919810000000 to=+918000000000 customer_phone={customer_phone}"


def _body(*turns, stream=True):
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "assistant", "content": "नमस्ते! बताइए, क्या problem है?"}]
    for i, t in enumerate(turns):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": t})
    return {"model": "fixfirst", "messages": msgs, "stream": stream}


def _spoken(resp) -> str:
    out = []
    for line in resp.text.splitlines():
        if line.startswith("data: {"):
            out.append(json.loads(line[6:])["choices"][0]["delta"].get("content", ""))
    return "".join(out).strip()


def test_bolna_needs_our_key(phone):
    assert phone.post("/bolna/v1/chat/completions", json=_body("hello")).status_code == 401
    assert phone.get("/bolna/v1/models", headers=AUTH).json()["data"][0]["id"] == "fixfirst"


def test_bolna_turns_feed_only_new_words_to_the_brain(phone):
    phone.llm.responses.extend([
        LLMResult(tool_calls=[call("say", text="Kaunsa appliance?", language="hi")]),
        LLMResult(tool_calls=[call("say", text="Theek hai.", language="hi")]),
    ])
    r = phone.post("/bolna/v1/chat/completions", json=_body("mera ek appliance kharab hai"), headers=AUTH)
    assert r.status_code == 200 and _spoken(r) == "Kaunsa appliance?"
    # The caller was found through the RentBasket API and copied into the local customers.
    assert any(c["name"] == "Test Customer" for c in phone.get("/api/customers").json())
    r = phone.post("/bolna/v1/chat/completions", json=_body("mera ek appliance kharab hai", "Kaunsa appliance?", "TV"),
                   headers=AUTH)
    assert _spoken(r) == "Theek hai."
    last_user = phone.llm.requests[-1]["messages"][-1]
    assert last_user == {"role": "user", "content": "TV"}   # not the whole Bolna history again
    # A repeat request with no new caller words gets the last reply again, without another model call.
    n = len(phone.llm.requests)
    r = phone.post("/bolna/v1/chat/completions", json=_body("mera ek appliance kharab hai", "Kaunsa appliance?", "TV"),
                   headers=AUTH)
    assert _spoken(r) == "Theek hai." and len(phone.llm.requests) == n


def test_bolna_hazard_and_hangup_webhook(phone):
    r = phone.post("/bolna/v1/chat/completions", json=_body("gas ki smell aa rahi hai kitchen mein"), headers=AUTH)
    assert "1906" in _spoken(r)
    assert phone.post("/bolna/webhook/wrong", json={"id": "exec-1", "status": "completed"}).status_code == 401
    assert "ff_test" not in webhook_token("ff_test")   # the LLM key never appears in the webhook URL
    r = phone.post(f"/bolna/webhook/{webhook_token('ff_test')}", json={"id": "exec-1", "status": "completed",
                                                                       "telephony_data": {"recording_url": "https://rec/1.mp3"}})
    assert r.json()["status"] == "completed"
    call_row = phone.get("/api/calls").json()[0]
    assert call_row["hazard"] == "gas" and call_row["channel"] == "phone"


def test_bolna_side_checks_are_not_conversation_turns(phone):
    body = {"model": "fixfirst", "stream": False,
            "messages": [{"role": "system", "content": "Decide if the call should end."}, {"role": "user", "content": "bye"}]}
    r = phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    assert r.json()["choices"][0]["message"]["content"] == "" and not phone.llm.requests


def test_lookup_by_phone_on_the_call_page(phone):
    c = phone.post("/api/customers/lookup", json={"phone": "98100 00000"}).json()
    assert c["name"] == "Test Customer" and {r["appliance"] for r in c["rentals"]} >= {"tv", "fridge", "ac", "washer", "furniture"}
    assert phone.post("/api/customers/lookup", json={"phone": "9999999999"}).status_code == 404


def test_bolna_call_without_prompt_variables_is_matched_through_the_webhook(phone):
    # Live behaviour: Bolna leaves {execution_id}/{from_number} unfilled, but its status webhooks arrive
    # first with the execution id and the numbers.
    from fixfirst.bolna import webhook_token
    hook = f"/bolna/webhook/{webhook_token('ff_test')}"
    phone.post(hook, json={"id": "exec-77", "status": "in-progress",
                           "telephony_data": {"to_number": "+919810000000", "from_number": "+918000000000"}})
    body = _body("mera TV kharab hai")
    body["messages"][0]["content"] = "FIXFIRST_CALL call_ref={execution_id} from={from_number} to={to_number}"
    phone.llm.responses.append(LLMResult(tool_calls=[call("say", text="Ji, bataiye.", language="hi")]))
    r = phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    assert _spoken(r) == "Ji, bataiye."
    calls = phone.get("/api/calls").json()
    assert calls[0]["customer_id"] == "RB9810000000"   # found through the webhook's number
    r = phone.post(hook, json={"id": "exec-77", "status": "completed", "transcript": "user: mera TV kharab hai"})
    assert r.json()["status"] == "completed"


def test_webhook_matches_an_anonymous_conversation_by_its_first_words(phone):
    from fixfirst.bolna import webhook_token
    body = _body("fridge leak kar raha hai")
    body["messages"][0]["content"] = "FIXFIRST_CALL call_ref={execution_id}"
    phone.llm.responses.append(LLMResult(tool_calls=[call("say", text="Theek hai.", language="hi")]))
    phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    r = phone.post(f"/bolna/webhook/{webhook_token('ff_test')}",
                   json={"id": "exec-88", "status": "completed", "transcript": "assistant: ...\nuser: fridge leak kar raha hai"})
    assert r.json()["status"] == "completed"


def test_customer_lookup_starts_at_the_webhook_not_the_first_turn(phone, monkeypatch):
    from fixfirst.bolna import webhook_token
    lookups = []
    original = rentbasket.fetch

    async def counting_fetch(settings, mobile):
        lookups.append(mobile)
        return await original(settings, mobile)

    monkeypatch.setattr(rentbasket, "fetch", counting_fetch)
    phone.post(f"/bolna/webhook/{webhook_token('ff_test')}",
               json={"id": "exec-99", "status": "ringing", "telephony_data": {"to_number": "+919810000000"}})
    assert lookups == ["9810000000"]   # looked up when the webhook arrived
    body = _body("AC kharab hai")
    body["messages"][0]["content"] = "FIXFIRST_CALL call_ref={execution_id}"
    phone.llm.responses.append(LLMResult(tool_calls=[call("say", text="Ji.", language="hi")]))
    phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    assert lookups == ["9810000000"]   # the first turn reused it
    assert phone.get("/api/calls").json()[0]["customer_id"] == "RB9810000000"


def test_bolna_hangup_check_says_yes_only_after_the_goodbye(phone):
    phone.llm.responses.append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", caller_requested=True,
                                                          language="hi")]))
    first = "mera washing machine kharab hai"
    booked = _spoken(phone.post("/bolna/v1/chat/completions", json=_body(first), headers=AUTH))
    check = {"model": "fixfirst", "stream": False, "messages": [
        {"role": "system", "content": "You are an AI assistant determining if a conversation is complete. "
                                      'Respond in json {"hangup": "Yes"} or {"hangup": "No"}'},
        {"role": "user", "content": f"user: {first}\nassistant: ..."}]}
    r = phone.post("/bolna/v1/chat/completions", json=check, headers=AUTH)
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"hangup": "No"}
    r = phone.post("/bolna/v1/chat/completions", json=_body(first, booked, "nahi bas, thank you"), headers=AUTH)
    assert "Have a good day" in _spoken(r)
    r = phone.post("/bolna/v1/chat/completions", json=check, headers=AUTH)
    assert json.loads(r.json()["choices"][0]["message"]["content"]) == {"hangup": "Yes"}


def test_caller_saying_cut_the_call_still_reaches_the_brain(phone):
    phone.llm.responses.append(LLMResult(tool_calls=[call("say", text="Ji, bataiye.", language="hi")]))
    r = phone.post("/bolna/v1/chat/completions", json=_body("please don't cut the call, my AC is broken"), headers=AUTH)
    assert _spoken(r) == "Ji, bataiye."



def test_bolna_call_as_a_customer_is_a_test_call(phone):
    from fixfirst.bolna import webhook_token
    hook = f"/bolna/webhook/{webhook_token('ff_test')}"
    phone.post(hook, json={"id": "exec-55", "status": "initiated", "context_details": {"recipient_data": {
        "test_customer_phone": "+919810000000"}}, "telephony_data": {"to_number": "+917000000000"}})
    body = _body("AC kharab hai")
    body["messages"][0]["content"] = "FIXFIRST_CALL call_ref={execution_id}"
    phone.llm.responses.append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", language="hi")]))
    phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    assert phone.get("/api/calls").json()[0]["customer_id"] == "RB9810000000"
    phone.post(hook, json={"id": "exec-55", "status": "completed"})   # ticket WhatsApps go out when the call ends
    customer_msgs = [m for m in phone.get("/api/outbox").json() if m["audience"] == "customer"]
    assert customer_msgs and all(m["to"] is None for m in customer_msgs)   # nothing to the real customer


def test_bolna_reply_the_caller_never_heard_is_undone_and_their_pieces_answered_together(phone):
    # Live call 26 Oct: "नहीं यार।" then "हाँ, सब कुछ नहीं है।" arrived as two turns. Bolna dropped our reply to
    # the first piece (the caller kept talking), yet the playbook had moved on and took the second piece as
    # the answer to a step the caller never heard.
    from fixfirst.playbooks import get_library
    ac = get_library().for_appliance("ac")
    phone.llm.responses.extend([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="weak_airflow", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="near", language="hi")]),
        LLMResult(tool_calls=[call("answer_step", outcome="near", language="hi")]),
    ])
    head = [{"role": "system", "content": SYSTEM}, {"role": "assistant", "content": "नमस्ते! बताइए, क्या problem है?"},
            {"role": "user", "content": "AC में हवा कम आ रही है।"}]
    post = lambda msgs: _spoken(phone.post("/bolna/v1/chat/completions", headers=AUTH,
                                           json={"model": "fixfirst", "messages": msgs, "stream": True}))
    first = post(head)
    assert first == ac.node("ac04_near").text("hi")
    head.append({"role": "assistant", "content": first})
    assert post(head + [{"role": "user", "content": "हाँ।"}]) == ac.node("ac04_off").text("hi")
    # Bolna's next request: no assistant message after "हाँ।", so the switch-off step was never spoken.
    again = post(head + [{"role": "user", "content": "हाँ।"}, {"role": "user", "content": "AC के पास ही हूँ।"}])
    assert again == ac.node("ac04_off").text("hi")
    assert phone.llm.requests[-1]["messages"][-1]["content"] == "हाँ। AC के पास ही हूँ।"
    path = phone.get("/api/calls").json()[0]["path"]
    assert [p["node"] for p in path] == ["triage", "ac04_near"]   # the "are you near it?" step answered once


def test_bolna_ticket_message_talked_over_is_recapped_not_read_again(phone):
    # Live call 30 Oct: the caller kept talking ("मेरे दोस्त का हाथ लग गया था") over the ticket message, Bolna
    # appended it to their last message, and the whole ticket message was read a second time.
    phone.llm.responses.extend([
        LLMResult(tool_calls=[call("escalate_to_technician", reason="monitor broken", language="hi")]),
        LLMResult(tool_calls=[call("add_note", note="A friend's hand hit it", language="hi")]),
    ])
    head = [{"role": "system", "content": SYSTEM}, {"role": "assistant", "content": "नमस्ते! बताइए, क्या problem है?"}]
    post = lambda msgs: _spoken(phone.post("/bolna/v1/chat/completions", headers=AUTH,
                                           json={"model": "fixfirst", "messages": msgs, "stream": True}))
    first = post(head + [{"role": "user", "content": "उससे हाथ लग गया था।"}])
    assert "ticket raise कर दिया है" in first
    again = post(head + [{"role": "user", "content": "उससे हाथ लग गया था। मेरे दोस्त का हाथ लग गया था।"}])
    session = phone.app.state.store.get(next(iter(phone.app.state.bolna.calls.values())).session_id)
    code = " ".join(session.ticket["code"].replace("-", ""))
    assert again.startswith(f"आपका ticket {code} raise हो गया है") and "ticket raise कर दिया है" not in again
    assert "A friend's hand hit it" in session.ticket["summary"]["notes"]
    assert phone.llm.requests[-1]["messages"][-1]["content"] == "मेरे दोस्त का हाथ लग गया था।"


def test_bolna_a_line_added_to_the_ticket_is_not_undone_when_talked_over(phone):
    # Live call 31 Oct: AC booked, the fridge joined the ticket, and "वॉशिंग मशीन में भी" came over the fridge's
    # reply. The fridge turn was undone (the call already had a ticket, so nothing seemed to change) and its
    # replay read the whole ticket message again.
    phone.llm.responses.extend([
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="fridge", symptom="not_cooling", language="hi")]),
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="washer", symptom="unknown", language="hi")]),
    ])
    system = {"role": "system", "content": "FIXFIRST_CALL call_ref=exec-31 from=+919811122233"}
    greet = {"role": "assistant", "content": "नमस्ते!"}
    post = lambda msgs: _spoken(phone.post("/bolna/v1/chat/completions", headers=AUTH,
                                           json={"model": "fixfirst", "messages": [system, greet] + msgs, "stream": True}))
    u, a = (lambda t: {"role": "user", "content": t}), (lambda t: {"role": "assistant", "content": t})
    ac = post([u("एसी कूलिंग कम कर रहा है।")])
    fridge = post([u("एसी कूलिंग कम कर रहा है।"), a(ac), u("फ्रिज भी कूलिंग नहीं कर रहा।")])
    assert "जोड़ दिया है" in fridge
    washer = post([u("एसी कूलिंग कम कर रहा है।"), a(ac), u("फ्रिज भी कूलिंग नहीं कर रहा।"),
                   u("वॉशिंग मशीन में भी दिक्कत है।")])
    session = phone.app.state.store.get(next(iter(phone.app.state.bolna.calls.values())).session_id)
    code = " ".join(session.earlier_tickets[-1]["code"].replace("-", ""))
    assert washer.startswith(f"आपका ticket {code} raise हो गया है") and "ticket raise कर दिया है" not in washer
    assert phone.llm.requests[-1]["messages"][-1]["content"] == "वॉशिंग मशीन में भी दिक्कत है।"
    assert [i["appliance"] for i in session.earlier_tickets[-1]["summary"]["items"]] == ["ac", "fridge"]


def test_bolna_end_call_tool_is_called_after_the_goodbye(phone):
    # Live calls 26 Oct: the goodbye was spoken but Bolna's hang-up check never fired, so the line stayed open.
    phone.llm.responses.append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", caller_requested=True,
                                                          language="hi")]))
    tools = [{"type": "function", "function": {"name": "end_call", "parameters": {}}}]
    first = "mera AC kharab hai, technician bhej do"
    r = phone.post("/bolna/v1/chat/completions", json={**_body(first), "tools": tools}, headers=AUTH)
    booked = _spoken(r)
    assert '"end_call"' not in r.text   # "anything else?" is a question: the call stays up
    r = phone.post("/bolna/v1/chat/completions", json={**_body(first, booked, "नहीं बस"), "tools": tools}, headers=AUTH)
    assert "Have a good day" in _spoken(r)
    chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    calls = [tc for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert [tc["function"]["name"] for tc in calls] == ["end_call"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    # Without the tool on offer, the reply ends normally and Bolna's hang-up prompt has to notice the goodbye.
    r = phone.post("/bolna/v1/chat/completions", json=_body(first, booked, "नहीं बस", "...", "ok bye", stream=False),
                   headers=AUTH)
    assert r.json()["choices"][0]["finish_reason"] == "stop"


def test_bolna_handoff_in_hours_calls_the_transfer_tool(phone, monkeypatch):
    from fixfirst.config import Tenant
    monkeypatch.setattr(Tenant, "in_human_hours", lambda self, at=None: True)
    tools = [{"type": "function", "function": {"name": "transfer_call_care", "parameters": {}}},
             {"type": "function", "function": {"name": "end_call", "parameters": {}}}]
    r = phone.post("/bolna/v1/chat/completions", json={**_body("mujhe kisi insaan se baat karni hai"), "tools": tools},
                   headers=AUTH)
    assert "connect कर रही हूँ" in _spoken(r)
    chunks = [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: {")]
    calls = [tc["function"]["name"] for c in chunks for tc in c["choices"][0]["delta"].get("tool_calls", [])]
    assert calls == ["transfer_call_care"]


def test_test_callers_ring_in_as_a_demo_customer(phone):
    phone.app.state.bolna.test_callers["9811111111"] = "C1"   # TEST_CALLERS="9811111111=C1"
    phone.llm.responses.append(LLMResult(tool_calls=[call("say", text="Ji, bataiye.", language="hi")]))
    system = "FIXFIRST_CALL call_ref=exec-9 from=+919811111111 to=+918000000000 customer_phone={customer_phone}"
    body = {"model": "fixfirst", "stream": False, "messages": [
        {"role": "system", "content": system}, {"role": "assistant", "content": "नमस्ते!"},
        {"role": "user", "content": "AC kharab hai"}]}
    phone.post("/bolna/v1/chat/completions", json=body, headers=AUTH)
    session = next(s for s in phone.app.state.store.sessions.values() if s.caller_phone and "9811111111" in s.caller_phone)
    assert session.customer["id"] == "C1" and session.test_call


def test_bolna_merging_the_callers_first_pieces_stays_one_call(phone):
    # Live call 30 Oct: "आप किसी।" then Bolna sent "आप किसी। हाँ। …" as the first message; the conversation hash
    # changed, a second session opened, the hang-up reached the first one, and no WhatsApp went out.
    from fixfirst.bolna import webhook_token
    hook = f"/bolna/webhook/{webhook_token('ff_test')}"
    phone.post(hook, json={"id": "exec-77", "status": "initiated", "telephony_data": {"from_number": "+919810000000"}})
    system = "FIXFIRST_CALL call_ref={execution_id}"
    greet = {"role": "assistant", "content": "नमस्ते! बताइए, क्या problem है?"}
    post = lambda first: phone.post("/bolna/v1/chat/completions", headers=AUTH, json={
        "model": "fixfirst", "stream": True,
        "messages": [{"role": "system", "content": system}, greet, {"role": "user", "content": first}]})
    phone.llm.responses.extend([
        LLMResult(tool_calls=[call("say", text="जी, बताइए।", language="hi")]),
        LLMResult(tool_calls=[call("escalate_to_technician", reason="AC not cooling", language="hi")]),
    ])
    post("आप किसी।")
    post("आप किसी। हाँ। मेरा AC ठंडा नहीं कर रहा।")
    sessions = {c.session_id for c in phone.app.state.bolna.calls.values()}
    assert len(sessions) == 1
    session = phone.app.state.store.get(sessions.pop())
    assert "आप किसी। हाँ। मेरा AC ठंडा नहीं कर रहा।" in [m["text"] for m in session.transcript if m["role"] == "user"]
    assert session.ticket
    phone.post(hook, json={"id": "exec-77", "status": "completed"})
    assert [m for m in phone.get("/api/outbox").json() if m["audience"] == "customer"]   # held WhatsApp went out


def test_transfer_result_reads_bolnas_transfer_report():
    from fixfirst.bolna import transfer_result
    # Live call 31 Oct: "transfer-completed", but the care number was busy and nobody picked up.
    busy = {"status": "transfer-completed", "duration": "0", "hangup_by": "Carrier",
            "hangup_reason": "Call recipient was busy", "termination_reason": "busy"}
    assert transfer_result({"transfer_call_data": busy}) == "failed: busy"
    assert transfer_result({"transfer_call_data": {"status": "transfer-completed", "duration": "42"}}) == "connected"
    assert transfer_result({"transfer_call_data": {"status": "transfer-initiated", "duration": "0"}}) is None
    assert transfer_result({"transfer_call_data": None}) is None and transfer_result({}) is None


def test_a_failed_transfer_is_on_the_call_and_the_ticket(phone):
    phone.post("/bolna/v1/chat/completions", json=_body("gas ki smell aa rahi hai kitchen mein"), headers=AUTH)
    hook = f"/bolna/webhook/{webhook_token('ff_test')}"
    phone.post(hook, json={"id": "exec-1", "status": "completed", "transfer_call_data": {
        "status": "transfer-completed", "duration": "0", "termination_reason": "busy"}})
    assert phone.get("/api/calls").json()[0]["transfer_result"] == "failed: busy"
    [ticket] = phone.get("/api/tickets").json()
    assert "Transfer to customer care failed (busy): call the customer back." in ticket["summary"]["notes"]
