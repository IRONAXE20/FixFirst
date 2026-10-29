"""Tickets, the technician handoff, WhatsApp messages, follow-ups and call records.

Tickets follow RentBasket's lifecycle (Customer Service Ecosystem, 29 Oct 2026): one shared FF-xxxx series,
NEW -> SELF_FIX_IN_PROGRESS -> RESOLVED_REMOTE, or TICKET_RAISED / URGENT -> SCHEDULED -> IN_PROGRESS ->
FIXED / REPLACED -> (AUDIT_REVIEW) -> CLOSED, and REOPENED when the same problem comes back within 7 days.
"""
import asyncio
import json
import re
import secrets
from datetime import timedelta

from loguru import logger
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Engine

from .config import Settings, Tenant, now_utc
from .db import calls, customers, fetch_customer, followups, row, tickets
from .session import CallSession, appliance_name
from .whatsapp import WhatsApp

STATES = ("NEW", "SELF_FIX_IN_PROGRESS", "RESOLVED_REMOTE", "TICKET_RAISED", "URGENT", "SCHEDULED", "IN_PROGRESS",
          "FIXED", "REPLACED", "AUDIT_REVIEW", "CLOSED", "REOPENED")
BOOKED = {"TICKET_RAISED", "URGENT", "REOPENED"}        # the team or a technician is coming
SELF_FIX = {"SELF_FIX_IN_PROGRESS", "RESOLVED_REMOTE"}  # the customer is fixing it, or fixed it
DONE = {"RESOLVED_REMOTE", "FIXED", "REPLACED", "CLOSED"}  # reopened if the same problem returns within 7 days
OPEN = set(STATES) - {"CLOSED", "RESOLVED_REMOTE"}      # what the customer link and "open tickets" show
REOPEN_DAYS = 7
CHECK_HOURS = 24                                        # T9 check-in after RESOLVED_REMOTE, FIXED or REPLACED

# T2 selffix_steps_<issue>: the KB's self-fix steps, fixed in each template body (Meta allows no line breaks
# inside a variable). Here for the v1 fallback text and the outbox preview.
SELF_FIX_STEPS = {
    "AC-04": ("your AC (weak airflow)", ["Switch off the AC from the remote and the wall switch.",
                                         "Open the front panel and slide out the filters.",
                                         "Wash them with plain water and let them dry fully.",
                                         "Put the filters back, close the panel, and switch the AC on."]),
    "FR-04": ("your fridge (water from the front)", ["Switch off the fridge at the wall.",
                                                     "Move the food out and keep the fridge door open.",
                                                     "Switch on a fan facing the fridge.",
                                                     "Leave it like this for 12 hours.",
                                                     "Wipe it dry, close the door, and switch the fridge on."]),
    "WM-01": ("your washing machine (water not coming in)", [
        "Switch off the machine and close the water tap.", "Unscrew the inlet pipe from the back of the machine.",
        "Pull out the small mesh filter inside the inlet and clean it under running water.",
        "Fit the filter and pipe back, open the tap, and start the machine."]),
    "MW-04": ("your microwave (bad smell)", ["Switch off and unplug the microwave.",
                                             "Wipe the inside walls, door and turntable with a damp cloth.",
                                             "Optional: heat a bowl of water with lemon for 3 minutes, then wipe again."]),
}
_ISSUE = re.compile(r"^([A-Z]{2}-\d\d)\b")


def issue_of(likely_cause: str | None) -> str | None:
    m = _ISSUE.match(likely_cause or "")
    return m.group(1) if m else None


class TicketService:
    # The server sets this: the live safety turn took 3.8 s because the reply waited on Meta's API.
    notify_in_background = False
    _pending: set = set()

    def __init__(self, engine: Engine, whatsapp: WhatsApp, tenant: Tenant, settings: Settings):
        self.engine = engine
        self.whatsapp = whatsapp
        self.tenant = tenant
        self.settings = settings

    @property
    def ecosystem(self) -> bool:
        return self.settings.whatsapp_templates == "ecosystem"

    @property
    def can_send_steps(self) -> bool:
        """Self-fix steps need T2 (selffix_steps_<issue>); the v1 set has no Utility template that can carry them."""
        return self.ecosystem or not self.whatsapp.live

    # ---- tickets ---------------------------------------------------------------------
    async def create(self, session: CallSession, *, priority: str, category: str, likely_cause: str,
                     technician_note: str | None = None, reason: str | None = None, status: str | None = None,
                     notify: bool = True) -> dict:
        """Raise a ticket. Booked tickets (TICKET_RAISED / URGENT / REOPENED) are `session.ticket`, one per call;
        a self-fix (SELF_FIX_IN_PROGRESS / RESOLVED_REMOTE) is `session.selffix_ticket` and notifies nobody."""
        status = status or ("URGENT" if priority == "urgent" else "TICKET_RAISED")
        self_fix = status in SELF_FIX
        if not self_fix and session.ticket:
            return session.ticket
        summary = self._summary(session, likely_cause, technician_note, reason)
        if not self_fix:
            reopened = self._reopen(session, summary, priority)
            if reopened:
                return await self._booked(session, reopened, notify)
        now = now_utc()
        token = secrets.token_urlsafe(12)
        with self.engine.begin() as c:
            result = c.execute(insert(tickets).values(
                call_id=session.id, customer_id=(session.customer or {}).get("id"),
                rental_id=(session.rental or {}).get("id"), priority=priority, category=category, status=status,
                summary=summary, photo_token=token, photos=list(session.photos), created_at=now, updated_at=now,
                issue_id=issue_of(likely_cause), origin_channel=self._channel(session), channels_used="voice_only",
                audit_required=bool(session.audit_required)))
            ticket_id = result.inserted_primary_key[0]
            code = f"FF-{1000 + ticket_id}"
            c.execute(update(tickets).where(tickets.c.id == ticket_id).values(code=code))
        ticket = {"id": ticket_id, "code": code, "priority": priority, "category": category, "status": status,
                  "summary": summary, "photo_token": token, "photo_url": self.link_for(session, token),
                  "issue_id": issue_of(likely_cause), "origin_channel": self._channel(session),
                  "channels_used": "voice_only", "audit_required": bool(session.audit_required),
                  "created_at": now.isoformat()}
        if session.photos:
            from . import photos as photo_store
            photo_store.link_ticket(self.engine, session.photos, code)
        if self_fix:
            session.selffix_ticket = ticket
            session.publish("ticket", ticket=ticket)
            self.persist_call(session)
            return ticket
        return await self._booked(session, ticket, notify)

    async def _booked(self, session: CallSession, ticket: dict, notify: bool) -> dict:
        session.ticket = ticket
        session.publish("ticket", ticket=ticket)
        if not notify:
            pass
        elif self.notify_in_background:   # the caller hears the reply now; WhatsApp goes out meanwhile
            task = asyncio.create_task(self._notify_safely(session, ticket))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)
        else:
            await self._notify(session, ticket)
        self.persist_call(session)
        return ticket

    def _reopen(self, session: CallSession, summary: dict, priority: str) -> dict | None:
        """The same problem on the same appliance within 7 days of a fix: reopen that ticket, don't start a new one."""
        cid = (session.customer or {}).get("id")
        if not cid or not session.appliance:
            return None
        since = now_utc() - timedelta(days=REOPEN_DAYS)
        with self.engine.begin() as c:
            for t in (row(r) for r in c.execute(select(tickets).where(
                    tickets.c.customer_id == cid, tickets.c.status.in_(DONE), tickets.c.updated_at >= since)
                    .order_by(tickets.c.updated_at.desc()))):
                if (t["summary"] or {}).get("appliance") != session.appliance:
                    continue
                s = dict(t["summary"] or {})
                s.setdefault("reopened", []).append({"call_id": session.id, "at": now_utc().isoformat(),
                                                     "likely_cause": summary["likely_cause"],
                                                     "steps_tried": summary["steps_tried"], "reason": summary["reason"]})
                s["likely_cause"], s["hazard"] = summary["likely_cause"], summary["hazard"] or s.get("hazard")
                new_priority = "urgent" if "urgent" in (priority, t["priority"]) else "normal"
                c.execute(update(tickets).where(tickets.c.id == t["id"]).values(
                    status="REOPENED", priority=new_priority, summary=s, updated_at=now_utc(),
                    audit_required=bool(t.get("audit_required") or session.audit_required)))
                session.publish("reopened", code=t["code"])
                return {"id": t["id"], "code": t["code"], "priority": new_priority, "category": t["category"],
                        "status": "REOPENED", "summary": s, "photo_token": t["photo_token"],
                        "photo_url": self.link_for(session, t["photo_token"]), "issue_id": t.get("issue_id"),
                        "origin_channel": t.get("origin_channel"), "channels_used": t.get("channels_used"),
                        "audit_required": bool(t.get("audit_required")), "created_at": t["created_at"].isoformat(),
                        "reopened": True}
        return None

    @staticmethod
    def _channel(session: CallSession) -> str:
        return "whatsapp" if session.channel == "whatsapp" else "voice"

    def set_status(self, code: str, status: str) -> dict | None:
        """The team moves a ticket along (ops page). FIXED / REPLACED schedule the 24 h check-in (T9)."""
        if status not in STATES:
            raise ValueError(f"status must be one of {', '.join(STATES)}")
        with self.engine.begin() as c:
            t = row(c.execute(select(tickets).where(tickets.c.code == code)).first())
            if not t:
                return None
            c.execute(update(tickets).where(tickets.c.code == code).values(status=status, updated_at=now_utc()))
        t["status"] = status
        if status in ("FIXED", "REPLACED"):
            self._schedule_check(t["call_id"], t["customer_id"], t, CHECK_HOURS * 60)
        return t

    def flag_audit(self, session: CallSession) -> None:
        """Possible misuse: the Rentbasket Auditing team reviews it. The bot never decides or mentions charges."""
        session.audit_required = True
        for t in (session.ticket, session.selffix_ticket):
            if t:
                t["audit_required"] = True
                with self.engine.begin() as c:
                    c.execute(update(tickets).where(tickets.c.code == t["code"]).values(audit_required=True))
        session.publish("audit_required")

    def _summary(self, session: CallSession, likely_cause: str, technician_note: str | None,
                 reason: str | None) -> dict:
        cust, rental = session.customer or {}, session.rental or {}
        run = session.run
        kind = "furniture" if session.appliance == "furniture" else "appliance"
        return {
            "customer": {"name": cust.get("name"), "phone": cust.get("phone") or session.caller_phone,
                         "area": cust.get("area"), "address": cust.get("address")},
            "appliance": session.appliance,
            "appliance_label": rental.get("label") or appliance_name(session.appliance, "en"),
            "product_id": (rental.get("details") or {}).get("product_id"),
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
            "sla_hours": self.tenant.sla_hours.get(kind),
            "caller_linked_by_otp": session.linked_by_otp or None,
            "caller_not_account_holder": session.not_account_holder or None,
        }

    # ---- the customer's photo-and-notes link -------------------------------------------
    def customer_token(self, customer_id: str | None) -> str | None:
        """One persistent link per customer (/u/<token>), created on first use."""
        if not customer_id:
            return None
        with self.engine.begin() as c:
            tok = c.execute(select(customers.c.link_token).where(customers.c.id == customer_id)).scalar()
            if tok:
                return tok
            exists = c.execute(select(customers.c.id).where(customers.c.id == customer_id)).first()
            if not exists:
                return None
            tok = secrets.token_urlsafe(16)
            c.execute(update(customers).where(customers.c.id == customer_id).values(link_token=tok))
            return tok

    def link_for(self, session: CallSession, ticket_token: str) -> str:
        """The customer's persistent link when we know them, else the ticket's own link."""
        return self.photo_url(self.customer_token((session.customer or {}).get("id")) or ticket_token)

    def photo_url(self, token: str) -> str:
        return f"{self.settings.public_base_url}/u/{token}"

    def call_url(self, session_id: str) -> str:
        return f"{self.settings.public_base_url}/ops#call-{session_id}"

    # ---- messages ----------------------------------------------------------------------
    def team_message(self, ticket: dict, session_id: str) -> str:
        s = ticket["summary"]
        cust = s["customer"]
        tried = "; ".join(t["answer"] for t in s["steps_tried"]) or "none"
        code = s.get("error_code") or {}
        head = f"FixFirst ticket {ticket['code']} · {ticket['status']}" + (" (reopened)" if ticket.get("reopened") else "")
        lines = [
            head + (f" · SAFETY: {s['hazard']}" if s.get("hazard") else "")
            + (" · AUDIT REQUIRED" if ticket.get("audit_required") else ""),
            f"Customer: {cust.get('name') or 'Unknown caller'} · {cust.get('phone') or '-'} · {cust.get('area') or '-'}",
            f"Appliance: {s['appliance_label']}" + (f" ({s['brand']})" if s.get("brand") else ""),
        ]
        if ticket["status"] == "URGENT":
            lines.append(f"SLA: call back within {self.tenant.urgent_callback_minutes} min and visit today")
        elif s.get("sla_hours"):
            lines.append(f"SLA: agree a visit time within {s['sla_hours']} h")
        if s.get("problem"):
            lines.append(f"Problem: {s['problem']}")
        if code:
            lines.append(f"Error code: {code.get('code')}" + (f" = {code['meaning']}" if code.get("meaning") else " (not interpreted)"))
        lines += [f"Tried on call: {tried}", f"Likely cause: {s['likely_cause']}"]
        if s.get("technician_note"):
            lines.append(f"Technician note: {s['technician_note']}")
        if s.get("reason"):
            lines.append(f"Caller said: {s['reason']}")
        if s.get("caller_linked_by_otp"):
            lines.append("Caller's number was linked to this account by OTP on the call")
        for note in s.get("notes", []):
            lines.append(f"Also: {note}")
        lines += [f"Photos: {ticket['photo_url']}", f"Call log: {self.call_url(session_id)}"]
        return "\n".join(lines)

    def customer_message(self, ticket: dict, session: CallSession) -> str:
        """The v1 text (fixfirst_customer_update's one variable)."""
        name = appliance_name(session.appliance, "en")
        if ticket["status"] == "URGENT":
            nums = self.tenant.emergency_numbers
            return (f"{self.tenant.name} FixFirst: for your safety please keep the {name} switched off at the wall and "
                    f"don't touch it. We've raised an URGENT ticket ({ticket['code']}); our team will call you shortly "
                    f"from {self.tenant.care_number()}. Emergency: {nums.get('national', '112')}.")
        promise = self.tenant.normal_visit_promise.get("en", "Our technician will contact you soon.")
        return (f"{self.tenant.name} FixFirst: a technician is booked for your {name} (ticket {ticket['code']}). "
                f"{promise} A photo of the problem or the display helps the technician come prepared: "
                f"{ticket['photo_url']}")

    def _names(self, session: CallSession) -> tuple[str, str]:
        name = ((session.customer or {}).get("name") or "there").split(" ")[0]
        product = (session.rental or {}).get("label") or appliance_name(session.appliance, "en") or "appliance"
        return name, product

    async def _notify_safely(self, session: CallSession, ticket: dict) -> None:
        try:
            await self._notify(session, ticket)
        except Exception as e:   # a WhatsApp failure must never surface in the call
            logger.exception(f"WhatsApp notification for {ticket['code']} failed: {e}")

    async def _notify(self, session: CallSession, ticket: dict) -> None:
        team_body = self.team_message(ticket, session.id)
        for number in self.tenant.team_whatsapp or [None]:
            await self.whatsapp.send(number, "team", f"ticket_{ticket['category']}", team_body,
                                     ticket_code=ticket["code"], call_id=session.id)
        session.publish("whatsapp", audience="team", body=team_body)
        to = None if session.test_call else session.caller_phone
        if not self.ecosystem:   # fixfirst_service_request (Utility)
            name, product = self._names(session)
            if ticket["status"] == "URGENT":
                line = (f"For your safety please keep it switched off at the wall and don't touch it. Our team will "
                        f"call you shortly from {self.tenant.care_number()}.")
            else:
                line = self.tenant.normal_visit_promise.get("en", "Our technician will contact you soon.")
            params = [name, ticket["code"], product, line]
            preview = (f"Hi {name}, your {self.tenant.name} service request {ticket['code']} for your {product} is "
                       f"registered. {line} To help our technician come prepared, please add a photo of the appliance "
                       f"using the button below. [button: {ticket['photo_url']}]")
            status = await self.whatsapp.send_template(to, "customer", f"ticket_{ticket['category']}",
                                                       self.settings.whatsapp_template_customer, params, preview=preview,
                                                       button_param=ticket["photo_url"].rsplit("/", 1)[-1],
                                                       ticket_code=ticket["code"], call_id=session.id)
        else:
            name, product = self._names(session)
            care = self.tenant.care_number((session.customer or {}).get("area"))
            if ticket["status"] == "URGENT":   # T4 urgent_safety
                params = [name, product, ticket["code"], care]
                preview = (f"Hi {name}, for your safety please keep the {product} switched off at the wall and do not "
                           f"touch it. We have raised an URGENT ticket ({ticket['code']}). Our team will call you "
                           f"shortly from {care}.")
                status = await self.whatsapp.send_template(to, "customer", "T4_urgent_safety", "urgent_safety", params,
                                                           preview=preview, ticket_code=ticket["code"], call_id=session.id)
            else:                              # T1 technician_booked, with the "Upload photo or details" button
                link = ticket["photo_url"]
                params = [name, product, ticket["code"], care, link]
                preview = (f"Hi {name}, Welcome to Rentbasket! A technician is booked for your {product} (ticket "
                           f"{ticket['code']}). Our service team will call you to fix a visit time from this number : "
                           f"\"{care}\". A photo of the problem or the display helps the technician come prepared: "
                           f"{link} We're here to ensure a smooth and worry-free experience. Let us know if you need help!")
                status = await self.whatsapp.send_template(to, "customer", "T1_technician_booked", "technician_booked",
                                                           params, preview=preview, button_param=link.rsplit("/", 1)[-1],
                                                           ticket_code=ticket["code"], call_id=session.id)
        self._whatsapp_used(ticket, status)

    def _whatsapp_used(self, ticket: dict, status: str) -> None:
        """An outbound WhatsApp counts as WhatsApp used even with no reply (call + template = both)."""
        if status in ("sent", "dry_run") and ticket.get("channels_used") != "both":
            ticket["channels_used"] = "both"
            with self.engine.begin() as c:
                c.execute(update(tickets).where(tickets.c.code == ticket["code"]).values(channels_used="both"))

    async def send_selffix_steps(self, session: CallSession, issue: str, ticket: dict) -> None:
        """T2 selffix_steps_<issue>: the KB steps on WhatsApp ("Did it work?" buttons on the template)."""
        what, steps = SELF_FIX_STEPS[issue]
        name, _ = self._names(session)
        preview = (f"Hi {name}, here are the steps we discussed for {what}: "
                   + " ".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) + " Did it work? Tap a button below.")
        to = None if session.test_call else session.caller_phone
        if self.ecosystem:
            status = await self.whatsapp.send_template(to, "customer", f"T2_selffix_{issue}",
                                                       f"selffix_steps_{issue.lower().replace('-', '')}", [name],
                                                       preview=preview, ticket_code=ticket["code"], call_id=session.id)
        else:   # no approved Utility template can carry steps until T2 is approved
            status = self.whatsapp.skip(to, "customer", f"selffix_{issue}", preview, ticket_code=ticket["code"],
                                        call_id=session.id)
        self._whatsapp_used(ticket, status)

    async def send_otp(self, session: CallSession, phone: str, otp: str) -> bool:
        """T10a otp_verify (an Authentication template) to the account holder's registered number. False when it
        can't be sent: then the caller goes to a person."""
        if self.whatsapp.live and not self.ecosystem:
            return False   # the v1 set has no authentication template
        preview = f"{otp} is your Rentbasket verification code." if not self.whatsapp.live else "otp_verify (code hidden)"
        status = await self.whatsapp.send_template(phone, "customer", "T10a_otp_verify", "otp_verify", [otp],
                                                   preview=preview, button_param=otp, call_id=session.id)
        return status in ("sent", "dry_run")

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
        ticket = session.ticket or session.selffix_ticket
        if self.ecosystem and ticket and kind in ("still_working", "check_back"):
            name, product = self._names(session)   # T9 followup_check, with "Yes, all good" / "No, still a problem"
            body = json.dumps({"template": "followup_check", "params": [name, product, ticket["code"]],
                               "preview": f"Hi {name}, just checking: is your {product} working fine after ticket "
                                          f"{ticket['code']}?"})
        with self.engine.begin() as c:
            c.execute(insert(followups).values(
                call_id=session.id, customer_id=(session.customer or {}).get("id"), kind=kind,
                due_at=now_utc() + timedelta(minutes=minutes), status="pending", body=body, created_at=now_utc()))
        session.publish("followup", kind=kind, minutes=minutes)

    def _schedule_check(self, call_id: str | None, customer_id: str | None, ticket: dict, minutes: int) -> None:
        """T9 after the team marks a ticket FIXED or REPLACED."""
        s = ticket.get("summary") or {}
        name = ((s.get("customer") or {}).get("name") or "there").split(" ")[0]
        product = s.get("appliance_label") or "appliance"
        preview = f"Hi {name}, just checking: is your {product} working fine after ticket {ticket['code']}?"
        body = (json.dumps({"template": "followup_check", "params": [name, product, ticket["code"]], "preview": preview})
                if self.ecosystem else f"{self.tenant.name} FixFirst: {preview} Reply YES, or NO and we'll help.")
        with self.engine.begin() as c:
            c.execute(insert(followups).values(call_id=call_id, customer_id=customer_id, kind="check_24h",
                                               due_at=now_utc() + timedelta(minutes=minutes), status="pending",
                                               body=body, created_at=now_utc()))

    async def send_due_followups(self) -> int:
        with self.engine.begin() as c:
            due = [row(r) for r in c.execute(select(followups).where(
                followups.c.status == "pending", followups.c.due_at <= now_utc()))]
            for f in due:
                c.execute(update(followups).where(followups.c.id == f["id"]).values(status="sent"))
        for f in due:
            phone = None
            customer_id = f.get("customer_id")
            if not customer_id:
                with self.engine.connect() as c:
                    call = row(c.execute(select(calls).where(calls.c.id == f["call_id"])).first())
                customer_id = (call or {}).get("customer_id")
            if customer_id:
                phone = (fetch_customer(self.engine, customer_id) or {}).get("phone")
            try:
                tpl = json.loads(f["body"]) if f["body"].startswith("{") else None
            except ValueError:
                tpl = None
            if tpl:
                await self.whatsapp.send_template(phone, "customer", f"followup_{f['kind']}", tpl["template"],
                                                  tpl["params"], preview=tpl["preview"], call_id=f["call_id"])
            else:   # check-ins need T9 (followup_check); the Marketing template isn't used
                self.whatsapp.skip(phone, "customer", f"followup_{f['kind']}", f["body"], call_id=f["call_id"])
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
            ticket_code=(session.ticket or session.selffix_ticket or {}).get("code"), consent_notice="given")
        try:
            with self.engine.begin() as c:
                exists = c.execute(select(calls.c.id).where(calls.c.id == session.id)).first()
                if exists:
                    c.execute(update(calls).where(calls.c.id == session.id).values(**values))
                else:
                    c.execute(insert(calls).values(id=session.id, **values))
        except Exception as e:  # never let bookkeeping break a live call
            logger.exception(f"Could not persist call {session.id}: {e}")
