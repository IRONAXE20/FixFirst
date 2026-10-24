"""FastAPI app: call page, ops view, knowledge view, WebRTC + Exotel voice endpoints and JSON APIs."""
import asyncio
import contextlib
import hmac
import importlib.util
import statistics
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel
from sqlalchemy import desc, select, update

from . import rentbasket
from .bolna import BolnaBridge, completion_body, sse_chunks, webhook_token
from .brain import Brain
from .config import DATA_DIR, WEB_DIR, Settings, get_settings, get_tenant, now_utc
from .db import calls, customers, fetch_rentals, get_engine, outbox, row, tickets
from .demo_llm import KeywordLLM
from .knowledge import DocLibrary
from .llm import ChatModel, make_brain_llm
from .playbooks import get_library
from .safety import get_screen
from .seed import seed_demo
from .session import CallSession, SessionStore
from .tickets import TicketService
from .whatsapp import WhatsApp

UPLOADS = DATA_DIR / "uploads"
VOICE_INSTALLED = importlib.util.find_spec("pipecat") is not None
MAX_PHOTO_BYTES = 10 * 1024 * 1024


class NewSession(BaseModel):
    customer_id: str | None = None
    phone: str | None = None     # look the customer up in RentBasket by mobile number
    mode: str = "voice"          # voice | text
    language: str | None = None


class TextTurn(BaseModel):
    text: str


class TicketPatch(BaseModel):
    status: str


def create_app(settings: Settings | None = None, llm_factory: Callable[[], ChatModel | None] | None = None) -> FastAPI:
    settings = settings or get_settings()
    tenant = get_tenant()
    engine = get_engine(settings.database_url)
    seed_demo(engine)
    library = get_library()
    screen = get_screen()
    docs = DocLibrary()
    ticket_service = TicketService(engine, WhatsApp(settings, engine), tenant, settings)
    store = SessionStore(engine, tenant)
    brains: dict[str, Brain] = {}
    bot_tasks: set[asyncio.Task] = set()

    shared_llm = make_brain_llm(settings)   # stateless and safe to share: one connection pool for every call

    def default_llm() -> ChatModel | None:
        # Claude or Sarvam per settings; with neither key, the offline keyword brain (typed calls only).
        return shared_llm or KeywordLLM()

    async def warm_up() -> None:
        """A fresh process answered its first turn in 14 s (SDK import, first TLS to the model and to Supabase).
        Pay that at boot instead: Bolna's "initiated" webhook wakes a sleeping server ~40 s before the caller speaks."""
        started = asyncio.get_running_loop().time()
        try:
            with engine.connect() as c:
                c.execute(select(1))
            if shared_llm is not None and llm_factory is None:
                await shared_llm.complete([{"role": "user", "content": "Reply with the word ok."}], [])
            logger.info(f"Warm-up done in {int((asyncio.get_running_loop().time() - started) * 1000)} ms")
        except Exception as e:   # never block startup on it
            logger.warning(f"Warm-up failed: {type(e).__name__}: {e}")

    make_llm = llm_factory or default_llm

    def brain_for(session: CallSession) -> Brain:
        if session.id not in brains:
            llm = make_llm()
            if llm is None:
                raise HTTPException(503, "No model key is set. Add ANTHROPIC_API_KEY (or SARVAM_API_KEY) to .env and restart.")
            brains[session.id] = Brain(session, llm, tenant=tenant, settings=settings, library=library,
                                       screen=screen, tickets=ticket_service, docs=docs)
        return brains[session.id]

    def session_or_404(session_id: str) -> CallSession:
        session = store.get(session_id)
        if not session:
            raise HTTPException(404, "Unknown session (it may have expired after a restart)")
        return session

    async def sync_customer(phone: str | None) -> str | None:
        return await rentbasket.sync_customer(engine, settings, phone, language=tenant.default_language)

    bolna = BolnaBridge(store=store, brain_for=brain_for, sync_customer=sync_customer, tenant=tenant)

    async def followup_loop():
        while True:
            try:
                sent = await ticket_service.send_due_followups()
                if sent:
                    logger.info(f"Sent {sent} WhatsApp follow-up(s)")
                await bolna.end_idle()
            except Exception as e:
                logger.exception(f"Follow-up loop error: {e}")
            await asyncio.sleep(30)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        loop_task = asyncio.create_task(followup_loop())
        warm_task = asyncio.create_task(warm_up())
        if not settings.brain_provider:
            logger.warning("No ANTHROPIC_API_KEY or SARVAM_API_KEY: pages work and Type mode runs the offline demo brain.")
        elif not settings.sarvam_ready:
            logger.info(f"Brain: {settings.brain_provider}. Browser voice needs SARVAM_API_KEY; Bolna phone calls don't.")
        yield
        loop_task.cancel()
        warm_task.cancel()
        for t in list(bot_tasks):
            t.cancel()

    app = FastAPI(title="FixFirst", lifespan=lifespan)
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
    UPLOADS.mkdir(parents=True, exist_ok=True)
    app.mount("/media", StaticFiles(directory=UPLOADS), name="media")

    # ---- pages -----------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    async def call_page():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/ops", include_in_schema=False)
    async def ops_page():
        return FileResponse(WEB_DIR / "ops.html")

    @app.get("/knowledge", include_in_schema=False)
    async def knowledge_page():
        return FileResponse(WEB_DIR / "knowledge.html")

    @app.get("/u/{token}", include_in_schema=False)
    async def upload_page(token: str):
        return FileResponse(WEB_DIR / "upload.html")

    # ---- config & customers ------------------------------------------------------------
    @app.get("/api/config")
    async def config():
        return {
            "tenant": {"name": tenant.name, "agent_name": tenant.agent_name, "service_area": tenant.service_area,
                       "support_phone": tenant.support_phone, "emergency_numbers": tenant.emergency_numbers},
            "sarvam_ready": settings.sarvam_ready and VOICE_INSTALLED, "whatsapp_mode": "live" if ticket_service.whatsapp.live else "dry_run",
            "models": {"llm": settings.llm_model, "stt": f"{settings.stt_model} ({settings.stt_mode})",
                       "tts": f"{settings.tts_model} / {tenant.tts_speaker}"},
            "doc_qa": settings.enable_doc_qa,
            "brain": settings.brain_provider or "offline_demo",
            "brain_model": {"anthropic": settings.anthropic_model, "sarvam": settings.llm_model}.get(
                settings.brain_provider or "", "keyword demo"),
            "customer_source": settings.customer_source,
        }

    @app.get("/api/customers")
    async def list_customers():
        with engine.connect() as c:
            custs = [row(r) for r in c.execute(select(customers).order_by(customers.c.id))]
        return [{**cust, "rentals": fetch_rentals(engine, cust["id"])} for cust in custs]

    class Lookup(BaseModel):
        phone: str

    @app.post("/api/customers/lookup")
    async def lookup_customer(body: Lookup):
        customer_id = await sync_customer(body.phone)
        if not customer_id:
            raise HTTPException(404, "No RentBasket customer found for that number")
        with engine.connect() as c:
            cust = row(c.execute(select(customers).where(customers.c.id == customer_id)).first())
        return {**cust, "rentals": fetch_rentals(engine, customer_id)}

    # ---- sessions ----------------------------------------------------------------------
    @app.post("/api/sessions")
    async def new_session(body: NewSession):
        customer_id = body.customer_id
        if body.phone:
            customer_id = await sync_customer(body.phone)
            if not customer_id:
                raise HTTPException(404, "No RentBasket customer found for that number")
        session = store.create(channel="text" if body.mode == "text" else "web", customer_id=customer_id)
        if body.language in ("hi", "en"):
            session.language = body.language
        greeting = None
        if body.mode == "text":
            greeting = brain_for(session).greeting()
        return {"session_id": session.id, "greeting": greeting, "session": session.snapshot()}

    @app.get("/api/sessions/{session_id}")
    async def get_session(session_id: str):
        return session_or_404(session_id).snapshot()

    @app.post("/api/sessions/{session_id}/text")
    async def text_turn(session_id: str, body: TextTurn):
        session = session_or_404(session_id)
        result = await brain_for(session).respond(body.text)
        return {"speech": result.speech, "hazard": result.hazard, "session": session.snapshot()}

    @app.post("/api/sessions/{session_id}/end")
    async def end_session(session_id: str):
        session = session_or_404(session_id)
        if session.id in brains:
            await brains[session.id].on_hangup()
        else:
            session.ended = True
        return session.snapshot()

    @app.websocket("/api/sessions/{session_id}/events")
    async def session_events(ws: WebSocket, session_id: str):
        session = store.get(session_id)
        await ws.accept()
        if not session:
            await ws.close(code=4404)
            return
        queue = session.subscribe()
        try:
            while True:
                await ws.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            session.unsubscribe(queue)

    @app.post("/api/sessions/{session_id}/photos")
    async def session_photo(session_id: str, file: UploadFile = File(...)):
        session = session_or_404(session_id)
        rel = await _save_photo(file, f"call-{session.id}")
        ticket_service.attach_session_photo(session, rel)
        return {"path": rel, "url": f"/media/{rel}"}

    # ---- voice: browser (WebRTC) and Exotel, through Pipecat ------------------------------
    # Pipecat is optional: the hosted server (requirements-render.txt) leaves it out, since phone
    # calls go through Bolna and WebRTC needs UDP, which Render doesn't accept.
    if VOICE_INSTALLED:
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.request_handler import (IceCandidate, SmallWebRTCPatchRequest,
                                                                    SmallWebRTCRequest, SmallWebRTCRequestHandler)
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

        from .voice import run_bot

        webrtc = SmallWebRTCRequestHandler()

        def start_bot(coro) -> None:
            task = asyncio.create_task(coro)
            bot_tasks.add(task)
            task.add_done_callback(bot_tasks.discard)

        @app.post("/api/offer")
        async def webrtc_offer(request: Request):
            body = await request.json()
            req = SmallWebRTCRequest.from_dict(body)
            if not settings.sarvam_ready:
                raise HTTPException(503, "Voice calls need SARVAM_API_KEY in .env. Use Type mode to try the offline demo.")
            session = session_or_404((req.request_data or {}).get("session_id", ""))
            brain = brain_for(session)

            async def on_connection(connection):
                transport = SmallWebRTCTransport(webrtc_connection=connection,
                                                 params=TransportParams(audio_in_enabled=True, audio_out_enabled=True))
                start_bot(run_bot(transport, brain, settings, tenant))

            return await webrtc.handle_web_request(req, on_connection)

        @app.patch("/api/offer")
        async def webrtc_ice(request: Request):
            body = await request.json()
            await webrtc.handle_patch_request(SmallWebRTCPatchRequest(
                pc_id=body["pc_id"],
                candidates=[IceCandidate(candidate=c.get("candidate", ""), sdp_mid=c.get("sdp_mid") or c.get("sdpMid"),
                                         sdp_mline_index=c.get("sdp_mline_index", c.get("sdpMLineIndex", 0)))
                            for c in body.get("candidates", [])]))
            return {"status": "ok"}

        # ---- voice: phone (Exotel Voicebot applet -> wss://<host>/ws/exotel) -----------------
        @app.websocket("/ws/exotel")
        async def exotel(ws: WebSocket):
            from pipecat.runner.utils import parse_telephony_websocket
            from pipecat.serializers.exotel import ExotelFrameSerializer
            from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

            await ws.accept()
            if not settings.sarvam_ready:
                logger.error("Exotel call rejected: SARVAM_API_KEY is not set")
                await ws.close()
                return
            _kind, call_data = await parse_telephony_websocket(ws)
            caller = call_data.get("from") or None
            session = store.create(channel="phone", customer_id=await sync_customer(caller), caller_phone=caller)
            serializer = ExotelFrameSerializer(stream_sid=call_data["stream_id"], call_sid=call_data.get("call_id"))
            transport = FastAPIWebsocketTransport(websocket=ws, params=FastAPIWebsocketParams(
                audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
            await run_bot(transport, brain_for(session), settings, tenant, sample_rate_in=8000, sample_rate_out=8000)


    # ---- phone via Bolna (FixFirst as Bolna's custom LLM) -----------------------------------
    def check_llm_key(request: Request) -> None:
        if not settings.fixfirst_llm_key:
            raise HTTPException(503, "FIXFIRST_LLM_KEY is not set; run `python -m fixfirst bolna-setup` first")
        if request.headers.get("authorization", "") != f"Bearer {settings.fixfirst_llm_key}":
            raise HTTPException(401, "bad key")

    @app.get("/bolna/v1/models")
    async def bolna_models(request: Request):
        check_llm_key(request)
        return {"object": "list", "data": [{"id": "fixfirst", "object": "model", "owned_by": "fixfirst"}]}

    @app.post("/bolna/v1/chat/completions")
    async def bolna_chat(request: Request):
        check_llm_key(request)
        body = await request.json()
        messages = body.get("messages") or []
        model = body.get("model") or "fixfirst"
        if not settings.brain_provider and llm_factory is None:   # the offline keyword brain isn't for real calls
            raise HTTPException(503, "Set ANTHROPIC_API_KEY (or SARVAM_API_KEY) for phone calls")
        if not any(m.get("role") == "user" for m in messages):   # Bolna's own checks before the caller speaks
            speech = ""
        else:
            speech = await bolna.turn(messages)
        if not body.get("stream"):
            return completion_body(speech, model)

        async def stream():
            try:
                for piece in sse_chunks(speech, model):
                    yield piece
            except asyncio.CancelledError:
                bolna.stream_cancelled(messages)
                raise

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.post("/bolna/webhook/{token}")
    async def bolna_webhook(request: Request, token: str):
        if not settings.fixfirst_llm_key or not hmac.compare_digest(token, webhook_token(settings.fixfirst_llm_key)):
            raise HTTPException(401, "bad token")
        return {"status": await bolna.webhook(await request.json())}

    # ---- tickets, calls, stats -----------------------------------------------------------
    @app.get("/api/tickets")
    async def list_tickets():
        with engine.connect() as c:
            out = [row(r) for r in c.execute(select(tickets).order_by(desc(tickets.c.id)).limit(200))]
        for t in out:
            t["photo_url"] = ticket_service.photo_url(t["photo_token"])
            t["photos"] = [f"/media/{p}" for p in t["photos"] or []]
        return out

    @app.patch("/api/tickets/{code}")
    async def patch_ticket(code: str, body: TicketPatch):
        if body.status not in ("open", "assigned", "closed"):
            raise HTTPException(400, "status must be open, assigned or closed")
        with engine.begin() as c:
            res = c.execute(update(tickets).where(tickets.c.code == code).values(status=body.status, updated_at=now_utc()))
        if not res.rowcount:
            raise HTTPException(404, "Unknown ticket")
        return {"code": code, "status": body.status}

    @app.get("/api/calls")
    async def list_calls():
        with engine.connect() as c:
            out = [row(r) for r in c.execute(select(calls).order_by(desc(calls.c.started_at)).limit(200))]
        names = {r["id"]: r["name"] for r in await list_customers()}
        for call in out:
            call["customer_name"] = names.get(call["customer_id"])
        return out

    @app.get("/api/outbox")
    async def list_outbox():
        with engine.connect() as c:
            return [row(r) for r in c.execute(select(outbox).order_by(desc(outbox.c.id)).limit(200))]

    @app.get("/api/stats")
    async def stats():
        with engine.connect() as c:
            all_calls = [row(r) for r in c.execute(select(calls))]
            open_tickets = sum(1 for _ in c.execute(select(tickets.c.id).where(tickets.c.status != "closed")))
        finished = [x for x in all_calls if x["outcome"] not in ("in_progress",)]
        by = {k: sum(1 for x in finished if x["outcome"] == k) for k in ("resolved", "escalated", "safety", "followup", "abandoned")}
        decided = by["resolved"] + by["escalated"]
        v2v = [t["voice_to_voice_ms"] for x in all_calls for t in (x["latency"] or []) if t.get("voice_to_voice_ms")]
        brain_ms = [t["brain_ms"] for x in all_calls for t in (x["latency"] or []) if t.get("brain_ms")]
        return {
            "calls": len(all_calls), **by, "open_tickets": open_tickets,
            "resolved_rate": round(by["resolved"] / decided, 3) if decided else None,
            "median_voice_to_voice_ms": int(statistics.median(v2v)) if v2v else None,
            "median_brain_ms": int(statistics.median(brain_ms)) if brain_ms else None,
            "visit_cost_inr": tenant.technician_visit_cost_inr,
            "estimated_savings_inr": by["resolved"] * tenant.technician_visit_cost_inr or None,
        }

    # ---- photo upload (link sent on WhatsApp) ----------------------------------------------
    @app.get("/api/upload/{token}")
    async def upload_info(token: str):
        with engine.connect() as c:
            t = row(c.execute(select(tickets).where(tickets.c.photo_token == token)).first())
        if not t:
            raise HTTPException(404, "This link is invalid or has expired")
        s = t["summary"]
        return {"code": t["code"], "appliance": s.get("appliance_label"), "name": (s.get("customer") or {}).get("name"),
                "photos": len(t["photos"] or []), "company": tenant.name}

    @app.post("/api/upload/{token}")
    async def upload_photo(token: str, file: UploadFile = File(...)):
        with engine.connect() as c:
            t = row(c.execute(select(tickets).where(tickets.c.photo_token == token)).first())
        if not t:
            raise HTTPException(404, "This link is invalid or has expired")
        rel = await _save_photo(file, t["code"])
        ticket_service.add_photo(token, rel)
        live = store.get(t["call_id"] or "")
        if live:
            live.publish("photo", path=rel)
        return {"ok": True, "url": f"/media/{rel}"}

    # ---- knowledge -------------------------------------------------------------------------
    @app.get("/api/playbooks")
    async def list_playbooks():
        return [{"id": pb.id, "appliance": pb.appliance, "title": pb.title, "status": pb.status,
                 "reviewed_by": pb.reviewed_by, "steps": len(pb.nodes)} for pb in library.playbooks.values()]

    @app.get("/api/playbooks/{playbook_id}")
    async def get_playbook(playbook_id: str):
        if playbook_id not in library.playbooks:
            raise HTTPException(404, "Unknown playbook")
        return library.get(playbook_id).to_dict()

    @app.get("/api/sources")
    async def list_sources():
        return library.sources

    @app.get("/api/safety")
    async def safety_scripts():
        return {kind: {"label": h["label"], "script": h["script"]} for kind, h in screen.hazards.items()}

    @app.get("/api/docs")
    async def list_docs():
        return docs.files

    @app.get("/api/docs/search")
    async def search_docs(q: str, appliance: str | None = None):
        return docs.search(q, k=6, appliance=appliance)

    @app.post("/api/docs")
    async def upload_doc(file: UploadFile = File(...)):
        data = await file.read()
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(413, "File too large (max 25 MB)")
        try:
            path = docs.save_upload(file.filename or "document", data)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"name": path.name, "files": docs.files}

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception):
        logger.exception(exc)
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)

    return app


async def _save_photo(file: UploadFile, folder: str) -> str:
    if not (file.content_type or "").startswith("image/"):
        raise HTTPException(400, "Please upload an image")
    data = await file.read()
    if len(data) > MAX_PHOTO_BYTES:
        raise HTTPException(413, "Image too large (max 10 MB)")
    ext = Path(file.filename or "").suffix.lower() if Path(file.filename or "").suffix.lower() in (".jpg", ".jpeg", ".png", ".webp", ".heic") else ".jpg"
    target = UPLOADS / folder
    target.mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex[:10]}{ext}"
    (target / name).write_bytes(data)
    return f"{folder}/{name}"
