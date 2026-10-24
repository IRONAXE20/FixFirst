"""Per-call state and the live event stream the call page and ops view subscribe to."""
import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.engine import Engine

from .config import Tenant, now_utc
from .db import fetch_customer, fetch_customer_by_phone, fetch_rentals
from .playbooks import PlaybookRun

APPLIANCE_NAMES = {
    "ac": {"en": "AC", "hi": "AC"},
    "washer": {"en": "washing machine", "hi": "washing machine"},
    "fridge": {"en": "fridge", "hi": "fridge"},
    "geyser": {"en": "geyser", "hi": "geyser"},
    "tv": {"en": "TV", "hi": "TV"},
    "microwave": {"en": "microwave", "hi": "microwave"},
    "inverter": {"en": "inverter", "hi": "inverter"},
    "stove": {"en": "gas stove", "hi": "gas stove"},
    "water_purifier": {"en": "water purifier", "hi": "RO water purifier"},
    "washer_semi": {"en": "semi-automatic washing machine", "hi": "semi-automatic washing machine"},
    "induction": {"en": "induction cooktop", "hi": "induction"},
    "furniture": {"en": "furniture item", "hi": "furniture"},
}


def appliance_name(appliance: str | None, language: str) -> str:
    names = APPLIANCE_NAMES.get(appliance or "", {})
    return names.get(language, names.get("en", appliance or "appliance"))


@dataclass
class CallSession:
    id: str
    channel: str
    customer: dict | None
    rentals: list[dict]
    language: str
    caller_phone: str | None = None
    started_at: datetime = field(default_factory=now_utc)
    rental: dict | None = None
    appliance: str | None = None
    run: PlaybookRun | None = None
    hazard: str | None = None
    error_code: dict | None = None
    pending_code: bool = False
    safety_probed: bool = False
    safety_rejects: int = 0
    outcome: str = "in_progress"
    resolution: str | None = None
    ticket: dict | None = None
    transcript: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    latency: list[dict] = field(default_factory=list)
    photos: list[str] = field(default_factory=list)
    recording_url: str | None = None
    presented: set[str] = field(default_factory=set)
    closed: bool = False
    ended: bool = False
    llm_failures: int = 0
    stalls: int = 0
    events: list[dict] = field(default_factory=list)
    _subscribers: list[asyncio.Queue] = field(default_factory=list)

    # ---- events --------------------------------------------------------------------
    def publish(self, type_: str, **data) -> None:
        event = {"type": type_, "at": now_utc().isoformat(), **data}
        self.events.append(event)
        for q in list(self._subscribers):
            q.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        for e in self.events:
            q.put_nowait(e)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    # ---- helpers -------------------------------------------------------------------
    def say(self, role: str, text: str, **meta) -> None:
        entry = {"role": role, "text": text, "at": now_utc().isoformat(), **meta}
        self.transcript.append(entry)
        self.publish(role, text=text, **meta)

    def rental_by_id(self, rental_id: str | None) -> dict | None:
        return next((r for r in self.rentals if r["id"] == rental_id), None)

    def rentals_of(self, appliance: str) -> list[dict]:
        return [r for r in self.rentals if r["appliance"] == appliance]

    @property
    def first_name(self) -> str:
        return (self.customer or {}).get("name", "").split(" ")[0]

    def snapshot(self) -> dict:
        node = self.run.node if self.run else None
        return {
            "id": self.id, "channel": self.channel, "language": self.language,
            "customer": self.customer, "rentals": self.rentals, "rental": self.rental,
            "appliance": self.appliance, "outcome": self.outcome, "resolution": self.resolution,
            "hazard": self.hazard, "error_code": self.error_code, "ticket": self.ticket,
            "playbook": self.run.playbook.id if self.run else None,
            "step": {"id": node.id, "kind": node.kind, "text": node.text(self.language)} if node else None,
            "path": self.run.path if self.run else [], "transcript": self.transcript, "latency": self.latency,
            "photos": self.photos, "closed": self.closed, "ended": self.ended, "recording_url": self.recording_url,
        }


class SessionStore:
    def __init__(self, engine: Engine, tenant: Tenant):
        self.engine = engine
        self.tenant = tenant
        self.sessions: dict[str, CallSession] = {}

    def create(self, *, channel: str, customer_id: str | None = None, caller_phone: str | None = None) -> CallSession:
        customer = fetch_customer(self.engine, customer_id) if customer_id else None
        if not customer and caller_phone:
            customer = fetch_customer_by_phone(self.engine, caller_phone)
        rentals = fetch_rentals(self.engine, customer["id"]) if customer else []
        language = (customer or {}).get("language") or self.tenant.default_language
        session = CallSession(id=uuid.uuid4().hex[:12], channel=channel, customer=customer, rentals=rentals,
                              language=language, caller_phone=caller_phone or (customer or {}).get("phone"))
        self.sessions[session.id] = session
        return session

    def get(self, session_id: str) -> CallSession | None:
        return self.sessions.get(session_id)
