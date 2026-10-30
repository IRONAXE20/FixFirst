import { api, esc, initChrome, LOGO, APPLIANCE_ICON } from "/static/common.js";
import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.esm.min.mjs";

// Each playbook as a flowchart, drawn from /api/playbooks/<id>: the same playbooks the agent reads on calls.
const $ = (id) => document.getElementById(id);
$("logo").innerHTML = LOGO;
let playbooks = [], safety = {}, current = null, lang = "en", seq = 0;

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const short = (s, n) => { s = (s || "").replace(/\s+/g, " ").trim(); return s.length > n ? s.slice(0, n - 1) + "…" : s; };
// Mermaid label: quoted, so only quotes and angle brackets need escaping.
const label = (s) => `"${String(s).replace(/"/g, "#quot;").replace(/</g, "#lt;").replace(/>/g, "#gt;")}"`;
// "AC-01 Not cooling (not cooling, thanda nahi…)" → "AC-01 Not cooling"
const answer = (o) => short((o.label || o.id).split(" (")[0], 42);

function kindOf(n) {
  if (n.kind === "resolve") return ["done", "✅ Fixed on the call"];
  if (n.kind === "followup") return ["wa", n.whatsapp ? "📲 Steps on WhatsApp" : "⏳ Follow-up"];
  if (n.kind === "escalate" && n.handoff) return ["person", "🙋 Hand to a person"];
  if (n.kind === "escalate" && n.partial) return ["wa", "🎫 Ticket: partly fixed"];
  if (n.kind === "escalate") return ["ticket", "🎫 Ticket"];
  return ["ask", null];
}

function chart(pb) {
  const lines = ["flowchart TD"];
  const hazards = new Set();
  for (const n of pb.nodes) {
    const [cls, tag] = kindOf(n);
    const said = n.say?.[lang] || n.say?.en || "";
    let text;
    if (cls === "ask") text = short(said, 95);
    else if (n.kind === "escalate") text = `${tag}<br/>${short((n.likely_cause || "").split(":")[0].split(" (")[0], 60)}`;
    else text = `${tag}${said ? "<br/>" + short(said, 70) : ""}`;
    lines.push(`  n_${n.id}[${label(text)}]:::${cls}`);
  }
  for (const n of pb.nodes) {
    for (const o of n.outcomes) {
      const to = o.hazard ? `hz_${o.hazard}` : o.next ? `n_${o.next}` : null;
      if (!to) continue;
      if (o.hazard) hazards.add(o.hazard);
      lines.push(`  n_${n.id} -->|${label(answer(o))}| ${to}`);
    }
  }
  for (const h of hazards) lines.push(`  hz_${h}[${label("⚠️ Safety script: " + short(safety[h]?.label || h, 40))}]:::safety`);
  const style = (bg, line) => `fill:${bg},stroke:${line},color:${css("--ink")}`;
  lines.push(`  classDef ask ${style(css("--surface-2"), css("--line"))}`);
  lines.push(`  classDef done ${style(css("--ok-soft"), css("--ok"))}`);
  lines.push(`  classDef ticket ${style(css("--info-soft"), css("--info"))}`);
  lines.push(`  classDef wa ${style(css("--warn-soft"), css("--warn"))}`);
  lines.push(`  classDef person ${style(matchMedia("(prefers-color-scheme: dark)").matches ? "#2a2240" : "#ece4fb", "#7b5cc4")}`);
  lines.push(`  classDef safety ${style(css("--danger-soft"), css("--danger"))}`);
  lines.push(`  style n_${pb.entry} stroke-width:3px`);
  return lines.join("\n");
}

function detail(pb, id) {
  if (id.startsWith("hz_")) {
    const h = safety[id.slice(3)] || {};
    $("detail").innerHTML = `<h3>⚠️ ${esc(h.label || id.slice(3))}</h3><div class="mono">safety script</div>`
      + `<p>${esc(h.script?.[lang] || h.script?.en || "")}</p>`;
    return;
  }
  const n = pb.nodes.find((x) => x.id === id);
  if (!n) return;
  const [, tag] = kindOf(n);
  const say = ["en", "hi"].filter((l) => n.say?.[l]).map((l) => `<p><span class="lang">${l.toUpperCase()}</span>${esc(n.say[l])}</p>`).join("");
  const outs = n.outcomes.map((o) => `<li><b>${esc(o.label || o.id)}</b> → ${esc(o.hazard ? "safety script" : o.next || "end")}</li>`).join("");
  $("detail").innerHTML = `<h3>${esc(tag || "Agent asks")}</h3><div class="mono">${esc(n.id)}</div>${say}`
    + (n.help?.[lang] || n.help?.en ? `<p class="muted">If asked: ${esc(n.help[lang] || n.help.en)}</p>` : "")
    + (n.likely_cause ? `<p><b>Ticket says:</b> ${esc(n.likely_cause)}</p>` : "")
    + (n.technician_note ? `<p><b>Technician note:</b> ${esc(n.technician_note)}</p>` : "")
    + (outs ? `<div class="muted">Caller's answers</div><ul>${outs}</ul>` : "");
}

async function show(id) {
  current = id;
  document.querySelectorAll("#tabs .tab").forEach((b) => b.classList.toggle("on", b.dataset.id === id));
  const pb = await api(`/api/playbooks/${id}`);
  try {
    const { svg } = await mermaid.render(`flow${++seq}`, chart(pb));
    $("chart").innerHTML = svg;
  } catch (e) {
    $("chart").innerHTML = `<p class="muted">Couldn't draw this playbook: ${esc(e.message || e)}</p>`;
    return;
  }
  $("chart").querySelectorAll("g.node").forEach((g) => {
    const m = /^flowchart-(n_|hz_)(.+)-\d+$/.exec(g.id);
    if (m) g.addEventListener("click", () => detail(pb, m[1] === "hz_" ? `hz_${m[2]}` : m[2]));
  });
  $("detail").innerHTML = `<h3>${esc(pb.title?.[lang] || pb.title?.en || pb.id)}</h3><div class="mono">${esc(pb.id)} · ${esc(pb.status)}</div>`
    + `<p class="muted">The thick box is where the call starts. Click any box for its full wording.</p>`;
}

async function init() {
  await initChrome("flows");
  mermaid.initialize({ startOnLoad: false, securityLevel: "strict", theme: "base",
                       themeVariables: { fontFamily: css("--font"), lineColor: css("--muted"), fontSize: "14px" },
                       flowchart: { htmlLabels: true, curve: "basis", nodeSpacing: 30, rankSpacing: 46 } });
  [playbooks, safety] = await Promise.all([api("/api/playbooks"), api("/api/safety")]);
  $("tabs").innerHTML = playbooks.map((p) => `<button class="tab" data-id="${esc(p.id)}">${APPLIANCE_ICON[p.appliance] || ""} ${esc(p.appliance.replace("_", " "))}</button>`).join("");
  $("tabs").querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => show(b.dataset.id)));
  const wanted = new URLSearchParams(location.search).get("p");
  if (playbooks.length) show(playbooks.some((p) => p.id === wanted) ? wanted : playbooks[0].id);
}

$("lang").addEventListener("click", () => {
  lang = lang === "en" ? "hi" : "en";
  $("lang").textContent = lang === "en" ? "हिंदी" : "English";
  if (current) show(current);
});

init();
