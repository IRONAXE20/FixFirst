"""Customer lookup from RentBasket's own API, copied into the local customers/rentals tables so tickets,
the ops view and the brain work the same as with demo data."""
import re
import time

import httpx
from loguru import logger
from sqlalchemy import delete, insert, select, update
from sqlalchemy.engine import Engine

from .config import Settings, now_utc
from .db import customers, linked_customer, rentals

CACHE_SECONDS = 600
_cache: dict[str, tuple[float, dict | None]] = {}

# Product names look like 'Window AC - 2009' or 'Fully Automatic Washing M/c - 2025'; order matters.
_APPLIANCE_RULES = [
    ("washer", r"washing|washer|semi[\s-]*auto"),   # RentBasket's SOP covers top, front and semi-automatic together
    ("ac", r"\bac\b|air\s*condition"),
    ("fridge", r"fridge|refrigerator"),
    ("tv", r"\b(led|tv|television)\b"),
    ("inverter", r"inverter|battery"),
    ("water_purifier", r"purifier|\bro\b"),
    ("stove", r"stove|burner|\bhob\b"),
    ("otg", r"\botg\b"),                            # an oven-toaster-grill isn't covered by the microwave SOP
    ("microwave", r"microwave"),
    ("induction", r"induction"),
    ("geyser", r"geyser|water heater"),
]


def appliance_for(product_name: str) -> str:
    name = product_name.lower()
    for appliance, pattern in _APPLIANCE_RULES:
        if re.search(pattern, name):
            return appliance
    return "furniture"


def mobile10(phone: str | None) -> str | None:
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 10 else None


def _area(address: str) -> str:
    parts = [p.strip() for p in address.split(",") if p.strip()]
    return ", ".join(parts[-3:])[:120] if parts else ""


def parse(data: dict, mobile: str, language: str) -> tuple[dict, list[dict]]:
    """API payload -> (customer row, rental rows)."""
    cust_id = f"RB{mobile}"
    addresses = [a for a in data.get("address") or [] if isinstance(a, str) and a.strip()]
    customer = {"id": cust_id, "name": (data.get("name") or "Customer").strip()[:120], "phone": f"+91 {mobile}",
                "language": language, "area": _area(addresses[0]) if addresses else None,
                "address": addresses[0][:300] if addresses else None}
    rows = []
    for pid, name in zip(data.get("product_ids") or [], data.get("product_names") or []):
        name = str(name)
        m = re.match(r"^(.*?)\s*-\s*((?:19|20)\d\d)$", name.strip())
        label, year = (m.group(1), m.group(2)) if m else (name.strip(), None)
        rows.append({"id": f"{cust_id}-{pid}"[:32], "customer_id": cust_id, "appliance": appliance_for(label),
                     "label": label[:160], "brand": None, "model": None,
                     "details": {"source": "rentbasket", "product_id": str(pid), "year": year}})
    return customer, rows


async def fetch(settings: Settings, mobile: str) -> dict | None:
    """The API's `data` object for a 10-digit mobile number, or None (unknown number or API down)."""
    hit = _cache.get(mobile)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            r = await client.get(settings.rentbasket_api_url, params={"mobile_no": mobile},
                                 headers={"Accept": "application/json"})
        body = r.json() if r.status_code == 200 else {}
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"RentBasket lookup failed: {type(e).__name__}")
        return None   # not cached, so the next call retries
    data = body.get("data") if body.get("status") else None
    if not isinstance(data, dict) or not data.get("name"):
        data = None
    _cache[mobile] = (time.monotonic(), data)
    return data


async def sync_customer(engine: Engine, settings: Settings, phone: str | None, language: str = "hi") -> str | None:
    """Look the caller up and upsert them locally. Returns the local customer id, or None if not found."""
    mobile = mobile10(phone)
    if not mobile or settings.customer_source != "rentbasket":
        return None
    data = await fetch(settings, mobile)
    if not data:
        # Not on any deal: maybe a roommate's or family member's number linked to an account by OTP.
        holder = linked_customer(engine, mobile)
        if holder and holder.startswith("RB") and holder[2:] != mobile:
            await sync_customer(engine, settings, holder[2:], language)   # refresh the account holder's deal
        return holder
    customer, rows = parse(data, mobile, language)
    with engine.begin() as c:
        exists = c.execute(select(customers.c.id).where(customers.c.id == customer["id"])).first()
        if exists:
            c.execute(update(customers).where(customers.c.id == customer["id"])
                      .values(**{k: v for k, v in customer.items() if k not in ("id", "language")}))
        else:
            c.execute(insert(customers).values(**customer, created_at=now_utc()))
        c.execute(delete(rentals).where(rentals.c.customer_id == customer["id"]))
        if rows:
            c.execute(insert(rentals), rows)
        else:   # the rental period has ended: the customer's photo link stops working
            c.execute(update(customers).where(customers.c.id == customer["id"]).values(link_token=None))
    return customer["id"]
