"""Storage: SQLite locally, Supabase Postgres in production (same schema via SQLAlchemy Core)."""
from functools import lru_cache
from pathlib import Path

from sqlalchemy import (JSON, Column, DateTime, ForeignKey, Integer, LargeBinary, MetaData, String, Table, Text,
                        create_engine, select)
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
    Column("status", String(16), nullable=False, default="open"),   # open | assigned | closed
    Column("summary", JSON, nullable=False),
    Column("photo_token", String(40), unique=True),
    Column("photos", JSON, default=list),
    Column("created_at", DateTime, nullable=False),
    Column("updated_at", DateTime, nullable=False),
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
    return engine


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


def fetch_rentals(engine: Engine, customer_id: str) -> list[dict]:
    with engine.connect() as c:
        return [row(r) for r in c.execute(select(rentals).where(rentals.c.customer_id == customer_id)
                                          .order_by(rentals.c.id))]
