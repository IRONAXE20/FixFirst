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
        status, error = "dry_run", None
        if not to:
            status = "no_recipient"
        elif self.live:
            template = self.s.whatsapp_template_team if audience == "team" else self.s.whatsapp_template_customer
            payload = {
                "messaging_product": "whatsapp",
                "to": to_e164_digits(to),
                "type": "template",
                "template": {
                    "name": template,
                    "language": {"code": self.s.whatsapp_template_lang},
                    "components": [{"type": "body", "parameters": [{"type": "text", "text": template_param(body)}]}],
                },
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
        with self.engine.begin() as c:
            c.execute(insert(outbox).values(to=to, audience=audience, kind=kind, body=body, status=status,
                                            error=error, ticket_code=ticket_code, call_id=call_id,
                                            created_at=now_utc()))
        return status
