import { api, esc, ms, initChrome, LOGO, APPLIANCE_ICON } from "/static/common.js";

const $ = (id) => document.getElementById(id);
const state = {
  cfg: null, customers: [], customerId: null, lang: "hi", mode: "voice",
  sessionId: null, ws: null, pc: null, localStream: null, muted: false, live: false,
  audioCtx: null, meters: [], steps: 0, partialEl: null, timer: null, startedAt: 0,
};

$("logo").innerHTML = LOGO;

// ---- setup ---------------------------------------------------------------------------
function seg(id, value, onChange) {
  const el = $(id);
  const set = (v) => el.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  set(value);
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b || state.live) return;
    set(b.dataset.v);
    onChange(b.dataset.v);
  });
}

function renderCustomers() {
  $("customers").innerHTML = state.customers.map((c) => `
    <button class="cust-item ${c.id === state.customerId ? "on" : ""}" data-id="${esc(c.id)}">
      <div class="cust-name">${esc(c.name)}</div>
      <div class="cust-area">${esc(c.area)} · ${c.language === "hi" ? "prefers Hindi" : "prefers English"}</div>
      <div class="rentals">${c.rentals.map((r) => `<span class="rental">${APPLIANCE_ICON[r.appliance] || "•"} ${esc(r.label.split(",")[0])}${r.brand ? ` · ${esc(r.brand)}` : ""}</span>`).join("")}</div>
    </button>`).join("");
}

$("customers").addEventListener("click", (e) => {
  const b = e.target.closest(".cust-item");
  if (!b || state.live) return;
  state.customerId = b.dataset.id;
  const c = state.customers.find((x) => x.id === state.customerId);
  state.lang = c?.language || state.lang;
  seg("lang", state.lang, (v) => (state.lang = v));
  renderCustomers();
});

$("lookup").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (state.live) return;
  const phone = $("lookup-phone").value.trim();
  const msg = $("lookup-msg");
  if (!phone) return;
  msg.classList.remove("hidden");
  msg.textContent = "Looking up…";
  try {
    const c = await api("/api/customers/lookup", { phone });
    state.customers = [c, ...state.customers.filter((x) => x.id !== c.id)];
    state.customerId = c.id;
    msg.textContent = `Found ${c.name} · ${c.rentals.length} rented item(s).`;
    renderCustomers();
  } catch (err) {
    msg.textContent = err.message || "No RentBasket customer found for that number.";
  }
});

async function init() {
  state.cfg = await initChrome("call");
  if (state.cfg && !state.cfg.sarvam_ready) {
    const offline = state.cfg.brain === "offline_demo";
    $("key-banner-text").innerHTML = offline
      ? "<b>No model key yet.</b> Typed calls run on an offline keyword demo brain (simple English/Hinglish only). Put <span class=\"mono\">ANTHROPIC_API_KEY=…</span> in <span class=\"mono\">.env</span> and restart for the real agent."
      : `<b>Browser voice is off</b> (it needs a Sarvam key). Typed calls run the real agent on ${esc(state.cfg.brain_model)}; phone calls go through Bolna.`;
    $("key-banner").classList.remove("hidden");
    state.mode = "text";
  }
  state.customers = await api("/api/customers");
  state.customerId = state.customers[0]?.id;
  state.lang = state.customers[0]?.language || "hi";
  renderCustomers();
  seg("lang", state.lang, (v) => (state.lang = v));
  seg("mode", state.mode, (v) => {
    if (v === "voice" && state.cfg && !state.cfg.sarvam_ready) {
      seg("mode", "text", () => {});
      setState("Browser voice needs the Sarvam key", "Add SARVAM_API_KEY (sk_…) to .env and restart, or call through Bolna. Type mode works now.");
      return;
    }
    state.mode = v;
    updateIdle();
  });
  buildMeter("m-you"); buildMeter("m-agent");
  updateIdle();
}

function updateIdle() {
  const text = state.mode === "text";
  $("call-state").textContent = text ? "Tap to start a typed call" : "Tap to call FixFirst";
  $("call-sub").textContent = text ? "Same agent and playbooks, without audio. Handy for testing." : "Allow the microphone when asked. Use headphones for the best experience.";
  $("meters").classList.toggle("hidden", text);
  $("mute-btn").classList.toggle("hidden", text);
}

// ---- call lifecycle --------------------------------------------------------------------
$("call-btn").addEventListener("click", () => (state.live ? endCall() : startCall()));

async function startCall() {
  resetView();
  setLive(true);
  setState("Connecting…", "");
  try {
    const res = await api("/api/sessions", { customer_id: state.customerId, mode: state.mode, language: state.lang });
    state.sessionId = res.session_id;
    connectEvents(res.session_id);
    if (state.mode === "text") {
      $("composer").classList.remove("hidden");
      setState("Typed call in progress", "Type what the customer says below.");
      $("text-input").focus();
    } else {
      await startVoice(res.session_id);
    }
    state.startedAt = Date.now();
    state.timer = setInterval(tick, 1000);
  } catch (err) {
    console.error(err);
    setState("Couldn't start the call", err.message);
    setLive(false);
    cleanupMedia();
  }
}

async function endCall() {
  if (state.sessionId) {
    try { await api(`/api/sessions/${state.sessionId}/end`, {}); } catch (e) { console.warn(e); }
  }
  cleanupMedia();
  setLive(false);
  clearInterval(state.timer);
  setState("Call ended", "Tap the button to start a new call.");
}

function setLive(live) {
  state.live = live;
  const btn = $("call-btn");
  btn.classList.toggle("live", live);
  btn.setAttribute("aria-label", live ? "End call" : "Start call");
  $("call-icon").style.transform = live ? "rotate(135deg)" : "";
  $("mute-btn").disabled = !live || state.mode !== "voice";
  document.querySelectorAll(".cust-item, .seg button").forEach((b) => (b.disabled = live));
}

function setState(main, sub) {
  $("call-state").textContent = main;
  if (sub !== undefined) $("call-sub").textContent = sub;
}

function tick() {
  const s = Math.floor((Date.now() - state.startedAt) / 1000);
  $("call-sub").dataset.timer = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  if (state.mode === "voice" && state.live && state.pc) $("call-sub").textContent = `${$("call-sub").dataset.timer} · ${state.cfg?.models?.llm || ""}`;
}

// ---- voice (WebRTC to Pipecat SmallWebRTC) ------------------------------------------------
async function startVoice(sessionId) {
  const pc = new RTCPeerConnection({ iceServers: [{ urls: "stun:stun.l.google.com:19302" }] });
  state.pc = pc;
  state.localStream = await navigator.mediaDevices.getUserMedia({
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  // Pipecat expects an audio transceiver first, then video.
  const audioTx = pc.addTransceiver("audio", { direction: "sendrecv" });
  pc.addTransceiver("video", { direction: "sendrecv" });
  pc.createDataChannel("chat");   // Pipecat expects one; app events travel over our own websocket
  await audioTx.sender.replaceTrack(state.localStream.getAudioTracks()[0]);
  pc.ontrack = (e) => {
    if (e.track.kind !== "audio") return;
    const stream = new MediaStream([e.track]);
    $("remote-audio").srcObject = stream;
    watchLevel(stream, "m-agent", (speaking) => $("call-btn").classList.toggle("speaking", speaking));
  };
  pc.onconnectionstatechange = () => {
    if (["failed", "disconnected"].includes(pc.connectionState) && state.live) {
      setState("Connection lost", "The call dropped. Tap to call again.");
      endCall();
    }
  };
  watchLevel(state.localStream, "m-you");
  await pc.setLocalDescription(await pc.createOffer());
  await iceGathered(pc);
  const answer = await api("/api/offer", {
    sdp: pc.localDescription.sdp, type: pc.localDescription.type, requestData: { session_id: sessionId },
  });
  await pc.setRemoteDescription(answer);
  setState("Connected", "Say hello once you hear the agent.");
}

function iceGathered(pc) {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => { pc.removeEventListener("icegatheringstatechange", check); resolve(); };
    const check = () => pc.iceGatheringState === "complete" && done();
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(done, 2500);
  });
}

$("mute-btn").addEventListener("click", () => {
  if (!state.localStream) return;
  state.muted = !state.muted;
  state.localStream.getAudioTracks().forEach((t) => (t.enabled = !state.muted));
  $("mute-btn").textContent = state.muted ? "🎙 Unmute" : "🔇 Mute";
});

function cleanupMedia() {
  state.pc?.close(); state.pc = null;
  state.localStream?.getTracks().forEach((t) => t.stop()); state.localStream = null;
  state.meters.forEach((m) => cancelAnimationFrame(m)); state.meters = [];
  $("call-btn").classList.remove("speaking");
  $("composer").classList.add("hidden");
  state.ws?.close(); state.ws = null;
}

// ---- level meters ------------------------------------------------------------------------
function buildMeter(id) { $(id).innerHTML = "<span></span>".repeat(7); }

function watchLevel(stream, meterId, onSpeaking) {
  state.audioCtx = state.audioCtx || new AudioContext();
  const src = state.audioCtx.createMediaStreamSource(stream);
  const analyser = state.audioCtx.createAnalyser();
  analyser.fftSize = 256;
  src.connect(analyser);
  const data = new Uint8Array(analyser.frequencyBinCount);
  const bars = [...$(meterId).children];
  let quietFrames = 0;
  const loop = () => {
    analyser.getByteFrequencyData(data);
    const avg = data.slice(2, 40).reduce((a, b) => a + b, 0) / 38;
    bars.forEach((b, i) => (b.style.height = `${Math.max(6, Math.min(34, avg * (0.5 + Math.sin(i * 1.3 + Date.now() / 140) * 0.25 + 0.5) / 3))}px`));
    if (onSpeaking) {
      quietFrames = avg > 12 ? 0 : quietFrames + 1;
      onSpeaking(quietFrames < 12);
    }
    state.meters.push(requestAnimationFrame(loop));
  };
  loop();
}

// ---- events from the server --------------------------------------------------------------
function connectEvents(sessionId) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/api/sessions/${sessionId}/events`);
  state.ws = ws;
  ws.onmessage = (e) => handleEvent(JSON.parse(e.data));
}

function handleEvent(ev) {
  switch (ev.type) {
    case "agent":
      addMsg("agent", ev.text, ev.latency);
      if (state.live && !state.closed) setState(state.mode === "voice" ? "Agent speaking" : "Your turn", undefined);
      break;
    case "user": clearPartial(); addMsg("user", ev.text); if (state.live) setState("Thinking…", undefined); break;
    case "user_partial": showPartial(ev.text); if (state.live) setState("Listening…", undefined); break;
    case "step": showStep(ev); break;
    case "playbook": $("step-title").textContent = ev.title?.en || ev.playbook; break;
    case "ticket": break;
    case "safety": sys(`Safety: ${ev.label}`, true); break;
    case "outcome":
      state.closed = true;
      setState({ resolved: "Fixed on the call", escalated: "Technician booked", safety: "Safety alert raised", followup: "Follow-up scheduled" }[ev.outcome] || "Call wrapped up", "You can keep talking, or end the call.");
      showOutcome(ev.outcome);
      break;
    case "error_code": sys(`Error code ${ev.code}${ev.meaning ? ` · ${ev.meaning}` : " · recorded for technician"}`); break;
    case "guard": sys("Guard blocked an improvised instruction", true); break;
    case "latency": showLatency(ev); break;
    case "interrupted": sys("Caller interrupted"); break;
    case "photo": addPhoto(ev.path); break;
    case "note": sys(`Added to ticket: ${ev.note}`); break;
    case "error": sys(ev.message, true); break;
    case "connected": setState("Connected", "Say hello once you hear the agent."); break;
    case "ended": break;
  }
}

function addMsg(role, text, latency) {
  const box = $("transcript");
  box.querySelector(".sys:only-child")?.remove();
  const el = document.createElement("div");
  el.className = `msg ${role}`;
  el.innerHTML = esc(text) + (latency ? `<div class="msg-meta">agent ${ms(latency.brain_ms)}${latency.voice_to_voice_ms ? ` · voice-to-voice ${ms(latency.voice_to_voice_ms)}` : ""}</div>` : "");
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function sys(text, bad) {
  const el = document.createElement("div");
  el.className = `sys ${bad ? "bad" : ""}`;
  el.textContent = text;
  $("transcript").appendChild(el);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function showPartial(text) {
  if (!state.partialEl) {
    state.partialEl = document.createElement("div");
    state.partialEl.className = "msg user partial";
    $("transcript").appendChild(state.partialEl);
  }
  state.partialEl.textContent = text;
  $("transcript").scrollTop = $("transcript").scrollHeight;
}
function clearPartial() { state.partialEl?.remove(); state.partialEl = null; }

function showStep(ev) {
  state.steps = ev.number || state.steps + 1;
  $("step-text").classList.remove("step-empty");
  $("step-text").textContent = ev.text;
  $("step-count").textContent = `Step ${state.steps} · ${ev.id}`;
  $("progress").innerHTML = Array.from({ length: state.steps }, (_, i) => `<span class="${i === state.steps - 1 ? "cur" : ""}"></span>`).join("");
}

function showLatency(ev) {
  if (ev.voice_to_voice_ms) $("lat-chip").textContent = `last turn ${ms(ev.voice_to_voice_ms)} voice-to-voice`;
  const last = [...document.querySelectorAll(".msg.agent .msg-meta")].pop();
  if (last && ev.voice_to_voice_ms) last.textContent = `agent ${ms(ev.brain_ms)} · voice-to-voice ${ms(ev.voice_to_voice_ms)}${ev.stt_ms ? ` · stt ${ms(ev.stt_ms)}` : ""}${ev.tts_ms ? ` · tts ${ms(ev.tts_ms)}` : ""}`;
}

async function showOutcome(outcome) {
  const snap = await api(`/api/sessions/${state.sessionId}`);
  const t = snap.ticket;
  const nums = state.cfg?.tenant?.emergency_numbers || {};
  const blocks = {
    resolved: ["ok", "✅ Fixed on the call", `No technician visit needed. A WhatsApp check is scheduled for tomorrow.${snap.resolution && snap.resolution !== "fixed_on_call" ? `\nResult: ${snap.resolution.replaceAll("_", " ")}` : ""}`],
    escalated: ["info", `🧰 Technician booked · ${t?.code || ""}`, t ? `Likely cause: ${t.summary.likely_cause}\nSteps tried: ${t.summary.steps_tried.map((s) => s.answer).join(" → ") || "none"}\nWhatsApp sent to the customer (with photo link) and the service team.` : ""],
    safety: ["bad", `⚠️ Safety alert · ${t?.code || ""}`, `${t?.summary?.likely_cause || ""}\nUrgent callback promised · Emergency ${nums.national || "112"} · LPG ${nums.lpg || "1906"}`],
    followup: ["warn", "⏳ Follow-up scheduled", "The customer will get a WhatsApp check-in and can reply to book a technician."],
  };
  const b = blocks[outcome];
  if (!b) return;
  $("outcome").innerHTML = `<div class="outcome ${b[0]}"><h3>${esc(b[1])}</h3><div class="detail">${esc(b[2])}</div>${t ? `<div class="small"><a href="/ops#ticket-${esc(t.code)}">Open in ops →</a></div>` : ""}</div>`;
}

function resetView() {
  $("transcript").innerHTML = '<div class="sys">Starting…</div>';
  $("outcome").innerHTML = "";
  $("step-text").textContent = "The agent's current instruction shows here, word for word from the playbook.";
  $("step-text").classList.add("step-empty");
  $("step-title").textContent = "Current step";
  $("step-count").textContent = "";
  $("progress").innerHTML = "";
  $("photos").innerHTML = "";
  $("lat-chip").textContent = "optional · tap to hide";
  state.steps = 0;
  state.closed = false;
}

// ---- typed turns & photos ------------------------------------------------------------------
$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("text-input");
  const text = input.value.trim();
  if (!text || !state.sessionId) return;
  input.value = "";
  input.disabled = true;
  try {
    await api(`/api/sessions/${state.sessionId}/text`, { text });
  } catch (err) {
    sys(err.message, true);
  } finally {
    input.disabled = false;
    input.focus();
  }
});

$("photo-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file) return;
  if (!state.sessionId) { alert("Start a call first, then add a photo."); return; }
  const fd = new FormData();
  fd.append("file", file);
  try { await api(`/api/sessions/${state.sessionId}/photos`, fd); } catch (err) { sys(err.message, true); }
});

function addPhoto(path) {
  $("photos").insertAdjacentHTML("beforeend", `<img src="/media/${esc(path)}" alt="Customer photo">`);
}

init();
