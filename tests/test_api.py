import base64

import pytest
from fastapi.testclient import TestClient

import fixfirst.server as server
from fixfirst.config import Settings
from fixfirst.llm import LLMResult, ScriptedLLM, call

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'api.db').as_posix()}", whatsapp_mode="dry_run",
                        public_base_url="http://test", sarvam_api_key="")
    scripts = {"responses": []}
    app = server.create_app(settings, llm_factory=lambda: ScriptedLLM(scripts["responses"]))
    with TestClient(app) as c:
        c.scripts = scripts
        c.db_url = settings.database_url
        yield c


def test_pages_and_config(client):
    for path in ("/", "/ops", "/knowledge", "/u/anything"):
        assert client.get(path).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["tenant"]["name"] == "RentBasket" and cfg["sarvam_ready"] is False
    assert len(client.get("/api/customers").json()) == 3
    assert {p["appliance"] for p in client.get("/api/playbooks").json()} == {"ac", "washer", "fridge", "microwave",
                                                                            "water_purifier", "furniture"}
    assert client.get("/api/playbooks/ac_not_cooling").json()["entry"] == "triage"
    assert "gas" in client.get("/api/safety").json()


def test_doc_search_finds_cited_passages(client):
    # The brand manuals were archived with the move to RentBasket's SOP; uploaded manuals still go through
    # the same index, so it is tested on the archived set.
    from fixfirst.config import KNOWLEDGE_DIR
    from fixfirst.knowledge import DocLibrary
    assert client.get("/api/docs/search", params={"q": "drain hose 5C"}).json() == []
    hits = DocLibrary([KNOWLEDGE_DIR / "archive" / "docs"]).search("drain hose 5C")
    assert hits and hits[0]["url"].startswith("https://")


def test_typed_call_to_ticket_with_photo(client):
    client.scripts["responses"] += [
        LLMResult(tool_calls=[call("escalate_to_technician", reason="Fridge not cooling", caller_requested=True, language="en")]),
    ]
    s = client.post("/api/sessions", json={"customer_id": "C2", "mode": "text", "language": "en"}).json()
    assert "Rahul" in s["greeting"]
    sid = s["session_id"]
    with client.websocket_connect(f"/api/sessions/{sid}/events") as ws:
        assert ws.receive_json()["type"] == "agent"   # greeting replayed
    turn = client.post(f"/api/sessions/{sid}/text", json={"text": "Just send a technician please"}).json()
    assert turn["session"]["outcome"] == "escalated"
    ticket = client.get("/api/tickets").json()[0]
    assert ticket["code"] == turn["session"]["ticket"]["code"]

    info = client.get(f"/api/upload/{ticket['photo_token']}").json()
    assert info["code"] == ticket["code"]
    up = client.post(f"/api/upload/{ticket['photo_token']}", files={"file": ("p.png", PNG, "image/png")})
    assert up.status_code == 200
    assert client.get("/api/tickets").json()[0]["photos"]
    assert client.post(f"/api/upload/{ticket['photo_token']}", files={"file": ("x.txt", b"hi", "text/plain")}).status_code == 400

    assert client.patch(f"/api/tickets/{ticket['code']}", json={"status": "SCHEDULED"}).json()["status"] == "SCHEDULED"
    stats = client.get("/api/stats").json()
    assert stats["escalated"] == 1 and stats["open_tickets"] == 1
    kinds = {m["audience"] for m in client.get("/api/outbox").json()}
    assert kinds == {"team", "customer"}
    client.post(f"/api/sessions/{sid}/end")
    assert client.get("/api/calls").json()[0]["outcome"] == "escalated"


def test_safety_turn_never_reaches_the_llm(client):
    s = client.post("/api/sessions", json={"customer_id": "C1", "mode": "text"}).json()
    turn = client.post(f"/api/sessions/{s['session_id']}/text", json={"text": "plug se chingari aa rahi hai"}).json()
    assert turn["hazard"] == "electrical" and turn["session"]["ticket"]["priority"] == "urgent"


def test_unknown_session_is_404(client):
    assert client.post("/api/sessions/nope/text", json={"text": "hi"}).status_code == 404



def test_photos_live_in_the_database_and_go_7_days_after_the_ticket_closes(client):
    from datetime import timedelta

    from sqlalchemy import select, update

    from fixfirst import photos as photo_store
    from fixfirst.config import now_utc
    from fixfirst.db import get_engine, photos, tickets

    client.scripts["responses"].append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", language="en")]))
    sid = client.post("/api/sessions", json={"customer_id": "C1", "mode": "text", "language": "en"}).json()["session_id"]
    ticket = client.post(f"/api/sessions/{sid}/text", json={"text": "my fridge is broken"}).json()["session"]["ticket"]
    url = client.post(f"/api/upload/{ticket['photo_token']}", files={"file": ("a.png", PNG, "image/png")}).json()["url"]
    assert url.startswith("/media/p/") and client.get(url).content == PNG
    engine = get_engine(client.db_url)
    assert photo_store.purge(engine, 7) == 0                      # ticket still open: kept
    client.patch(f"/api/tickets/{ticket['code']}", json={"status": "CLOSED"})
    assert photo_store.purge(engine, 7) == 0                      # closed today: inside the 7-day buffer
    with engine.begin() as c:
        c.execute(update(tickets).where(tickets.c.code == ticket["code"]).values(updated_at=now_utc() - timedelta(days=8)))
    assert photo_store.purge(engine, 7) == 1
    assert client.get(url).status_code == 404
    with engine.connect() as c:
        assert c.execute(select(photos)).first() is None
        assert c.execute(select(tickets.c.photos).where(tickets.c.code == ticket["code"])).scalar() == []



@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "UPLOADS", tmp_path / "uploads")
    settings = Settings(database_url=f"sqlite:///{(tmp_path / 'pb.db').as_posix()}", whatsapp_mode="dry_run",
                        public_base_url="http://test", admin_key="admin-test")
    app = server.create_app(settings, llm_factory=lambda: ScriptedLLM([]))
    with TestClient(app) as c:
        yield c


def test_playbooks_are_stored_versioned_and_validated_before_saving(admin_client):
    import yaml
    c = admin_client
    listing = {p["id"]: p for p in c.get("/api/playbooks").json()}
    assert listing["ac_not_cooling"]["version"] == 1                     # seeded from the YAML file
    src = c.get("/api/playbooks/ac_not_cooling/source").json()
    doc = yaml.safe_load(src["yaml"])
    original = doc["title"]["en"]
    doc["title"]["en"] = "AC not cooling (edited)"
    edited = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    assert c.put("/api/playbooks/ac_not_cooling", json={"yaml": edited}).status_code == 401   # no admin key
    H = {"X-Admin-Key": "admin-test"}
    broken = dict(doc, nodes={**doc["nodes"], "ac04_off": {**doc["nodes"]["ac04_off"], "say": {"en": "only English"}}})
    r = c.put("/api/playbooks/ac_not_cooling", json={"yaml": yaml.safe_dump(broken, allow_unicode=True)}, headers=H)
    assert r.status_code == 422 and any("say" in e for e in r.json()["errors"])   # rejected, nothing saved
    r = c.put("/api/playbooks/ac_not_cooling", json={"yaml": edited, "by": "test", "base_version": 1}, headers=H)
    assert r.json() == {"ok": True, "version": 2}
    assert c.get("/api/playbooks/ac_not_cooling").json()["title"]["en"] == "AC not cooling (edited)"   # live at once
    r = c.put("/api/playbooks/ac_not_cooling", json={"yaml": edited, "base_version": 1}, headers=H)
    assert r.status_code == 422 and "version 2" in r.json()["errors"][0]           # someone else saved meanwhile
    assert [h["version"] for h in c.get("/api/playbooks/ac_not_cooling/source").json()["history"]] == [2, 1]
    doc["title"]["en"] = original
    c.put("/api/playbooks/ac_not_cooling", json={"yaml": yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)}, headers=H)


def test_import_retires_playbooks_that_no_longer_have_a_file(tmp_path):
    # Moving to RentBasket's SOP archived the TV, inverter, purifier, stove and semi-automatic playbooks:
    # a database seeded before that must stop serving them.
    from sqlalchemy import insert, select

    from fixfirst import playbook_store
    from fixfirst.config import now_utc
    from fixfirst.db import get_engine, playbook_docs
    from fixfirst.playbooks import PlaybookLibrary

    engine = get_engine(f"sqlite:///{(tmp_path / 'retire.db').as_posix()}")
    old = {"id": "tv_problem", "appliance": "tv", "entry": "a", "nodes": {"a": {"kind": "resolve", "say": {"en": "x", "hi": "x"}}}}
    with engine.begin() as c:
        c.execute(insert(playbook_docs).values(id="tv_problem", appliance="tv", status="draft-unreviewed", version=3,
                                               doc=old, updated_at=now_utc(), updated_by="seed"))
    library = PlaybookLibrary()
    playbook_store.sync(engine, library)
    assert "tv_problem" in library.playbooks
    saved = playbook_store.import_files(engine, library)
    assert set(saved) == {"ac_not_cooling", "fridge_not_cooling", "washer_problem", "microwave_problem",
                          "water_purifier_problem", "furniture_problem"}
    assert "tv_problem" not in library.playbooks
    with engine.connect() as c:
        assert "tv_problem" not in {r.id for r in c.execute(select(playbook_docs.c.id))}


def test_customer_link_lists_open_tickets_takes_notes_and_asks_which_ticket(client):
    from sqlalchemy import select

    from fixfirst.db import customers, get_engine
    for text in ("my fridge is broken", "my washing machine is broken"):
        client.scripts["responses"].append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", language="en")]))
        sid = client.post("/api/sessions", json={"customer_id": "C1", "mode": "text", "language": "en"}).json()["session_id"]
        client.post(f"/api/sessions/{sid}/text", json={"text": text})
    with get_engine(client.db_url).connect() as c:
        token = c.execute(select(customers.c.link_token).where(customers.c.id == "C1")).scalar()
    info = client.get(f"/api/upload/{token}").json()
    assert info["persistent"] and len(info["tickets"]) == 2 and info["name"] == "Ananya Sharma"
    # Two open tickets: the customer has to pick one.
    assert client.post(f"/api/upload/{token}/note", json={"note": "it smells too"}).status_code == 400
    code = info["tickets"][0]["code"]
    assert client.post(f"/api/upload/{token}/note", json={"note": "it smells too", "ticket": code}).json()["ticket"] == code
    up = client.post(f"/api/upload/{token}", data={"ticket": code}, files={"file": ("p.png", PNG, "image/png")})
    assert up.status_code == 200 and up.json()["ticket"] == code
    t = next(t for t in client.get("/api/tickets").json() if t["code"] == code)
    assert t["summary"]["customer_notes"][0]["note"] == "it smells too" and t["photos"]


def test_customer_link_works_through_the_broken_template_button(client):
    # technician_booked's button was approved as /u/%7B%7B1%7D%7D{{1}}: links arrive as /u/{{1}}<token>.
    from sqlalchemy import select

    from fixfirst.db import customers, get_engine
    client.scripts["responses"].append(LLMResult(tool_calls=[call("escalate_to_technician", reason="x", language="en")]))
    sid = client.post("/api/sessions", json={"customer_id": "C1", "mode": "text", "language": "en"}).json()["session_id"]
    client.post(f"/api/sessions/{sid}/text", json={"text": "my fridge is broken"})
    with get_engine(client.db_url).connect() as c:
        token = c.execute(select(customers.c.link_token).where(customers.c.id == "C1")).scalar()
    assert client.get(f"/api/upload/%7B%7B1%7D%7D{token}").json()["name"] == "Ananya Sharma"
