"""Bolna runs the phone call (telephony, Sarvam speech-to-text and text-to-speech, turn-taking) and calls
FixFirst as its "custom LLM": an OpenAI-compatible /chat/completions endpoint. Each request carries the
whole conversation; we feed only the caller's new words to the brain, which keeps its own state.

Bolna sends no call id to a custom LLM. The agent's prompt asks for Bolna's variables
("call_ref={execution_id} from={from_number} to={to_number}"), but in live tests Bolna left them unfilled.
So the bridge also learns calls from Bolna's status webhooks, which arrive before the first turn and carry
the execution id and the phone numbers: a new conversation adopts the most recent unclaimed execution.
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
PENDING_SECONDS = 5 * 60   # how long a webhook-announced execution waits for its first conversation turn
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


def webhook_phones(payload: dict) -> list[str]:
    """Phone numbers in an execution payload (telephony details and the user_data we passed), in order."""
    found: list[str] = []

    def walk(node, key=""):
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, str(k).lower())
        elif isinstance(node, list):
            for v in node:
                walk(v, key)
        elif isinstance(node, (str, int)) and any(w in key for w in ("number", "phone", "from", "to", "recipient", "caller")):
            if mobile10(str(node)) and str(node) not in found:
                found.append(str(node))

    walk({"context_details": payload.get("context_details"), "telephony_data": payload.get("telephony_data"),
          "user_data": payload.get("user_data")})
    return found


@dataclass
class BolnaCall:
    session_id: str
    key: str = ""
    execution_id: str | None = None
    first_words: str = ""
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
        self.by_execution: dict[str, str] = {}   # Bolna execution id -> key in self.calls
        self.pending: dict[str, dict] = {}       # executions seen in webhooks, not yet tied to a conversation
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

    def _claim_pending(self) -> tuple[str | None, list[str]]:
        """The most recent execution a webhook announced that no conversation has claimed yet."""
        now = time.monotonic()
        for eid in [e for e, p in self.pending.items() if now - p["at"] > PENDING_SECONDS]:
            self.pending.pop(eid, None)
        if not self.pending:
            return None, []
        eid = max(self.pending, key=lambda e: self.pending[e]["at"])
        return eid, self.pending.pop(eid)["phones"]

    async def _open(self, key: str, cv: dict, convo: list[dict]) -> BolnaCall:
        execution_id, hook_phones = (cv.get("call_ref"), []) if cv.get("call_ref") else self._claim_pending()
        phones = [p for p in (cv.get("customer_phone"), cv.get("from"), cv.get("to"), *hook_phones) if mobile10(p)]
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
        first = next((_text(m.get("content")) for m in convo if m.get("role") == "user"), "")
        call = BolnaCall(session_id=session.id, key=key, execution_id=execution_id, first_words=first)
        self.calls[key] = call
        if execution_id:
            self.by_execution[execution_id] = key
        session.publish("bolna", call_ref=key, execution_id=execution_id)
        logger.info(f"Bolna call {key} (execution {execution_id or 'unknown'}) -> session {session.id} "
                    f"(customer {'found' if customer_id else 'unknown'}, {len(phones)} number(s))")
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
    def _call_for(self, execution_id: str, payload: dict) -> BolnaCall | None:
        key = self.by_execution.get(execution_id) or execution_id
        if key in self.calls:
            return self.calls[key]
        # A conversation that started without an execution id: match it by the caller's first words.
        transcript = str(payload.get("transcript") or "")
        for call in self.calls.values():
            if not call.execution_id and call.first_words and call.first_words in transcript:
                call.execution_id = execution_id
                self.by_execution[execution_id] = call.key
                return call
        return None

    async def webhook(self, payload: dict) -> str:
        status = str(payload.get("status") or "").lower()
        execution_id = str(payload.get("id") or payload.get("execution_id") or "")
        td = payload.get("telephony_data") or {}
        logger.info(f"Bolna webhook status={status or '?'} execution={execution_id[:8] or '?'} "
                    f"fields={sorted(payload)[:20]} telephony={sorted(td) if isinstance(td, dict) else type(td).__name__}")
        if not execution_id:
            return "no execution id"
        call = self._call_for(execution_id, payload)
        if not call:
            if status not in FINAL_STATUSES:   # announced before its first turn: remember it for _open
                entry = self.pending.setdefault(execution_id, {"at": time.monotonic(), "phones": []})
                entry["phones"] = webhook_phones(payload) or entry["phones"]
            else:
                self.pending.pop(execution_id, None)
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
            self.calls.pop(call.key, None)
            self.by_execution.pop(execution_id, None)
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
