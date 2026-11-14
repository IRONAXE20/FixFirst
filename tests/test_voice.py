"""Our own voice line (no Bolna): picking speech providers from the keys, the pipeline processor that hands
turns to the brain, and the Plivo phone line."""
import asyncio
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("pipecat")

from pipecat.frames.frames import (BotStoppedSpeakingFrame, EndWorkerFrame, InterruptionFrame, LLMContextFrame,
                                   LLMTextFrame)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.tests.utils import SleepFrame, run_test

import fixfirst.server as server
from fixfirst import plivo, rentbasket, speech
from fixfirst.config import Settings, Tenant
from fixfirst.llm import LLMResult, ScriptedLLM, call
from fixfirst.voice import FixFirstProcessor


def said(text: str) -> LLMContextFrame:
    return LLMContextFrame(context=LLMContext([{"role": "user", "content": text}]))


def spoken(frames) -> list[str]:
    return [f.text for f in frames if isinstance(f, LLMTextFrame)]


# ---- which providers --------------------------------------------------------------------------
@pytest.mark.parametrize("keys,stt,tts", [
    ({"sarvam_api_key": "sk"}, "sarvam", "sarvam"),
    ({"elevenlabs_api_key": "el"}, "elevenlabs", "elevenlabs"),
    ({"cartesia_api_key": "ca", "cartesia_voice_id": "v"}, "cartesia", "cartesia"),
    ({"deepgram_api_key": "dg"}, "deepgram", None),                  # Deepgram's voices don't speak Hindi
    ({"deepgram_api_key": "dg", "elevenlabs_api_key": "el"}, "elevenlabs", "elevenlabs"),
    ({"deepgram_api_key": "dg", "elevenlabs_api_key": "el", "stt_provider": "deepgram"}, "deepgram", "elevenlabs"),
    ({}, None, None),
])
def test_one_key_picks_the_speech_providers(keys, stt, tts):
    s = Settings(**keys)
    assert speech.pick(s) == (stt, tts)
    assert s.voice_ready == bool(stt and tts)
    assert (speech.missing(s) is None) == bool(stt and tts)


def test_every_provider_builds_its_pipecat_services():
    tenant = Tenant.load()
    for keys in ({"sarvam_api_key": "sk"}, {"elevenlabs_api_key": "el"},
                 {"cartesia_api_key": "ca", "cartesia_voice_id": "v"}):
        s = Settings(**keys)
        assert speech.make_stt(s) and speech.make_tts(s, tenant)
    pytest.importorskip("deepgram")
    assert speech.make_stt(Settings(deepgram_api_key="dg"))


# ---- the pipeline processor ---------------------------------------------------------------------
def _phone(env, responses):
    env.settings.identity_check = False
    return env.make(responses, channel="phone", phone="+91 98111 22233")


def test_a_caller_turn_goes_to_the_brain_and_its_reply_to_the_voice(env):
    brain, session, llm = _phone(env, [
        LLMResult(tool_calls=[call("start_troubleshooting", appliance="ac", symptom="unknown", language="hi")])])
    down, _ = asyncio.run(run_test(FixFirstProcessor(brain), frames_to_send=[said("मेरा AC ठंडा नहीं कर रहा")]))
    assert spoken(down) and session.appliance == "ac"
    assert llm.requests[0]["messages"][-1]["content"] == "मेरा AC ठंडा नहीं कर रहा"


def test_the_goodbye_ends_the_call(env):
    brain, session, llm = _phone(env, [])
    session.end_requested = False
    brain.respond = _fake_respond(brain, "धन्यवाद! Have a good day!", end=True)
    down, _ = asyncio.run(run_test(FixFirstProcessor(brain), frames_to_send=[said("बस, थैंक यू")],
                                   send_end_frame=False))
    assert spoken(down) == ["धन्यवाद! Have a good day!"]
    assert any(isinstance(f, EndWorkerFrame) for f in down)


def test_a_handoff_transfers_the_call_once_connect_has_played(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: True)
    brain, session, llm = _phone(env, [])
    line = FakeLine()
    down, _ = asyncio.run(run_test(FixFirstProcessor(brain, telephony=line),
                                   frames_to_send=[said("मुझे किसी इंसान से बात करनी है"), SleepFrame(0.3), BotStoppedSpeakingFrame()]))
    assert spoken(down) == [brain._phrase("handoff_now")]
    assert line.transfers == 1 and session.transfers_asked == 1 and not session.transfer_requested


def test_a_failed_transfer_goes_on_the_ticket(env, monkeypatch):
    monkeypatch.setattr(env.tenant.__class__, "in_human_hours", lambda self, at=None: True)
    brain, session, llm = _phone(env, [])
    asyncio.run(run_test(FixFirstProcessor(brain, telephony=FakeLine(fail=True)),
                         frames_to_send=[said("मुझे किसी इंसान से बात करनी है"), SleepFrame(0.3), BotStoppedSpeakingFrame()]))
    assert session.transfer_result == "failed: transfer error"
    assert any("Transfer to customer care failed" in n for n in session.ticket["summary"]["notes"])


def test_a_reply_the_caller_talked_over_is_answered_with_their_next_words(env):
    # Same rule as on Bolna (turns.py): the unheard turn is undone and both pieces are answered together.
    brain, session, llm = _phone(env, [LLMResult(content="जी, बताइए।"), LLMResult(content="ठीक है।")])
    down, _ = asyncio.run(run_test(FixFirstProcessor(brain), frames_to_send=[
        said("मेरा"), SleepFrame(0.3), InterruptionFrame(), SleepFrame(0.1), said("फ्रिज खराब है")]))
    assert llm.requests[-1]["messages"][-1]["content"] == "मेरा फ्रिज खराब है"


class FakeLine:
    def __init__(self, fail=False):
        self.transfers, self.fail = 0, fail

    async def transfer(self):
        self.transfers += 1
        if self.fail:
            raise RuntimeError("Plivo down")


def _fake_respond(brain, speech_text, end=False):
    from fixfirst.brain import TurnResult

    async def respond(text):
        brain.s.say("user", text)
        brain.s.end_requested = end
        return TurnResult(speech=speech_text)
    return respond


# ---- the Plivo phone line -------------------------------------------------------------------------
@pytest.fixture
def line(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")

    async def no_customer(_settings, _mobile):
        return None
    monkeypatch.setattr(rentbasket, "fetch", no_customer)
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'v.db').as_posix()}", whatsapp_mode="dry_run",
                        public_base_url="https://fixfirst.test", sarvam_api_key="sk_test",
                        plivo_auth_id="MA_TEST", plivo_auth_token="secret")
    app = server.create_app(settings, llm_factory=lambda: ScriptedLLM([]))
    with TestClient(app) as c:
        c.token, c.settings = plivo.url_token("secret"), settings
        yield c


def test_plivo_answers_with_a_recording_and_the_audio_stream(line):
    r = line.post(f"/plivo/answer/{line.token}", data={"CallUUID": "c-1", "From": "919811122233", "To": "918000000000"})
    xml = ET.fromstring(r.text)
    record, stream = xml.find("Record"), xml.find("Stream")
    assert record.get("recordSession") == "true" and record.get("callbackUrl").endswith(f"/plivo/recording/{line.token}")
    assert stream.text == f"wss://fixfirst.test/ws/plivo/{line.token}" and stream.get("bidirectional") == "true"
    assert line.app.state.plivo_calls.pending["c-1"]["from"] == "919811122233"
    assert line.post("/plivo/answer/wrong", data={"CallUUID": "c-2"}).status_code == 404


def test_plivo_rejects_calls_until_a_speech_key_is_set(line):
    line.settings.sarvam_api_key = ""
    xml = ET.fromstring(line.post(f"/plivo/answer/{line.token}", data={"CallUUID": "c-3"}).text)
    assert xml.find("Hangup") is not None and xml.find("Stream") is None


def test_plivo_transfer_dials_customer_care_from_our_number(line):
    line.post(f"/plivo/answer/{line.token}", data={"CallUUID": "c-4", "From": "919811122233", "To": "918000000000"})
    dial = ET.fromstring(line.post(f"/plivo/transfer/{line.token}", data={"CallUUID": "c-4"}).text).find("Dial")
    care = "".join(ch for ch in Tenant.load().care_number() if ch.isdigit())
    assert dial.find("Number").text.endswith(care[-10:]) and dial.get("callerId") == "918000000000"
    assert dial.get("action").endswith(f"/plivo/dial-status/{line.token}")


def test_plivo_reports_the_transfer_result_and_the_recording_on_the_call(line):
    store = line.app.state.store
    session = store.create(channel="phone", caller_phone="+91 98111 22233")
    session.ticket = {"code": "FF-0000", "summary": {"notes": []}}
    line.app.state.plivo_calls.started("c-5", session.id)
    line.post(f"/plivo/dial-status/{line.token}", data={"DialALegUUID": "c-5", "DialStatus": "busy"})
    assert session.transfer_result == "failed: busy"
    line.post(f"/plivo/recording/{line.token}", data={"CallUUID": "c-5", "RecordUrl": "https://rec/c5.mp3"})
    assert session.recording_url == "https://rec/c5.mp3"


def test_dial_results():
    assert plivo.dial_result({"DialStatus": "completed", "DialBLegDuration": "42"}) == "connected"
    assert plivo.dial_result({"DialStatus": "completed", "DialBLegDuration": "0"}) == "failed: completed"
    assert plivo.dial_result({"DialStatus": "no-answer"}) == "failed: no-answer"
    assert plivo.dial_result({}) == "failed: unknown"
