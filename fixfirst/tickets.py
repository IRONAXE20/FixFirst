"""Tickets, the technician handoff, WhatsApp messages, follow-ups and call records."""
import secrets
from datetime import timedelta

from loguru import logger
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .config import Settings, Tenant, now_utc
from .db import calls, fetch_customer, followups, row, tickets
from .session import CallSession, appliance_name
from .whatsapp import WhatsApp


class TicketService:
    def __init__(self, engine: Engine, whatsapp: WhatsApp, tenant: Tenant, settings: Settings):
        self.engine = engine
        self.whatsapp = whatsapp
        self.tenant = tenant
        self.settings = settings

    # ---- tickets ---------------------------------------------------------------------
    async def create(self, session: CallSession, *, priority: str, category: str, likely_cause: str,
                     technician_note: str | None = None, reason: str | None = None) -> dict:
        if session.ticket:
            return session.ticket
        now = now_utc()
        summary = self._summary(session, likely_cause, technician_note, reason)
        token = secrets.token_urlsafe(12)
        with self.engine.begin() as c:
            result = c.execute(insert(tickets).values(
                call_id=session.id, customer_id=(session.customer or {}).get("id"),
                rental_id=(session.rental or {}).get("id"), priority=priority, category=category, status="open",
                summary=summary, photo_token=token, photos=list(session.photos), created_at=now, updated_at=now))
            ticket_id = result.inserted_primary_key[0]
            code = f"FF-{1000 + ticket_id}"
            c.execute(update(tickets).where(tickets.c.id == ticket_id).values(code=code))
        ticket = {"id": ticket_id, "code": code, "priority": priority, "category": category, "status": "open",
                  "summary": summary, "photo_token": token, "photo_url": self.photo_url(token),
                  "created_at": now.isoformat()}
        session.ticket = ticket
        if session.photos:
            from . import photos as photo_store
            photo_store.link_ticket(self.engine, session.photos, code)
        session.publish("ticket", ticket=ticket)
        await self._notify(session, ticket)
        self.persist_call(session)
        return ticket

    def _summary(self, session: CallSession, likely_cause: str, technician_note: str | None,
                 reason: str | None) -> dict:
        cust, rental = session.customer or {}, session.rental or {}
        run = session.run
        return {
            "customer": {"name": cust.get("name"), "phone": cust.get("phone") or session.caller_phone,
                         "area": cust.get("area"), "address": cust.get("address")},
            "appliance": session.appliance,
            "appliance_label": rental.get("label") or appliance_name(session.appliance, "en"),
            "brand": rental.get("brand") or (session.error_code or {}).get("brand"),
            "model": rental.get("model"),
            "problem": run.playbook.title.get("en") if run else None,
            "error_code": session.error_code,
            "hazard": session.hazard,
            "steps_tried": [{"node": p["node"], "answer": p["label"]} for p in run.steps_tried()] if run else [],
            "likely_cause": likely_cause,
            "technician_note": technician_note,
            "reason": reason,
            "language": session.language,
            "playbook": run.playbook.id if run else None,
            "playbook_status": run.playbook.status if run else None,
        }

    def photo_url(self, token: str) -> str:
        return f"{self.settings.public_base_url}/u/{token}"

    def call_url(self, session_id: str) -> str:
        return f"{self.settings.public_base_url}/ops#call-{session_id}"

    def team_message(self, ticket: dict, session_id: str) -> str:
        s = ticket["summary"]
        cust = s["customer"]
        tried = "; ".join(t["answer"] for t in s["steps_tried"]) or "none"
        code = s.get("error_code") or {}
        lines = [
            f"FixFirst ticket {ticket['code']} · {ticket['priority'].upper()}"
            + (f" · SAFETY: {s['hazard']}" if s.get("hazard") else ""),
            f"Customer: {cust.get('name') or 'Unknown caller'} · {cust.get('phone') or '-'} · {cust.get('area') or '-'}",
            f"Appliance: {s['appliance_label']}" + (f" ({s['brand']})" if s.get("brand") else ""),
        ]
        if s.get("problem"):
            lines.append(f"Problem: {s['problem']}")
        if code:
            lines.append(f"Error code: {code.get('code')}" + (f" = {code['meaning']}" if code.get("meaning") else " (not interpreted)"))
        lines += [f"Tried on call: {tried}", f"Likely cause: {s['likely_cause']}"]
        if s.get("technician_note"):
            lines.append(f"Technician note: {s['technician_note']}")
        if s.get("reason"):
            lines.append(f"Caller said: {s['reason']}")
        for note in s.get("notes", []):
            lines.append(f"Also: {note}")
        lines += [f"Photos: {ticket['photo_url']}", f"Call log: {self.call_url(session_id)}"]
        return "\n".join(lines)

    def customer_message(self, ticket: dict, session: CallSession) -> str:
        name = appliance_name(session.appliance, "en")
        if ticket["category"] == "safety":
            nums = self.tenant.emergency_numbers
            return (f"{self.tenant.name} FixFirst: we've marked your report URGENT (ticket {ticket['code']}). "
                    f"Our team will call you within {self.tenant.urgent_callback_minutes} minutes. "
                    f"Emergency: {nums.get('national', '112')} · LPG leak: {nums.get('lpg', '1906')}.")
        promise = self.tenant.normal_visit_promise.get("en", "Our service team will call you to fix a visit time.")
        return (f"{self.tenant.name} FixFirst: a technician is booked for your {name} (ticket {ticket['code']}). "
                f"{promise} A photo of the problem or the display helps the technician come prepared: "
                f"{ticket['photo_url']}")

    async def _notify(self, session: CallSession, ticket: dict) -> None:
        team_body = self.team_message(ticket, session.id)
        for number in self.tenant.team_whatsapp or [None]:
            await self.whatsapp.send(number, "team", f"ticket_{ticket['category']}", team_body,
                                     ticket_code=ticket["code"], call_id=session.id)
        await self.whatsapp.send(None if session.test_call else session.caller_phone, "customer",
                                 f"ticket_{ticket['category']}",
                                 self.customer_message(ticket, session), ticket_code=ticket["code"],
                                 call_id=session.id)
        session.publish("whatsapp", audience="team", body=team_body)

    def add_note(self, ticket: dict, note: str) -> None:
        ticket["summary"].setdefault("notes", []).append(note)
        with self.engine.begin() as c:
            c.execute(update(tickets).where(tickets.c.code == ticket["code"])
                      .values(summary=ticket["summary"], updated_at=now_utc()))

    def add_photo(self, token: str, path: str) -> dict | None:
        with self.engine.begin() as c:
            t = row(c.execute(select(tickets).where(tickets.c.photo_token == token)).first())
            if not t:
                return None
            photos = list(t["photos"] or []) + [path]
            c.execute(update(tickets).where(tickets.c.id == t["id"]).values(photos=photos, updated_at=now_utc()))
        t["photos"] = photos
        return t

    def attach_session_photo(self, session: CallSession, path: str) -> None:
        session.photos.append(path)
        if session.ticket:
            self.add_photo(session.ticket["photo_token"], path)
        session.publish("photo", path=path)

    # ---- follow-ups ------------------------------------------------------------------
    def schedule_followup(self, session: CallSession, minutes: int, kind: str, body: str) -> None:
        if session.test_call:   # a `bolna-call --as` test: never message the real customer later
            session.publish("followup", kind=kind, minutes=minutes, skipped="test call")
            return
        with self.engine.begin() as c:
            c.execute(insert(followups).values(
                call_id=session.id, customer_id=(session.customer or {}).get("id"), kind=kind,
                due_at=now_utc() + timedelta(minutes=minutes), status="pending", body=body, created_at=now_utc()))
        session.publish("followup", kind=kind, minutes=minutes)

    async def send_due_followups(self) -> int:
        with self.engine.begin() as c:
            due = [row(r) for r in c.execute(select(followups).where(
                followups.c.status == "pending", followups.c.due_at <= now_utc()))]
            for f in due:
                c.execute(update(followups).where(followups.c.id == f["id"]).values(status="sent"))
        for f in due:
            phone = None
            with self.engine.connect() as c:
                call = row(c.execute(select(calls).where(calls.c.id == f["call_id"])).first())
            if call and call.get("customer_id"):
                phone = (fetch_customer(self.engine, call["customer_id"]) or {}).get("phone")
            await self.whatsapp.send(phone, "customer", f"followup_{f['kind']}", f["body"], call_id=f["call_id"])
        return len(due)

    # ---- call records ----------------------------------------------------------------
    def persist_call(self, session: CallSession) -> None:
        values = dict(
            customer_id=(session.customer or {}).get("id"), rental_id=(session.rental or {}).get("id"),
            channel=session.channel, language=session.language, started_at=session.started_at,
            ended_at=now_utc() if session.ended else None, outcome=session.outcome, resolution=session.resolution,
            playbook_id=session.run.playbook.id if session.run else None, hazard=session.hazard,
            error_code=session.error_code, path=session.run.path if session.run else [],
            transcript=session.transcript, latency=session.latency,
            ticket_code=(session.ticket or {}).get("code"), consent_notice="given")
        try:
            with self.engine.begin() as c:
                exists = c.execute(select(calls.c.id).where(calls.c.id == session.id)).first()
                if exists:
                    c.execute(update(calls).where(calls.c.id == session.id).values(**values))
                else:
                    c.execute(insert(calls).values(id=session.id, **values))
        except Exception as e:  # never let bookkeeping break a live call
            logger.exception(f"Could not persist call {session.id}: {e}")
