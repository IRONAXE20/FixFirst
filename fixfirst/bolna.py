"""Bolna runs the phone call (telephony, Sarvam speech-to-text and text-to-speech, turn-taking) and calls
FixFirst as its "custom LLM": an OpenAI-compatible /chat/completions endpoint. Each request carries the
whole conversation; we feed only the caller's new words to the brain, which keeps its own state.

Bolna sends no call id to a custom LLM, so the agent's system prompt (see bolna_admin.py) carries Bolna's
prompt variables, e.g. "call_ref={execution_id} from={from_number} to={to_number}"; we read them back here.
"""
import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from dataclasses import dataclass, field

from loguru import logger

from .rentbasket import mobile10

IDLE_END_SECONDS = 20 * 60
MARKER = "FIXFIRST_CALL"   # first word of the agent's system prompt (bolna_admin.py)
FINAL_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled", "cancelled", "stopped", "error", "balance-low"}
_VAR = re.compile(r"\b(call_ref|from|to|customer_phone|customer_name)=(\S+)")


def webhook_token(llm_key: str) -> str:
    """The webhook URL's secret path part. Derived from FIXFIRST_LLM_KEY, but not the key itself: request
    logs (Render's included) record the full URL, and the key must not end up there."""
    return hmac.new(llm_key.encode(), b"fixfirst-bolna-webhook", hashlib.sha256).hexdigest()[:32]


def call_vars(system_prompt: str) -> dict:
    """Variables Bolna substituted into the prompt. An unsubstituted one stays literal ("{execution_id}")."""
    found = {k: v for k, v in _VAR.findall(system_prompt or "")}
    return {k: v for k, v in found.items() if not (v.startswith("{") and v.endswith("}"))}


def _text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict)).strip()
    return ""


@dataclass
class BolnaCall:
    session_id: str
    users_seen: int = 0
    last_speech: str = ""
    presented_before_last: set[str] = field(default_factory=set)
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class BolnaBridge:
    def __init__(self, *, store, brain_for, sync_customer, tenant):
        self.store = store
        self.brain_for = brain_for
        self.sync_customer = sync_customer   # async (phone) -> local customer id or None
        self.tenant = tenant
        self.calls: dict[str, BolnaCall] = {}
        self._open_lock = asyncio.Lock()

    # ---- conversation turns ------------------------------------------------------------
    async def turn(self, messages: list[dict]) -> str:
        system = next((_text(m.get("content")) for m in messages if m.get("role") == "system"), "")
        convo = [m for m in messages if m.get("role") in ("user", "assistant")]
        users = [_text(m.get("content")) for m in convo if m.get("role") == "user"]
        if MARKER not in system:
            return ""   # one of Bolna's own side checks, not the caller's conversation
        cv = call_vars(system)
        key = cv.get("call_ref") or self._fallback_key(convo)
        async with self._open_lock:   # two first requests for one call must not open two sessions
            call = self.calls.get(key) or await self._open(key, cv, convo)
        async with call.lock:
            call.last_active = time.monotonic()
            brain = self.brain_for(self.store.get(call.session_id))
            new = [u for u in users[call.users_seen:] if u]
            call.users_seen = len(users)
            if not new:
                return call.last_speech   # a re-ask without new words (silence nudge or retry)
            if call.last_speech and self._was_cut_off(convo, call.last_speech):
                self._mark_unheard(brain, call.presented_before_last)
            call.presented_before_last = set(brain.s.presented)
            result = await brain.respond(" ".join(new))
            call.last_speech = result.speech
            return result.speech

    def stream_cancelled(self, messages: list[dict]) -> None:
        """Bolna dropped the stream (the caller talked over the reply): the new step wasn't heard."""
        system = next((_text(m.get("content")) for m in messages if m.get("role") == "system"), "")
        call = self.calls.get(call_vars(system).get("call_ref") or "")
        if call:
            session = self.store.get(call.session_id)
            if session:
                self._mark_unheard(self.brain_for(session), call.presented_before_last)

    async def _open(self, key: str, cv: dict, convo: list[dict]) -> BolnaCall:
        phones = [p for p in (cv.get("customer_phone"), cv.get("from"), cv.get("to")) if mobile10(p)]
        customer_id, caller = None, phones[0] if phones else None
        for phone in phones:   # inbound: the caller is `from`; outbound: `to` (or customer_phone we passed)
            customer_id = await self.sync_customer(phone)
            if customer_id:
                caller = phone
                break
        session = self.store.create(channel="phone" if phones else "web", customer_id=customer_id, caller_phone=caller)
        brain = self.brain_for(session)
        welcome = _text(convo[0].get("content")) if convo and convo[0].get("role") == "assistant" else ""
        brain.adopt_greeting(welcome) if welcome else brain.greeting()
        call = BolnaCall(session_id=session.id)
        self.calls[key] = call
        session.publish("bolna", call_ref=key)
        logger.info(f"Bolna call {key} -> session {session.id} (customer {'found' if customer_id else 'unknown'})")
        return call

    @staticmethod
    def _fallback_key(convo: list[dict]) -> str:
        # Without call_ref (e.g. a dashboard test chat), the opening exchange identifies the conversation.
        head = "|".join(_text(m.get("content")) for m in convo[:2])
        return "anon-" + hashlib.sha1(head.encode("utf-8")).hexdigest()[:12]

    @staticmethod
    def _was_cut_off(convo: list[dict], last_speech: str) -> bool:
        """Bolna's history keeps what was actually spoken; a much shorter last reply means an interruption."""
        last_assistant = next((_text(m.get("content")) for m in reversed(convo) if m.get("role") == "assistant"), None)
        if last_assistant is None:
            return False
        return len(last_assistant) < 0.7 * len(last_speech)

    @staticmethod
    def _mark_unheard(brain, presented_before: set[str]) -> None:
        brain.s.presented = set(presented_before)
        brain.s.publish("interrupted")

    # ---- call end ------------------------------------------------------------------------
    async def webhook(self, payload: dict) -> str:
        status = str(payload.get("status") or "").lower()
        call = None
        execution_id = str(payload.get("id") or payload.get("execution_id") or "")
        if execution_id:
            call = self.calls.get(execution_id)
        if not call:
            return "unknown call"
        session = self.store.get(call.session_id)
        if not session:
            return "session gone"
        recording = (payload.get("telephony_data") or {}).get("recording_url")
        if recording and not getattr(session, "recording_url", None):
            session.recording_url = recording
            session.publish("recording", url=recording)
        if status in FINAL_STATUSES:
            brain = self.brain_for(session)
            await brain.on_hangup()
            if recording and session.ticket:
                brain.tickets.add_note(session.ticket, f"Call recording: {recording}")
            self.calls.pop(execution_id, None)
        return status or "ok"

    async def end_idle(self) -> int:
        """Close calls Bolna never reported as finished (no webhook configured, or it failed)."""
        ended = 0
        for key, call in list(self.calls.items()):
            if time.monotonic() - call.last_active > IDLE_END_SECONDS:
                session = self.store.get(call.session_id)
                if session:
                    await self.brain_for(session).on_hangup()
                self.calls.pop(key, None)
                ended += 1
        return ended


def sse_chunks(speech: str, model: str):
    """OpenAI streaming chunks for a finished reply, split by sentence so text-to-speech can start early."""
    cid, created = f"chatcmpl-{uuid.uuid4().hex[:16]}", int(time.time())

    def chunk(delta: dict, finish=None) -> str:
        body = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        return f"data: {json.dumps(body, ensure_ascii=False)}\n\n"

    yield chunk({"role": "assistant", "content": ""})
    for part in re.split(r"(?<=[.!?।])\s+", speech.strip()):
        if part:
            yield chunk({"content": part + " "})
    yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


def completion_body(speech: str, model: str) -> dict:
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:16]}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": {"role": "assistant", "content": speech},
                                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
