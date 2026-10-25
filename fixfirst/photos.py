"""Customer photos, stored in the database (Supabase in production) instead of the server's disk, which
Render wipes on every deploy. A photo is deleted PHOTO_RETENTION_DAYS after its ticket is closed; a photo
never attached to a ticket is deleted after ORPHAN_DAYS."""
import uuid
from datetime import timedelta

from loguru import logger
from sqlalchemy import delete, select, update
from sqlalchemy.engine import Engine

from .config import now_utc
from .db import photos, tickets

PREFIX = "p/"          # photo paths stored on tickets look like "p/<id>.jpg" and are served at /media/<path>
ORPHAN_DAYS = 30
TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".heic": "image/heic"}


def save(engine: Engine, data: bytes, *, ext: str, content_type: str, call_id: str | None = None,
         ticket_code: str | None = None) -> str:
    photo_id = uuid.uuid4().hex[:16]
    with engine.begin() as c:
        c.execute(photos.insert().values(id=photo_id, call_id=call_id, ticket_code=ticket_code,
                                         content_type=content_type or TYPES.get(ext, "image/jpeg"),
                                         size=len(data), data=data, created_at=now_utc()))
    return f"{PREFIX}{photo_id}{ext}"


def load(engine: Engine, path: str) -> tuple[str, bytes] | None:
    if not path.startswith(PREFIX):
        return None
    photo_id = path[len(PREFIX):].split(".")[0]
    with engine.connect() as c:
        r = c.execute(select(photos.c.content_type, photos.c.data).where(photos.c.id == photo_id)).first()
    return (r.content_type, bytes(r.data)) if r else None


def link_ticket(engine: Engine, paths: list[str], ticket_code: str) -> None:
    """Photos sent during the call before the ticket existed belong to that ticket from now on."""
    ids = [p[len(PREFIX):].split(".")[0] for p in paths if p.startswith(PREFIX)]
    if ids:
        with engine.begin() as c:
            c.execute(update(photos).where(photos.c.id.in_(ids)).values(ticket_code=ticket_code))


def purge(engine: Engine, retention_days: int) -> int:
    """Delete photos of tickets closed more than `retention_days` ago, and old photos with no ticket."""
    now = now_utc()
    with engine.begin() as c:
        closed = c.execute(select(tickets.c.id, tickets.c.code, tickets.c.photos)
                           .where(tickets.c.status == "closed")
                           .where(tickets.c.updated_at < now - timedelta(days=retention_days))).all()
        codes = [t.code for t in closed if t.photos]
        removed = 0
        if codes:
            removed += c.execute(delete(photos).where(photos.c.ticket_code.in_(codes))).rowcount or 0
            for t in closed:
                if t.photos:   # keep the ticket, drop its now-dead photo links
                    c.execute(update(tickets).where(tickets.c.id == t.id).values(photos=[]))
        removed += c.execute(delete(photos).where(photos.c.ticket_code.is_(None))
                             .where(photos.c.created_at < now - timedelta(days=ORPHAN_DAYS))).rowcount or 0
    if removed:
        logger.info(f"Deleted {removed} photo(s) past retention")
    return removed
