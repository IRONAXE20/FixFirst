"""Bolna runs the phone call (telephony, Sarvam speech-to-text and text-to-speech, turn-taking) and calls
FixFirst as its "custom LLM": an OpenAI-compatible /chat/completions endpoint. Each request carries the
whole conversation; we feed only the caller's new words to the brain, which keeps its own state.

Bolna sends no call id to a custom LLM. The agent's prompt asks for Bolna's variables
("call_ref={execution_id} from={from_number} to={to_number}"), but in live tests Bolna left them unfilled.
So the bridge also learns calls from Bolna's status webhooks, which arrive before the first turn and carry
the execution id and the phone numbers: a new conversation adopts the most recent unclaimed execution.
"""
import asyncio
import copy
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
MERGE_SECONDS = 120   # a conversation whose first words extend a call this recent is that call
FINAL_STATUSES = {"completed", "no-answer", "busy", "failed", "canceled", "cancelled", "stopped", "error", "balance-low"}
_HANGUP_CHECK = re.compile(r"hang ?up|cut the call|end the call|conversation (is )?(complete|over|ended)|\"hangup\"", re.I)
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


def _find_key(node, key: str):
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for v in node.values():
            found = _find_key(v, key)
            if found:
                return found
    elif isinstance(node, list):
        for v in node:
            found = _find_key(v, key)
            if found:
                return found
    return None


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

    test = _find_key(payload, "test_customer_phone")
    if test and mobile10(str(test)):
        return [str(test)]   # `bolna-call --as`: look the caller up as this customer only
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
    last_user: str = ""                 # the caller's latest message as Bolna last sent it
    last_speech: str = ""
    last_words: str = ""                # the caller's words behind last_speech
    checkpoint: dict | None = None      # session state from before last_speech's turn
    last_active: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def checkpoint(session) -> dict:
    """A copy of the session's state, to undo a turn whose reply the caller never heard. The playbook, the
    event log and the live-view subscribers are shared, not copied."""
    memo = {id(session.events): session.events, id(session._subscribers): session._subscribers}
    if session.run:
        memo[id(session.run.playbook)] = session.run.playbook
    return copy.deepcopy(session.__dict__, memo)


def _ticket_changed(before: dict, session) -> bool:
    """A new ticket, or a new line on the call's ticket (live call 31 Oct: the fridge joined the AC's ticket, the
    turn was undone as if nothing happened, and its replay read the whole ticket message again)."""
    lines = lambda t: (t or {}).get("summary", {}).get("items")
    return bool(session.ticket) and (not before["ticket"] or lines(session.ticket) != lines(before["ticket"])
                                     or len(session.earlier_tickets) != len(before["earlier_tickets"]))


def _side_effects(before: dict, session) -> bool:
    """Did the turn do something that can't be taken back (a ticket, a safety alert, an ending)?"""
    return bool(_ticket_changed(before, session) or (session.hazard and not before["hazard"])
                or (session.closed and not before["closed"]) or (session.end_requested and not before["end_requested"]))


class BolnaBridge:
    def __init__(self, *, store, brain_for, sync_customer, tenant, test_callers: dict | None = None):
        self.store = store
        self.test_callers = test_callers or {}   # your own numbers -> a demo customer id (TEST_CALLERS)
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
        last_user = users[-1] if users else ""
        # A check sent inside our own conversation would carry Bolna's JSON answer format; a caller saying
        # "cut the call" must still reach the brain.
        if MARKER not in system or '"hangup"' in last_user:
            return self.side_check(system, messages)   # Bolna's own check, not the caller's conversation
        cv = call_vars(system)
        key = cv.get("call_ref") or self._fallback_key(convo)
        async with self._open_lock:   # two first requests for one call must not open two sessions
            call = self._find(key, convo) or await self._open(key, cv, convo)
        async with call.lock:
            call.last_active = time.monotonic()
            brain = self.brain_for(self.store.get(call.session_id))
            fresh = users[call.users_seen:]
            appended = False
            if not fresh and users and call.last_user and users[-1] != call.last_user                     and users[-1].startswith(call.last_user):
                # Bolna added the caller's next piece to their last message instead of a new one (live call
                # 30 Oct: "आप किसी।" became "आप किसी। हाँ। हो सकती है।"): the extra words are new, and our reply
                # to the first piece wasn't heard.
                fresh, appended = [users[-1][len(call.last_user):].strip()], True
            new = [u for u in fresh if u]
            call.users_seen = len(users)
            call.last_user = users[-1] if users else ""
            if not new:
                return call.last_speech   # a re-ask without new words (silence nudge or retry)
            words = " ".join(new)
            if call.last_speech and call.checkpoint and (appended or not self._was_heard(convo, call.last_speech, len(fresh))):
                # Callers pause mid-sentence ("नहीं यार।" … "हाँ, सब कुछ नहीं है।"); Bolna sends each piece as a
                # turn and drops our reply when they keep talking. That reply's step was never heard, so its
                # piece wasn't an answer to it: undo the turn and answer the whole sentence instead.
                session = brain.s
                before = call.checkpoint
                if (session.hazard and not before["hazard"]) or (session.end_requested and not before["end_requested"]):
                    # A safety alert or goodbye can't be undone: say it again so it's actually heard.
                    session.say("user", words)
                    session.messages.append({"role": "user", "content": words})
                    session.publish("interrupted", repeated=True)
                    return call.last_speech
                if _side_effects(before, session):
                    # A ticket can't be undone either, but its long message isn't read again (live call 30 Oct:
                    # the caller talked over it and heard all of it twice): answer the new words, with a
                    # one-line reminder of the ticket.
                    recap = brain.ticket_recap() if _ticket_changed(before, session) else ""
                    session.publish("interrupted", recapped=bool(recap))
                    call.checkpoint, call.last_words = checkpoint(session), words
                    result = await brain.respond(words)
                    call.last_speech = f"{recap} {result.speech}".strip()
                    return call.last_speech
                session.__dict__.update(call.checkpoint)
                session.publish("interrupted", merged=call.last_words)
                logger.info(f"[{session.id}] reply not heard; answering the caller's pieces together")
                words = f"{call.last_words} {words}"
            call.checkpoint, call.last_words = checkpoint(brain.s), words
            result = await brain.respond(words)
            call.last_speech = result.speech
            return result.speech

    def _find(self, key: str, convo: list[dict]) -> BolnaCall | None:
        """The call this conversation belongs to. Without a call_ref the key hashes the caller's first message,
        which changes when Bolna merges their opening pieces: then it's the recent call whose first words this
        one extends (live call 30 Oct: one call became two sessions, and the real one never got its hang-up)."""
        call = self.calls.get(key)
        if call or not key.startswith("anon-"):
            return call
        first = next((_text(m.get("content")) for m in convo if m.get("role") == "user"), "")
        now = time.monotonic()
        for c in sorted(self.calls.values(), key=lambda c: c.last_active, reverse=True):
            if (now - c.last_active < MERGE_SECONDS and c.first_words and first and c.key.startswith("anon-")
                    and (first.startswith(c.first_words) or c.first_words.startswith(first))):
                session = self.store.get(c.session_id)
                if session and not session.ended:
                    self.calls[key] = c
                    return c
        return None

    def _session_of(self, messages: list[dict]):
        system = next((_text(m.get("content")) for m in messages if m.get("role") == "system"), "")
        convo = [m for m in messages if m.get("role") in ("user", "assistant")]
        call = self._find(call_vars(system).get("call_ref") or self._fallback_key(convo), convo)
        return self.store.get(call.session_id) if call else None

    def call_ends(self, messages: list[dict]) -> bool:
        """After this reply, should the call end? Yes once the caller is done and the goodbye was said."""
        session = self._session_of(messages)
        return bool(session and session.end_requested)

    def call_transfers(self, messages: list[dict]) -> bool:
        """After this reply, should Bolna transfer the call to a person (handoff in human hours)? Asked once."""
        session = self._session_of(messages)
        if session and session.transfer_requested:
            session.transfer_requested = False
            session.publish("transfer")
            return True
        return False

    def side_check(self, system: str, messages: list[dict]) -> str:
        """Bolna's hangup-using-a-prompt check (hangup_after_LLMCall) asks after each reply whether to end the
        call. We answer from the call's own state: yes only after the caller declined "anything else?" and
        heard the goodbye. Bolna's prompt asks for {"hangup": "Yes"|"No"}."""
        text = " ".join(_text(m.get("content")) for m in messages)
        call = max((c for c in self.calls.values() if c.first_words and c.first_words in text),
                   key=lambda c: c.last_active, default=None)
        session = self.store.get(call.session_id) if call else None
        hangup = bool(session and session.end_requested)
        logger.info(f"Bolna side check: system={system[:90]!r} roles={[m.get('role') for m in messages][-6:]} "
                    f"call={'matched' if call else 'unmatched'} hangup={hangup}")
        if not call and not _HANGUP_CHECK.search(text):
            return ""
        return json.dumps({"hangup": "Yes" if hangup else "No"})

    def _claim_pending(self) -> tuple[str | None, list[str], asyncio.Task | None]:
        """The most recent execution a webhook announced that no conversation has claimed yet."""
        now = time.monotonic()
        for eid in [e for e, p in self.pending.items() if now - p["at"] > PENDING_SECONDS]:
            self.pending.pop(eid, None)
        if not self.pending:
            return None, [], None
        eid = max(self.pending, key=lambda e: self.pending[e]["at"])
        entry = self.pending.pop(eid)
        self._claimed_test = bool(entry.get("test"))
        return eid, entry["phones"], entry.get("lookup")

    async def _lookup(self, phones: list[str]) -> tuple[str | None, str | None]:
        """(customer id, the number it matched) for the first of `phones` RentBasket knows, looked up in parallel."""
        unique = list(dict.fromkeys(p for p in phones if mobile10(p)))
        ids = await asyncio.gather(*(self.sync_customer(p) for p in unique), return_exceptions=True)
        for phone, cid in zip(unique, ids):   # inbound: the caller is `from`; outbound: `to` / customer_phone
            if isinstance(cid, str) and cid:
                return cid, phone
        return None, unique[0] if unique else None

    async def _open(self, key: str, cv: dict, convo: list[dict]) -> BolnaCall:
        self._claimed_test = False
        execution_id, hook_phones, prefetched = ((cv.get("call_ref"), [], None) if cv.get("call_ref")
                                                 else self._claim_pending())
        phones = [p for p in (cv.get("customer_phone"), cv.get("from"), cv.get("to"), *hook_phones) if mobile10(p)]
        test_as = next((self.test_callers[mobile10(p)] for p in phones if mobile10(p) in self.test_callers), None)
        if test_as:   # TEST_CALLERS: your own number calls in as a demo customer; nothing goes to real customers
            customer_id, caller = test_as, next(p for p in phones if mobile10(p) in self.test_callers)
            self._claimed_test = True
        elif prefetched and not cv.get("customer_phone"):
            customer_id, caller = await prefetched   # started when Bolna's webhook announced the call
        else:
            customer_id, caller = await self._lookup(phones)
        session = self.store.create(channel="phone" if phones else "web", customer_id=customer_id, caller_phone=caller)
        session.test_call = self._claimed_test   # `bolna-call --as`: nothing is sent to the real customer
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
    def _was_heard(convo: list[dict], last_speech: str, fresh_users: int) -> bool:
        """Bolna's history keeps only what was actually spoken. Our last reply was heard if an assistant
        message follows the caller words it answered, and it wasn't cut short."""
        tail_users, last_assistant = 0, None
        for m in reversed(convo):
            if m.get("role") == "assistant":
                last_assistant = _text(m.get("content"))
                break
            tail_users += m.get("role") == "user"
        if tail_users > fresh_users:   # the caller's earlier piece has no reply after it: ours was dropped
            return False
        return last_assistant is not None and len(last_assistant) >= 0.7 * len(last_speech)

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
                entry["test"] = entry.get("test") or bool(_find_key(payload, "test_customer_phone"))
                if entry["phones"] and "lookup" not in entry:
                    # The first turn comes ~40 s later; look the caller up now so it isn't waiting on this.
                    entry["lookup"] = asyncio.create_task(self._lookup(entry["phones"]))
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
        transfer = transfer_result(payload)
        if transfer and transfer != session.transfer_result:
            # Live call 31 Oct: the care number was busy, the caller heard nothing more and hung up.
            session.transfer_result = transfer
            session.publish("transfer_result", result=transfer)
            logger.info(f"[{session.id}] transfer to customer care: {transfer}")
            if transfer != "connected" and session.ticket:
                self.brain_for(session).tickets.add_note(
                    session.ticket, f"Transfer to customer care failed ({transfer.split(': ', 1)[-1]}): call the customer back.")
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


def transfer_result(payload: dict) -> str | None:
    """How Bolna's transfer to customer care went: "connected", "failed: busy" (no-answer, ...), or None while
    there's no transfer or it hasn't finished. Bolna says "transfer-completed" even when the care number was
    busy, so the length of the transferred call decides."""
    t = payload.get("transfer_call_data")
    if not isinstance(t, dict) or not t:
        return None
    try:
        seconds = float(t.get("duration") or 0)
    except (TypeError, ValueError):
        seconds = 0
    if seconds > 0:
        return "connected"
    reason = t.get("termination_reason") or t.get("hangup_reason")
    return f"failed: {reason}"[:40] if reason else None


def sse_chunks(speech: str, model: str, end_tool: str | None = None):
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
    if end_tool:
        # After the goodbye text: Bolna's own end-call tool, which hangs up once the goodbye has played.
        yield chunk({"tool_calls": [_end_call(end_tool, index=0)]})
        yield chunk({}, "tool_calls")
    else:
        yield chunk({}, "stop")
    yield "data: [DONE]\n\n"


def _offered_tool(body: dict, prefix: str) -> str | None:
    for t in body.get("tools") or []:
        name = ((t or {}).get("function") or {}).get("name") or ""
        if name.startswith(prefix):
            return name
    return None


def end_call_tool(body: dict) -> str | None:
    """The name of the end-call tool Bolna offered in this request ("end_call..."), if any."""
    return _offered_tool(body, "end_call")


def transfer_call_tool(body: dict) -> str | None:
    """The name of the transfer tool bolna-setup put on the agent ("transfer_call..."), if Bolna offered it."""
    return _offered_tool(body, "transfer_call")


def _end_call(name: str, index: int | None = None) -> dict:
    """Our call of one of Bolna's own tools: end_call after the goodbye, or transfer_call after "connecting you"
    (Bolna fills the call id and the care number from the tool's settings)."""
    args = ({"call_sid": "", "reason": "The caller needs a person"} if name.startswith("transfer_call")
            else {"reason": "The caller is done and heard the goodbye"})
    tc = {"id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
          "function": {"name": name, "arguments": json.dumps(args)}}
    return {"index": index, **tc} if index is not None else tc


def completion_body(speech: str, model: str, end_tool: str | None = None) -> dict:
    message = {"role": "assistant", "content": speech}
    if end_tool:
        message["tool_calls"] = [_end_call(end_tool)]
    return {"id": f"chatcmpl-{uuid.uuid4().hex[:16]}", "object": "chat.completion", "created": int(time.time()),
            "model": model, "choices": [{"index": 0, "message": message,
                                         "finish_reason": "tool_calls" if end_tool else "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
