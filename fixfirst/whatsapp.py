"""WhatsApp delivery through the Meta Cloud API. Every message is logged to the outbox
(shown on /ops); in dry_run mode nothing leaves the machine."""
import os
import re

import httpx
from loguru import logger
from sqlalchemy import insert
from sqlalchemy.engine import Engine

from .config import Settings, now_utc
from .db import outbox

GRAPH_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v23.0")


def template_param(value: str, limit: int = 1000) -> str:
    """WhatsApp template parameters may not contain newlines/tabs or 4+ consecutive spaces."""
    value = re.sub(r"[\r\n\t]+", " | ", value)
    value = re.sub(r" {2,}", " ", value).strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def mask(phone: str | None) -> str:
    """Phone numbers in logs keep only the last 4 digits."""
    digits = "".join(ch for ch in phone or "" if ch.isdigit())
    return f"…{digits[-4:]}" if digits else "none"


def to_e164_digits(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return "91" + digits if len(digits) == 10 else digits


class WhatsApp:
    def __init__(self, settings: Settings, engine: Engine):
        self.s = settings
        self.engine = engine
        self.live = settings.whatsapp_mode == "live"
        if self.live and not (settings.whatsapp_token and settings.whatsapp_phone_number_id):
            logger.warning("WHATSAPP_MODE=live but token/phone number id missing; falling back to dry_run")
            self.live = False
        self.url = f"https://graph.facebook.com/{GRAPH_VERSION}/{settings.whatsapp_phone_number_id}/messages"

    async def send(self, to: str | None, audience: str, kind: str, body: str,
                   ticket_code: str | None = None, call_id: str | None = None) -> str:
        """A v1 template with the whole message as its one variable (fixfirst_customer_update / _team_ticket)."""
        template = self.s.whatsapp_template_team if audience == "team" else self.s.whatsapp_template_customer
        return await self.send_template(to, audience, kind, template, [body], preview=body,
                                        ticket_code=ticket_code, call_id=call_id)

    async def send_template(self, to: str | None, audience: str, kind: str, template: str, params: list[str], *,
                            preview: str, button_param: str | None = None, ticket_code: str | None = None,
                            call_id: str | None = None) -> str:
        """Send an approved template: `params` fill its body {{1}}, {{2}}…; `button_param` is the dynamic suffix
        of its first URL button (T1's photo link, T10a's code). `preview` is what the outbox and /ops show."""
        status, error = "dry_run", None
        if not to:
            status = "no_recipient"
        elif self.live:
            components = [{"type": "body", "parameters": [{"type": "text", "text": template_param(p)} for p in params]}]
            if button_param is not None:
                components.append({"type": "button", "sub_type": "url", "index": "0",
                                   "parameters": [{"type": "text", "text": button_param}]})
            payload = {
                "messaging_product": "whatsapp",
                "to": to_e164_digits(to),
                "type": "template",
                "template": {"name": template, "language": {"code": self.s.whatsapp_template_lang},
                             "components": components},
            }
            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    resp = await client.post(self.url, json=payload,
                                             headers={"Authorization": f"Bearer {self.s.whatsapp_token}"})
                status = "sent" if resp.status_code < 400 else "failed"
                if status == "failed":
                    error = f"{resp.status_code} {resp.text[:300]}"
            except httpx.HTTPError as e:
                status, error = "failed", str(e)
            if error:
                logger.warning(f"WhatsApp send to {mask(to)} failed: {error}")
        self._log(to, audience, kind, preview, status, error, ticket_code, call_id)
        return status

    def skip(self, to: str | None, audience: str, kind: str, preview: str, *, ticket_code: str | None = None,
             call_id: str | None = None) -> str:
        """No approved Utility template carries this message yet (e.g. self-fix steps before T2): log it, send nothing."""
        logger.info(f"WhatsApp {kind} to {mask(to)} not sent: no approved template for it yet")
        self._log(to, audience, kind, preview, "no_template", None, ticket_code, call_id)
        return "no_template"

    def _log(self, to, audience, kind, preview, status, error, ticket_code, call_id) -> None:
        with self.engine.begin() as c:
            c.execute(insert(outbox).values(to=to, audience=audience, kind=kind, body=preview, status=status,
                                            error=error, ticket_code=ticket_code, call_id=call_id,
                                            created_at=now_utc()))
