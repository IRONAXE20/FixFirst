import { api, esc, ms, timeAgo, outcomeBadge, initChrome, LOGO, APPLIANCE_ICON } from "/static/common.js";

const $ = (id) => document.getElementById(id);
$("logo").innerHTML = LOGO;
const state = { tab: "tickets", open: null, data: { tickets: [], calls: [], outbox: [] } };
let STATES = ["NEW", "SELF_FIX_IN_PROGRESS", "RESOLVED_REMOTE", "TICKET_RAISED", "URGENT", "SCHEDULED", "IN_PROGRESS",
  "FIXED", "REPLACED", "AUDIT_REVIEW", "CLOSED", "REOPENED"];
api("/api/ticket-states").then((s) => { STATES = s; }).catch(() => {});

const hash = location.hash.slice(1);
if (hash.startsWith("call-")) { state.tab = "calls"; state.open = hash.slice(5); }
if (hash.startsWith("ticket-")) { state.tab = "tickets"; state.open = hash.slice(7); }

$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-tab]");
  if (!b) return;
  state.tab = b.dataset.tab;
  state.open = null;
  render();
});

async function refresh() {
  const [stats, tickets, calls, outbox] = await Promise.all([
    api("/api/stats"), api("/api/tickets"), api("/api/calls"), api("/api/outbox"),
  ]);
  state.data = { tickets, calls, outbox };
  renderKpis(stats);
  render();
  $("refreshed").textContent = `updated ${new Date().toLocaleTimeString()}`;
}

function renderKpis(s) {
  const pct = s.resolved_rate == null ? "–" : `${Math.round(s.resolved_rate * 100)}%`;
  const kpis = [
    ["Calls", s.calls, `${s.abandoned} dropped`],
    ["Fixed on call", pct, `${s.resolved} of ${s.resolved + s.escalated} decided`],
    ["Technician tickets", s.escalated, `${s.open_tickets} open`],
    ["Safety alerts", s.safety, "urgent callbacks"],
    ["Voice-to-voice", ms(s.median_voice_to_voice_ms), `median · agent ${ms(s.median_brain_ms)}`],
    ["Visits saved", s.estimated_savings_inr ? `₹${s.estimated_savings_inr.toLocaleString("en-IN")}` : s.resolved, s.visit_cost_inr ? `at ₹${s.visit_cost_inr}/visit` : "set visit cost in tenant.yaml"],
  ];
  $("kpis").innerHTML = kpis.map(([l, v, sub]) => `<div class="card kpi"><div class="kpi-label">${esc(l)}</div><div class="kpi-value">${esc(v)}</div><div class="kpi-sub">${esc(sub)}</div></div>`).join("");
}

function render() {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === state.tab));
  if (state.tab === "tickets") renderTickets();
  if (state.tab === "calls") renderCalls();
  if (state.tab === "outbox") renderOutbox();
}

function priorityBadge(t) {
  const audit = t.audit_required ? ` <span class="badge warn" title="Possible misuse: Auditing team">Audit</span>` : "";
  if (t.priority === "urgent") return `<span class="badge bad">URGENT</span>${audit}`;
  const label = { system: "System", selffix: "Self-fix", handoff: "Handoff" }[t.category] || "Normal";
  return `<span class="badge ${t.category === "system" ? "warn" : "info"}">${label}</span>${audit}`;
}

function renderTickets() {
  const rows = state.data.tickets;
  if (!rows.length) { $("panel").innerHTML = `<div class="empty">No tickets yet. Make a call from the Call page.</div>`; return; }
  $("panel").innerHTML = `<table class="table"><thead><tr><th>Ticket</th><th>Priority</th><th>Customer</th><th>Appliance</th><th>Likely cause</th><th>Status</th><th>Created</th></tr></thead><tbody>
    ${rows.map((t) => {
      const s = t.summary;
      const open = state.open === t.code;
      return `<tr class="row" data-code="${esc(t.code)}"><td><b>${esc(t.code)}</b></td><td>${priorityBadge(t)}</td>
        <td>${esc(s.customer?.name || "Unknown")}<div class="muted small">${esc(s.customer?.area || s.customer?.phone || "")}</div></td>
        <td>${APPLIANCE_ICON[s.appliance] || ""} ${esc(s.appliance_label || s.appliance || "–")}${s.brand ? `<div class="muted small">${esc(s.brand)}</div>` : ""}</td>
        <td>${esc(s.likely_cause)}</td>
        <td><select class="status" data-code="${esc(t.code)}">${STATES.map((v) => `<option ${v === t.status ? "selected" : ""}>${v}</option>`).join("")}</select>
          <div class="muted small">${esc(t.origin_channel || "voice")} · ${esc((t.channels_used || "voice_only").replace("_", " "))}</div></td>
        <td class="muted small">${timeAgo(t.created_at)}</td></tr>
        ${open ? `<tr class="detail-row"><td colspan="7">${ticketDetail(t)}</td></tr>` : ""}`;
    }).join("")}</tbody></table>`;
}

function ticketDetail(t) {
  const s = t.summary;
  const wa = state.data.outbox.find((m) => m.ticket_code === t.code && m.audience === "team");
  const code = s.error_code ? `${s.error_code.code}${s.error_code.meaning ? ` = ${s.error_code.meaning}` : " (recorded, not interpreted)"}` : "–";
  return `<div class="detail">
    <div>
      <h3>Technician handoff</h3>
      <dl class="kv">
        <dt>Problem</dt><dd>${esc(s.problem || "–")}</dd>
        <dt>Error code</dt><dd>${esc(code)}</dd>
        <dt>Likely cause</dt><dd>${esc(s.likely_cause)}</dd>
        <dt>Bring / check</dt><dd>${esc(s.technician_note || "–")}</dd>
        ${s.hazard ? `<dt>Hazard</dt><dd><span class="badge bad">${esc(s.hazard)}</span></dd>` : ""}
        ${s.reason ? `<dt>Caller said</dt><dd>${esc(s.reason)}</dd>` : ""}
        ${(s.notes || []).map((n) => `<dt>Also</dt><dd>${esc(n)}</dd>`).join("")}
        ${(s.customer_notes || []).map((n) => `<dt>Customer wrote</dt><dd>${esc(n.note)}</dd>`).join("")}
        ${s.sla_hours ? `<dt>SLA</dt><dd>agree a visit within ${esc(s.sla_hours)} h</dd>` : ""}
        ${(s.reopened || []).length ? `<dt>Reopened</dt><dd>${s.reopened.length}× · ${esc(s.reopened.at(-1).likely_cause)}</dd>` : ""}
        <dt>Address</dt><dd>${esc(s.customer?.address || "–")} · ${esc(s.customer?.phone || "")}</dd>
        <dt>Playbook</dt><dd>${esc(s.playbook || "–")} ${s.playbook_status && s.playbook_status !== "reviewed" ? `<span class="badge warn">draft</span>` : ""}</dd>
      </dl>
      <h3 style="margin-top:12px">Steps tried on the call</h3>
      ${s.steps_tried?.length ? `<ol class="steps">${s.steps_tried.map((x) => `<li>${esc(x.answer)} <span class="muted small">(${esc(x.node)})</span></li>`).join("")}</ol>` : `<div class="muted small">None</div>`}
      <h3 style="margin-top:12px">Photos</h3>
      ${t.photos?.length ? `<div class="thumbs">${t.photos.map((p) => `<a href="${esc(p)}" target="_blank"><img src="${esc(p)}" alt=""></a>`).join("")}</div>` : `<div class="muted small">None yet · <a href="${esc(t.photo_url)}" target="_blank">customer upload link</a></div>`}
    </div>
    <div>
      <h3>WhatsApp to the service team</h3>
      <div class="wa">${esc(wa?.body || "–")}</div>
      <div class="small" style="margin-top:8px"><a href="#call-${esc(t.call_id)}" data-goto-call="${esc(t.call_id)}">Open call transcript →</a></div>
    </div></div>`;
}

function renderCalls() {
  const rows = state.data.calls;
  if (!rows.length) { $("panel").innerHTML = `<div class="empty">No calls yet.</div>`; return; }
  $("panel").innerHTML = `<table class="table"><thead><tr><th>When</th><th>Customer</th><th>Channel</th><th>Outcome</th><th>Playbook</th><th>Turns</th><th>Voice-to-voice</th><th>Ticket</th></tr></thead><tbody>
    ${rows.map((c) => {
      const lat = (c.latency || []).map((t) => t.voice_to_voice_ms).filter(Boolean);
      const med = lat.length ? lat.sort((a, b) => a - b)[Math.floor(lat.length / 2)] : null;
      const open = state.open === c.id;
      return `<tr class="row" data-call="${esc(c.id)}"><td class="small">${timeAgo(c.started_at)}</td><td>${esc(c.customer_name || "Unknown caller")}</td>
        <td><span class="badge neutral">${esc(c.channel)}</span></td><td>${outcomeBadge(c.outcome)}${c.hazard ? ` <span class="badge bad">${esc(c.hazard)}</span>` : ""}</td>
        <td class="small">${esc(c.playbook_id || "–")}</td><td>${(c.transcript || []).filter((t) => t.role === "user").length}</td>
        <td>${ms(med)}</td><td>${c.ticket_code ? `<a href="#ticket-${esc(c.ticket_code)}" data-goto-ticket="${esc(c.ticket_code)}">${esc(c.ticket_code)}</a>` : "–"}</td></tr>
        ${open ? `<tr class="detail-row"><td colspan="8">${callDetail(c)}</td></tr>` : ""}`;
    }).join("")}</tbody></table>`;
}

function callDetail(c) {
  const lat = c.latency || [];
  return `<div class="detail">
    <div><h3>Transcript</h3><div class="tx">${(c.transcript || []).map((t) => `<div class="${t.role === "agent" ? "a" : "u"}"><b>${t.role === "agent" ? "Agent" : "Caller"}:</b> ${esc(t.text)}</div>`).join("") || "<span class='muted'>Empty</span>"}</div></div>
    <div><h3>Playbook path</h3>${(c.path || []).length ? `<ol class="steps">${c.path.map((p) => `<li><span class="mono">${esc(p.node)}</span> → ${esc(p.label)}</li>`).join("")}</ol>` : "<div class='muted small'>No playbook steps</div>"}
      <h3 style="margin-top:12px">Latency per turn</h3>
      ${lat.length ? `<table class="table"><tr><th>#</th><th>Agent</th><th>LLM</th><th>STT</th><th>TTS</th><th>Voice-to-voice</th></tr>${lat.map((t) => `<tr><td>${t.turn}</td><td>${ms(t.brain_ms)}</td><td>${ms(t.llm_ms)}</td><td>${ms(t.stt_ms)}</td><td>${ms(t.tts_ms)}</td><td><b>${ms(t.voice_to_voice_ms)}</b></td></tr>`).join("")}</table>` : "<div class='muted small'>–</div>"}
      ${c.error_code ? `<h3 style="margin-top:12px">Error code</h3><div>${esc(c.error_code.code)} ${esc(c.error_code.brand || "")}</div>` : ""}
    </div></div>`;
}

function renderOutbox() {
  const rows = state.data.outbox;
  if (!rows.length) { $("panel").innerHTML = `<div class="empty">No messages yet.</div>`; return; }
  const cls = { sent: "ok", dry_run: "neutral", failed: "bad", no_recipient: "warn" };
  $("panel").innerHTML = `<table class="table"><thead><tr><th>When</th><th>To</th><th>Audience</th><th>Kind</th><th>Status</th><th>Message</th></tr></thead><tbody>
    ${rows.map((m) => `<tr><td class="small">${timeAgo(m.created_at)}</td><td class="small">${esc(m.to || "–")}</td><td>${esc(m.audience)}</td><td class="small">${esc(m.kind)}</td>
      <td><span class="badge ${cls[m.status] || "neutral"}">${esc(m.status.replace("_", " "))}</span>${m.error ? `<div class="small muted">${esc(m.error)}</div>` : ""}</td>
      <td><div class="wa">${esc(m.body)}</div></td></tr>`).join("")}</tbody></table>`;
}

$("panel").addEventListener("click", (e) => {
  if (e.target.closest("select")) return;
  const gotoCall = e.target.closest("[data-goto-call]");
  const gotoTicket = e.target.closest("[data-goto-ticket]");
  if (gotoCall) { e.preventDefault(); state.tab = "calls"; state.open = gotoCall.dataset.gotoCall; render(); return; }
  if (gotoTicket) { e.preventDefault(); e.stopPropagation(); state.tab = "tickets"; state.open = gotoTicket.dataset.gotoTicket; render(); return; }
  if (e.target.closest("a")) return;
  const tr = e.target.closest("tr.row");
  if (!tr) return;
  const id = tr.dataset.code || tr.dataset.call;
  state.open = state.open === id ? null : id;
  render();
});

$("panel").addEventListener("change", async (e) => {
  const sel = e.target.closest("select.status");
  if (!sel) return;
  await api(`/api/tickets/${sel.dataset.code}`, { status: sel.value }, "PATCH");
  refresh();
});

initChrome("ops");
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
