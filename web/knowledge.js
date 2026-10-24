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
      <div><h2 style="font-size:18px">${esc(pb.title.en)}</h2><div class="muted small">${esc(pb.title.hi)} · <span class="mono">knowledge/playbooks/${esc(pb.appliance)}.yaml</span></div></div>
      <span class="badge ${pb.status === "reviewed" ? "ok" : "warn"}">${pb.status === "reviewed" ? `reviewed by ${esc(pb.reviewed_by || "")}` : "draft · unreviewed"}</span>
    </div>
    ${Object.keys(pb.routes || {}).length ? `<p class="small muted">Error-code routes: ${Object.entries(pb.routes).map(([r, n]) => `<span class="mono">${esc(r)}</span> → <a href="#n-${esc(n)}">${esc(n)}</a>`).join(", ")}</p>` : ""}
    <div style="margin-top:12px">${order.map(nodeCard).join("")}</div>`;
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
