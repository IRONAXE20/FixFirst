import { api, esc, initChrome, LOGO, APPLIANCE_ICON } from "/static/common.js";

const $ = (id) => document.getElementById(id);
$("logo").innerHTML = LOGO;
let sources = {};
let playbooks = [];
let current = null;

const KIND = { resolve: ["ok", "Fixed"], escalate: ["info", "Technician"], followup: ["warn", "Follow-up"] };

async function init() {
  await initChrome("knowledge");
  [playbooks, sources] = await Promise.all([api("/api/playbooks"), api("/api/sources")]);
  $("pb-list").innerHTML = playbooks.map((p) => `
    <button class="pb-item" data-id="${esc(p.id)}"><div class="t">${APPLIANCE_ICON[p.appliance] || ""} ${esc(p.title.en)}</div>
    <div class="muted small">${p.steps} steps · <span class="badge ${p.status === "reviewed" ? "ok" : "warn"}">${p.status === "reviewed" ? "reviewed" : "draft · needs review"}</span></div></button>`).join("");
  showPlaybook(playbooks[0].id);
}

$("pb-list").addEventListener("click", (e) => {
  const b = e.target.closest(".pb-item");
  if (b) showPlaybook(b.dataset.id);
});
$("show-safety").addEventListener("click", showSafety);
$("show-docs").addEventListener("click", showDocs);

function selectItem(id) {
  document.querySelectorAll(".pb-item").forEach((b) => b.classList.toggle("on", b.dataset.id === id));
}

function sourceLinks(ids) {
  if (!ids?.length) return "";
  return `<div class="srcs">Source: ${ids.map((id) => {
    const s = sources[id];
    if (!s) return esc(id);
    const label = `${esc(s.publisher)}${s.official ? "" : " (internal)"}`;
    return s.url.startsWith("http") ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${label}</a>` : label;
  }).join(" · ")}</div>`;
}

async function showPlaybook(id) {
  selectItem(id);
  const pb = await api(`/api/playbooks/${id}`);
  current = pb;
  const order = orderNodes(pb);
  $("main").innerHTML = `
    <div class="row" style="justify-content:space-between; flex-wrap:wrap">
      <div><h2 style="font-size:18px">${esc(pb.title.en)}</h2><div class="muted small">${esc(pb.title.hi)} · <span class="mono">${esc(pb.id)}</span>${listed(id)?.version ? ` · version ${listed(id).version}` : ""}</div></div>
      <div class="row"><span class="badge ${pb.status === "reviewed" ? "ok" : "warn"}">${pb.status === "reviewed" ? `reviewed by ${esc(pb.reviewed_by || "")}` : "draft · unreviewed"}</span>
        <button class="btn" id="edit-btn">✏️ Edit</button></div>
    </div>
    ${Object.keys(pb.routes || {}).length ? `<p class="small muted">Error-code routes: ${Object.entries(pb.routes).map(([r, n]) => `<span class="mono">${esc(r)}</span> → <a href="#n-${esc(n)}">${esc(n)}</a>`).join(", ")}</p>` : ""}
    <div style="margin-top:12px">${order.map(nodeCard).join("")}</div>`;
  $("edit-btn").addEventListener("click", () => showEditor(id));
}

const listed = (id) => playbooks.find((p) => p.id === id);
const store = {
  get: (k) => { try { return localStorage.getItem(k) || ""; } catch { return ""; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

async function showEditor(id) {
  selectItem(id);
  const src = await api(`/api/playbooks/${id}/source`);
  $("main").innerHTML = `
    <div class="row" style="justify-content:space-between; flex-wrap:wrap">
      <div><h2 style="font-size:18px">Edit ${esc(id)}</h2><div class="muted small">Editing version ${src.version}. Spoken text is in <span class="mono">say.hi</span> / <span class="mono">say.en</span>; answers in <span class="mono">outcomes</span>.</div></div>
      <button class="btn ghost" id="back-btn">← Back to steps</button>
    </div>
    ${src.editing ? "" : `<div class="banner warn" style="margin-top:10px"><span>⚠️</span><span>Editing is switched off on this server (no <span class="mono">ADMIN_KEY</span> set). You can still validate changes.</span></div>`}
    <div class="edit-bar">
      <input id="ed-by" placeholder="Your name" value="${esc(store.get("ff-editor-name"))}">
      <input id="ed-key" type="password" placeholder="Admin key" value="${esc(store.get("ff-admin-key"))}">
      <button class="btn" id="ed-validate">Check</button>
      <button class="btn primary" id="ed-save">Save as version ${src.version + 1}</button>
      <span id="ed-msg"></span>
    </div>
    <ul class="errs" id="ed-errs"></ul>
    <textarea class="editor" id="ed-text" spellcheck="false">${esc(src.yaml)}</textarea>
    <h2 style="margin-top:16px">History</h2>
    <table class="table"><tr><th>Version</th><th>Saved</th><th>By</th></tr>
      ${src.history.map((h) => `<tr><td>${h.version}</td><td>${esc(new Date(h.saved_at + "Z").toLocaleString())}</td><td>${esc(h.saved_by || "")}</td></tr>`).join("")}</table>`;
  const show = (ok, msg, errors = []) => {
    $("ed-msg").innerHTML = msg ? `<span class="${ok ? "ok-msg" : "muted small"}">${esc(msg)}</span>` : "";
    $("ed-errs").innerHTML = errors.map((e) => `<li>${esc(e)}</li>`).join("");
  };
  $("back-btn").addEventListener("click", () => showPlaybook(id));
  $("ed-validate").addEventListener("click", async () => {
    const r = await api(`/api/playbooks/${id}/validate`, { yaml: $("ed-text").value });
    r.ok ? show(true, `Looks good: ${r.steps} steps, nothing broken.`) : show(false, "Not saved. Fix these first:", r.errors);
  });
  $("ed-save").addEventListener("click", async () => {
    store.set("ff-editor-name", $("ed-by").value);
    store.set("ff-admin-key", $("ed-key").value);
    const res = await fetch(`/api/playbooks/${id}`, {
      method: "PUT", headers: { "Content-Type": "application/json", "X-Admin-Key": $("ed-key").value },
      body: JSON.stringify({ yaml: $("ed-text").value, by: $("ed-by").value, base_version: src.version }),
    });
    const data = await res.json().catch(() => ({}));
    if (res.ok) {
      playbooks = await api("/api/playbooks");
      show(true, `Saved as version ${data.version}. Calls use it from now on.`);
      setTimeout(() => showPlaybook(id), 900);
    } else {
      show(false, data.detail || "Not saved. Fix these first:", data.errors || []);
    }
  });
}

function orderNodes(pb) {
  const byId = Object.fromEntries(pb.nodes.map((n) => [n.id, n]));
  const seen = new Set();
  const out = [];
  const queue = [pb.entry, ...Object.values(pb.routes || {})];
  while (queue.length) {
    const id = queue.shift();
    if (seen.has(id) || !byId[id]) continue;
    seen.add(id);
    out.push(byId[id]);
    byId[id].outcomes.forEach((o) => o.next && queue.push(o.next));
  }
  return out.concat(pb.nodes.filter((n) => !seen.has(n.id)));
}

function nodeCard(n) {
  const kind = KIND[n.kind];
  return `<div class="node" id="n-${esc(n.id)}">
    <div class="node-head"><span class="node-id">${esc(n.id)}</span>${kind ? `<span class="badge ${kind[0]}">${kind[1]}</span>` : ""}
      ${n.followup_minutes ? `<span class="muted small">WhatsApp check-in after ${n.followup_minutes} min</span>` : ""}</div>
    ${n.say?.en ? `<div class="say"><div><span class="lang">EN</span>${esc(n.say.en)}</div><div><span class="lang">HI</span>${esc(n.say.hi)}</div></div>` : ""}
    ${n.help?.en ? `<div class="say muted"><div><span class="lang">IF ASKED</span>${esc(n.help.en)}</div></div>` : ""}
    ${n.outcomes.length ? `<div class="outs">${n.outcomes.map((o) => o.hazard
      ? `<span class="out hz">${esc(o.label)} → safety: ${esc(o.hazard)}</span>`
      : `<span class="out">${esc(o.label)} → <a href="#n-${esc(o.next)}">${esc(o.next)}</a></span>`).join("")}</div>` : ""}
    ${n.likely_cause ? `<div class="term"><b>Likely cause:</b> ${esc(n.likely_cause)}${n.technician_note ? ` · <b>Technician:</b> ${esc(n.technician_note)}` : ""}</div>` : ""}
    ${sourceLinks(n.sources)}
  </div>`;
}

async function showSafety() {
  selectItem(null);
  const scripts = await api("/api/safety");
  $("main").innerHTML = `<h2 style="font-size:18px">Safety scripts</h2>
    <p class="small muted">Read word for word when the hazard screen or the model flags danger. An urgent ticket is created and the on-call team is notified. Source: <span class="mono">knowledge/safety/hazards.yaml</span> (draft, needs technician and legal sign-off).</p>
    ${Object.entries(scripts).map(([kind, s]) => `<div class="node"><div class="node-head"><span class="badge bad">${esc(kind)}</span><b>${esc(s.label)}</b></div>
      <div class="say"><div><span class="lang">EN</span>${esc(s.script.en)}</div><div><span class="lang">HI</span>${esc(s.script.hi)}</div></div></div>`).join("")}`;
}

async function showDocs() {
  selectItem(null);
  const files = await api("/api/docs");
  $("main").innerHTML = `<h2 style="font-size:18px">Document library</h2>
    <p class="small muted">Manuals and service docs. Playbook steps cite these; upload RentBasket's SOPs and model manuals here (PDF, Markdown or text). Search is BM25 over the chunks.</p>
    <div class="row" style="margin:12px 0"><input type="search" id="q" placeholder="Search the manuals, e.g. 'drain filter' or '4C'"><label class="btn">⬆ Upload<input type="file" id="up" accept=".pdf,.md,.txt" hidden></label></div>
    <div id="hits"></div>
    <h2 style="margin-top:18px">Files (${files.length})</h2>
    <table class="table"><tr><th>File</th><th>Title</th><th>Chunks</th><th>Source</th></tr>
    ${files.map((f) => `<tr><td class="mono">${esc(f.name)}</td><td>${esc(f.title)}${f.uploaded ? ` <span class="badge info">uploaded</span>` : ""}</td><td>${f.chunks}</td><td>${f.url ? `<a href="${esc(f.url)}" target="_blank" rel="noopener">link</a>` : "–"}</td></tr>`).join("")}</table>`;
  let t;
  $("q").addEventListener("input", (e) => { clearTimeout(t); t = setTimeout(() => search(e.target.value), 250); });
  $("up").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const fd = new FormData();
    fd.append("file", file);
    try { await api("/api/docs", fd); showDocs(); } catch (err) { alert(err.message); }
  });
}

async function search(q) {
  if (!q.trim()) { $("hits").innerHTML = ""; return; }
  const hits = await api(`/api/docs/search?q=${encodeURIComponent(q)}`);
  $("hits").innerHTML = hits.length ? `<div class="card card-pad">${hits.map((h) => `<div class="doc-hit"><b>${esc(h.title)}</b> <span class="muted small">· ${esc(h.location)} · score ${h.score}</span>
    ${h.url ? ` <a class="small" href="${esc(h.url)}" target="_blank" rel="noopener">source</a>` : ""}<p>${esc(h.text)}</p></div>`).join("")}</div>` : `<div class="muted small">No matches</div>`;
}

init();
