"""Demo customers and rentals (made-up people and brands) so the call page works out of the box."""
from sqlalchemy import delete, insert, select
from sqlalchemy.engine import Engine

from .config import now_utc
from .db import customers, rentals

DEMO_CUSTOMERS = [
    {"id": "C1", "name": "Ananya Sharma", "phone": "+91 90000 00001", "language": "hi",
     "area": "Sector 52, Gurgaon", "address": "Tower B, Ardee City, Sector 52, Gurgaon"},
    {"id": "C2", "name": "Rahul Verma", "phone": "+91 90000 00002", "language": "en",
     "area": "Sector 116, Noida", "address": "Flat 1203, Sector 116, Noida"},
    {"id": "C3", "name": "Meera Iyer", "phone": "+91 90000 00003", "language": "en",
     "area": "DLF Phase 3, Gurgaon", "address": "U-Block, DLF Phase 3, Gurgaon"},
]

DEMO_RENTALS = [
    ("R11", "C1", "ac", "Split AC 1.5 ton (bedroom)", "Voltas", None),
    ("R12", "C1", "washer", "Fully automatic top-load washing machine 6.5 kg", "LG", None),
    ("R13", "C1", "fridge", "Single door fridge 190 L", "Whirlpool", None),
    ("R14", "C1", "washer_semi", "Semi automatic washing machine 7 kg (balcony)", "Whirlpool", None),
    ("R21", "C2", "ac", "Window AC 1.5 ton", "LG", None),
    ("R22", "C2", "washer", "Fully automatic top-load washing machine 7 kg", "Samsung", None),
    ("R23", "C2", "fridge", "Double door fridge 253 L", "Samsung", None),
    ("R24", "C2", "inverter", "Inverter with single battery", "Luminous", None),
    ("R25", "C2", "stove", "Gas stove, 3 burner", None, None),
    ("R26", "C2", "water_purifier", "RO water purifier (kitchen)", "Aquaguard", None),
    ("R31", "C3", "ac", "Split AC 1.5 ton (living room)", "Daikin", None),
    ("R32", "C3", "washer", "Fully automatic top-load washing machine 7 kg", "Whirlpool", None),
    ("R33", "C3", "tv", "Smart LED TV 43 inch", "Samsung", None),
    ("R34", "C3", "microwave", "Solo microwave 20 L", "IFB", None),
]


def seed_demo(engine: Engine) -> bool:
    """Insert the demo customers if missing, and keep their rentals in line with DEMO_RENTALS.
    Returns True if it added the customers."""
    demo_ids = [c["id"] for c in DEMO_CUSTOMERS]
    with engine.begin() as c:
        have = {r[0] for r in c.execute(select(customers.c.id).where(customers.c.id.in_(demo_ids)))}
        missing = [cust for cust in DEMO_CUSTOMERS if cust["id"] not in have]
        if missing:
            c.execute(insert(customers), [{**cust, "created_at": now_utc()} for cust in missing])
        c.execute(delete(rentals).where(rentals.c.customer_id.in_(demo_ids)))
        c.execute(insert(rentals), [
            {"id": rid, "customer_id": cid, "appliance": app, "label": label, "brand": brand, "model": model,
             "details": {"demo": True}}
            for rid, cid, app, label, brand, model in DEMO_RENTALS])
    return bool(missing)
