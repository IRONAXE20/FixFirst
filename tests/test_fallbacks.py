import asyncio

from fixfirst.demo_llm import KeywordLLM, _code


def run(coro):
    return asyncio.run(coro)


def test_voice_failure_books_a_callback_once(env):
    brain, session, _ = env.make([])
    run(brain.voice_failure("SarvamTTSService failed: auth"))
    run(brain.voice_failure("SarvamSTTService failed: auth"))
    assert session.ticket["category"] == "system" and session.outcome == "escalated"


def test_voice_failure_never_overrides_a_safety_ticket(env):
    brain, session, _ = env.make([])
    run(brain.respond("plug se chingari nikal rahi hai"))
    run(brain.voice_failure("TTS down"))
    assert session.ticket["category"] == "safety"


def test_demo_brain_drives_a_typed_call(env):
    brain, session, _ = env.make([], language="en")
    brain.llm = KeywordLLM()
    run(brain.respond("my AC is not cooling"))
    assert session.run and session.run.current == "safety_check"
    run(brain.respond("no, nothing like that"))
    assert session.run.current == "mode_check"
    run(brain.respond("please just send a technician"))
    assert session.outcome == "escalated"


def test_demo_brain_reads_error_codes():
    assert _code("machine pe IE aa raha hai") == "IE"
    assert _code("it shows F05") == "F05"
    assert _code("Samsung 4C") == "4C"


def test_whatsapp_logs_mask_phone_numbers():
    from fixfirst.whatsapp import mask
    assert mask("+91 98765 43210") == "…3210" and mask(None) == "none"

