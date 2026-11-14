"""Plivo carries the phone call when FixFirst runs its own voice pipeline (no Bolna). Plivo is the carrier Bolna
used underneath, has Indian numbers, and Pipecat speaks its audio stream natively.

The call, step by step:
1. A customer dials the number; Plivo asks POST /plivo/answer/<token> what to do. We answer with XML: record
   the whole call, and stream its audio both ways over wss://<host>/ws/plivo/<token>.
2. The stream runs our pipeline (voice.py): greeting, turns, the goodbye. After the goodbye the serializer
   hangs up through Plivo's REST API.
3. A transfer to customer care (9am-8pm handoff) asks Plivo's REST API to move the call to
   /plivo/transfer/<token>, whose XML dials the care number; /plivo/dial-status/<token> reports how it went.
4. /plivo/recording/<token> receives the recording's URL, which goes on the call's ticket.

The <token> is derived from PLIVO_AUTH_TOKEN, so only Plivo (configured by `python -m fixfirst plivo-setup`)
knows the URLs. `PLIVO_AUTH_ID` and `PLIVO_AUTH_TOKEN` come from the Plivo console.
"""
import hashlib
import hmac
import time
from xml.sax.saxutils import escape, quoteattr

import httpx
from loguru import logger

API = "https://api.plivo.com/v1/Account"
APP_NAME = "FixFirst"
PENDING_SECONDS = 600


def url_token(auth_token: str) -> str:
    return hmac.new(auth_token.encode(), b"fixfirst-plivo", hashlib.sha256).hexdigest()[:32]


def ws_url(public_url: str, token: str) -> str:
    base = public_url.rstrip("/")
    return ("wss://" + base[8:] if base.startswith("https://") else "ws://" + base[7:]) + f"/ws/plivo/{token}"


def answer_xml(public_url: str, token: str) -> str:
    base = public_url.rstrip("/")
    recording = quoteattr(f"{base}/plivo/recording/{token}")
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Record recordSession="true" redirect="false" fileFormat="mp3" maxLength="3600" '
            f'action={recording} callbackUrl={recording} callbackMethod="POST"/>'
            '<Stream bidirectional="true" keepCallAlive="true" contentType="audio/x-mulaw;rate=8000">'
            f'{escape(ws_url(base, token))}</Stream></Response>')


def transfer_xml(public_url: str, token: str, care_number: str, caller_id: str | None) -> str:
    action = quoteattr(f"{public_url.rstrip('/')}/plivo/dial-status/{token}")
    caller = f" callerId={quoteattr(caller_id)}" if caller_id else ""
    number = "".join(ch for ch in care_number if ch.isdigit())
    number = "91" + number if len(number) == 10 else number
    return ('<?xml version="1.0" encoding="UTF-8"?><Response>'
            f'<Dial action={action} method="POST" redirect="false" timeout="30"{caller}>'
            f'<Number>{escape(number)}</Number></Dial></Response>')


def reject_xml() -> str:
    return '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup reason="rejected"/></Response>'


def dial_result(form: dict) -> str:
    """Plivo's Dial callback -> "connected" or "failed: <reason>" (busy, no-answer, timeout, failed...)."""
    status = (form.get("DialStatus") or "").lower()
    duration = form.get("DialBLegDuration") or form.get("DialBLegBillDuration")
    try:
        talked = float(duration) > 0 if duration not in (None, "") else status == "completed"
    except ValueError:
        talked = status == "completed"
    return "connected" if status == "completed" and talked else f"failed: {status or 'unknown'}"[:40]


class PlivoCalls:
    """Calls on the Plivo line: who's calling (from the answer webhook, before the audio stream starts) and
    which FixFirst session each call is."""

    def __init__(self):
        self.pending: dict[str, dict] = {}    # call UUID -> {"from", "to", "at"}
        self.sessions: dict[str, str] = {}    # call UUID -> session id

    def answered(self, call_uuid: str, caller: str | None, ours: str | None) -> None:
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v["at"] < PENDING_SECONDS}
        self.pending[call_uuid] = {"from": caller, "to": ours, "at": now}

    def started(self, call_uuid: str, session_id: str) -> dict:
        self.sessions[call_uuid] = session_id
        return self.pending.get(call_uuid) or {}


class PlivoTelephony:
    """What the pipeline can ask of the phone line during a call (voice.py): move it to customer care."""

    def __init__(self, auth_id: str, auth_token: str, call_uuid: str, transfer_url: str):
        self.auth_id, self.auth_token, self.call_uuid, self.transfer_url = auth_id, auth_token, call_uuid, transfer_url

    async def transfer(self) -> None:
        async with httpx.AsyncClient(timeout=10, auth=(self.auth_id, self.auth_token)) as c:
            r = await c.post(f"{API}/{self.auth_id}/Call/{self.call_uuid}/",
                             json={"legs": "aleg", "aleg_url": self.transfer_url, "aleg_method": "POST"})
        if r.status_code >= 300:
            raise RuntimeError(f"Plivo transfer: HTTP {r.status_code} {r.text[:200]}")
        logger.info(f"Plivo call {self.call_uuid} moved to customer care")


def setup(auth_id: str, auth_token: str, public_url: str, number: str | None = None) -> str:
    """Create (or update) the Plivo Application that sends calls to FixFirst, and attach the number to it.
    Returns the application id."""
    if not public_url.startswith("https://"):
        raise SystemExit("--public-url must be the server's public https address (e.g. https://fixfirst-zgo0.onrender.com)")
    base, token = public_url.rstrip("/"), url_token(auth_token)
    app = {"app_name": APP_NAME, "answer_url": f"{base}/plivo/answer/{token}", "answer_method": "POST"}
    with httpx.Client(base_url=f"{API}/{auth_id}", auth=(auth_id, auth_token), timeout=30) as c:
        found = next((a for a in c.get("/Application/", params={"limit": 100}).json().get("objects", [])
                      if a.get("app_name") == APP_NAME), None)
        if found:
            app_id = found["app_id"]
            r = c.post(f"/Application/{app_id}/", json=app)
        else:
            r = c.post("/Application/", json=app)
            app_id = r.json().get("app_id") if r.status_code < 300 else None
        if r.status_code >= 300 or not app_id:
            raise SystemExit(f"Saving the Plivo application failed: HTTP {r.status_code} {r.text[:300]}")
        if number:
            digits = "".join(ch for ch in number if ch.isdigit())
            r = c.post(f"/Number/{digits}/", json={"app_id": app_id})
            if r.status_code >= 300:
                raise SystemExit(f"Attaching {number} failed: HTTP {r.status_code} {r.text[:300]}")
    return app_id
