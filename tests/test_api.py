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
        yield c


def test_pages_and_config(client):
    for path in ("/", "/ops", "/knowledge", "/u/anything"):
        assert client.get(path).status_code == 200
    cfg = client.get("/api/config").json()
    assert cfg["tenant"]["name"] == "RentBasket" and cfg["sarvam_ready"] is False
    assert len(client.get("/api/customers").json()) == 3
    assert {p["appliance"] for p in client.get("/api/playbooks").json()} == {"ac", "washer", "washer_semi", "fridge", "inverter", "tv", "water_purifier", "stove"}
    assert client.get("/api/playbooks/ac_not_cooling").json()["entry"] == "safety_check"
    assert "gas" in client.get("/api/safety").json()


def test_doc_search_finds_cited_passages(client):
    hits = client.get("/api/docs/search", params={"q": "drain hose 5C"}).json()
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

    assert client.patch(f"/api/tickets/{ticket['code']}", json={"status": "assigned"}).json()["status"] == "assigned"
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
