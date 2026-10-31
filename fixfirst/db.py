"""Storage: SQLite locally, Supabase Postgres in production (same schema via SQLAlchemy Core)."""
from functools import lru_cache
from pathlib import Path

from sqlalchemy import (JSON, Boolean, Column, DateTime, ForeignKey, Integer, LargeBinary, MetaData, String, Table,
                        Text, create_engine, inspect, select, text)
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateSchema

from .config import get_settings, now_utc

PG_SCHEMA = "fixfirst"
metadata = MetaData()

customers = Table(
    "customers", metadata,
    Column("id", String(32), primary_key=True),
    Column("name", String(120), nullable=False),
    Column("phone", String(20), nullable=False, index=True),
    Column("language", String(4), nullable=False, default="hi"),
    Column("area", String(120)),
    Column("address", String(300)),
    Column("created_at", DateTime, default=now_utc),
    # The customer's one persistent photo-and-notes link (/u/<token>); replaced when the mobile number changes
    # (a new number is a new customer id) and cleared when no rental is left.
    Column("link_token", String(40), unique=True),
)

rentals = Table(
    "rentals", metadata,
    Column("id", String(32), primary_key=True),
    Column("customer_id", String(32), ForeignKey("customers.id"), nullable=False, index=True),
    Column("appliance", String(32), nullable=False),   # ac | washer | fridge | geyser | tv | ...
    Column("label", String(160), nullable=False),      # how the customer would describe it
    Column("brand", String(60)),
    Column("model", String(80)),
    Column("details", JSON, default=dict),
)

calls = Table(
    "calls", metadata,
    Column("id", String(32), primary_key=True),
    Column("customer_id", String(32), index=True),
    Column("rental_id", String(32)),
    Column("channel", String(16), nullable=False),     # web | phone | text
    Column("language", String(4)),
    Column("started_at", DateTime, nullable=False),
    Column("ended_at", DateTime),
    Column("outcome", String(24), nullable=False, default="in_progress"),
    Column("resolution", String(40)),
    Column("playbook_id", String(60)),
    Column("hazard", String(24)),
    Column("error_code", JSON),
    Column("path", JSON, default=list),
    Column("transcript", JSON, default=list),
    Column("latency", JSON, default=list),
    Column("ticket_code", String(16)),
    Column("consent_notice", String(8)),
    Column("guard_flags", JSON, default=list),         # guardrail hits (injection, staff claim, abuse...)
    Column("transfer_result", String(40)),             # handoff to customer care: connected | failed: <reason>
)

tickets = Table(
    "tickets", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(16), unique=True),
    Column("call_id", String(32), index=True),
    Column("customer_id", String(32), index=True),
    Column("rental_id", String(32)),
    Column("priority", String(8), nullable=False),     # urgent | normal
    Column("category", String(24), nullable=False),    # safety | technician | system
    Column("status", String(24), nullable=False, default="TICKET_RAISED"),   # tickets.STATES
    Column("summary", JSON, nullable=False),
    Column("photo_token", String(40), unique=True),
    Column("photos", JSON, default=list),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    # RentBasket ecosystem (29 Oct 2026): the KB issue, the channel that raised the ticket (never changes),
    # the channels it has used since (an outbound WhatsApp counts even without a reply), possible misuse.
    Column("issue_id", String(16)),
    Column("origin_channel", String(12)),                 # voice | whatsapp
    Column("channels_used", String(16)),                  # voice_only | whatsapp_only | both
    Column("audit_required", Boolean, default=False),
    # The customer came back about the same unit (open ticket chased, or a fixed one reopened): high alert.
    Column("high_alert", Boolean, default=False),
)

# A number that isn't on any RentBasket deal, linked to the account holder after an OTP to their number.
linked_numbers = Table(
    "linked_numbers", metadata,
    Column("phone", String(20), primary_key=True),        # last 10 digits
    Column("customer_id", String(32), nullable=False, index=True),
    Column("holder_phone", String(20)),
    Column("linked_at", DateTime, nullable=False),
)

outbox = Table(
    "outbox", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("to", String(20)),
    Column("audience", String(12), nullable=False),    # customer | team
    Column("kind", String(24), nullable=False),
    Column("body", Text, nullable=False),
    Column("status", String(12), nullable=False),      # dry_run | sent | failed | no_recipient
    Column("error", Text),
    Column("ticket_code", String(16)),
    Column("call_id", String(32)),
    Column("created_at", DateTime, nullable=False),
)

photos = Table(
    "photos", metadata,
    Column("id", String(32), primary_key=True),
    Column("call_id", String(32), index=True),
    Column("ticket_code", String(16), index=True),
    Column("content_type", String(40), nullable=False),
    Column("size", Integer, nullable=False),
    Column("data", LargeBinary, nullable=False),
    Column("created_at", DateTime, nullable=False),
)

# Playbooks live here (the YAML files in knowledge/playbooks seed an empty table and are the export format).
playbook_docs = Table(
    "playbooks", metadata,
    Column("id", String(60), primary_key=True),
    Column("appliance", String(32), nullable=False),
    Column("status", String(24), nullable=False),
    Column("version", Integer, nullable=False),
    Column("doc", JSON, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    Column("updated_by", String(80)),
)

playbook_history = Table(
    "playbook_history", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("playbook_id", String(60), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("doc", JSON, nullable=False),
    Column("saved_at", DateTime, nullable=False),
    Column("saved_by", String(80)),
)

followups = Table(
    "followups", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("call_id", String(32), index=True),
    Column("customer_id", String(32)),
    Column("kind", String(24), nullable=False),        # still_working | check_back | disconnected
    Column("due_at", DateTime, nullable=False, index=True),
    Column("status", String(12), nullable=False, default="pending"),
    Column("body", Text, nullable=False),
    Column("created_at", DateTime, nullable=False),
)


@lru_cache
def get_engine(url: str | None = None) -> Engine:
    url = url or get_settings().database_url
    if url.startswith("sqlite:///"):
        Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, future=True)
    else:
        # Postgres (Supabase): keep FixFirst's tables in their own schema. Supabase's REST API exposes
        # every `public` table to the project's anon key unless row-level security is set up, and these
        # hold customer names and addresses.
        engine = create_engine(url, future=True, pool_pre_ping=True)
        with engine.begin() as c:
            c.execute(CreateSchema(PG_SCHEMA, if_not_exists=True))
        engine = engine.execution_options(schema_translate_map={None: PG_SCHEMA})
    metadata.create_all(engine)
    _migrate(engine, url)
    return engine


# Columns added after the first deploy: create_all() makes missing tables but never alters existing ones.
_ADDED = {
    "customers": [("link_token", "VARCHAR(40)")],
    "calls": [("guard_flags", "JSON"), ("transfer_result", "VARCHAR(40)")],
    "tickets": [("issue_id", "VARCHAR(16)"), ("origin_channel", "VARCHAR(12)"), ("channels_used", "VARCHAR(16)"),
                ("audit_required", "BOOLEAN DEFAULT FALSE"), ("high_alert", "BOOLEAN DEFAULT FALSE")],
}
# Ticket statuses before the RentBasket lifecycle.
_OLD_STATUS = {"open": "TICKET_RAISED", "assigned": "SCHEDULED", "closed": "CLOSED"}


def _migrate(engine: Engine, url: str) -> None:
    pg = not url.startswith("sqlite")
    prefix = f"{PG_SCHEMA}." if pg else ""
    insp = inspect(engine.execution_options(schema_translate_map=None) if pg else engine)
    with engine.begin() as c:
        for table, cols in _ADDED.items():
            have = {col["name"] for col in insp.get_columns(table, schema=PG_SCHEMA if pg else None)}
            for name, ddl in cols:
                if name not in have:
                    c.execute(text(f"ALTER TABLE {prefix}{table} ADD COLUMN {name} {ddl}"))
        if pg:   # "SELF_FIX_IN_PROGRESS" doesn't fit the old VARCHAR(16)
            c.execute(text(f"ALTER TABLE {prefix}tickets ALTER COLUMN status TYPE VARCHAR(24)"))
        for old, new in _OLD_STATUS.items():
            c.execute(text(f"UPDATE {prefix}tickets SET status = :new WHERE status = :old"), {"new": new, "old": old})
        c.execute(text(f"UPDATE {prefix}tickets SET status = 'URGENT' WHERE status = 'TICKET_RAISED' "
                       f"AND priority = 'urgent' AND origin_channel IS NULL"))


def row(r) -> dict | None:
    return dict(r._mapping) if r is not None else None


def fetch_customer(engine: Engine, customer_id: str) -> dict | None:
    with engine.connect() as c:
        return row(c.execute(select(customers).where(customers.c.id == customer_id)).first())


def fetch_customer_by_phone(engine: Engine, phone: str) -> dict | None:
    digits = "".join(ch for ch in phone if ch.isdigit())[-10:]
    if not digits:
        return None
    with engine.connect() as c:
        for r in c.execute(select(customers)):
            if "".join(ch for ch in r.phone if ch.isdigit()).endswith(digits):
                return row(r)
    return None


def _mobile(phone: str | None) -> str:
    return "".join(ch for ch in phone or "" if ch.isdigit())[-10:]


def linked_customer(engine: Engine, phone: str | None) -> str | None:
    """The account a non-registered number was linked to by OTP, if any."""
    digits = _mobile(phone)
    if not digits:
        return None
    with engine.connect() as c:
        return c.execute(select(linked_numbers.c.customer_id).where(linked_numbers.c.phone == digits)).scalar()


def link_number(engine: Engine, phone: str, customer_id: str, holder_phone: str | None) -> None:
    digits = _mobile(phone)
    with engine.begin() as c:
        c.execute(linked_numbers.delete().where(linked_numbers.c.phone == digits))
        c.execute(linked_numbers.insert().values(phone=digits, customer_id=customer_id,
                                                 holder_phone=_mobile(holder_phone) or None, linked_at=now_utc()))


def fetch_rentals(engine: Engine, customer_id: str) -> list[dict]:
    with engine.connect() as c:
        return [row(r) for r in c.execute(select(rentals).where(rentals.c.customer_id == customer_id)
                                          .order_by(rentals.c.id))]
