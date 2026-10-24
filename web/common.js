// Shared helpers for all FixFirst pages.
export async function api(path, body, method) {
  const opts = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const res = await fetch(path, opts);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `${res.status} ${res.statusText}`);
  return data;
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function timeAgo(iso) {
  if (!iso) return "";
  const d = new Date(iso.endsWith("Z") ? iso : iso + "Z");
  const s = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return d.toLocaleDateString();
}

export function ms(v) {
  if (v == null) return "–";
  return v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${v} ms`;
}

const OUTCOME = {
  resolved: ["ok", "Fixed on call"], escalated: ["info", "Technician booked"], safety: ["bad", "Safety alert"],
  followup: ["warn", "Follow-up"], abandoned: ["neutral", "Dropped"], in_progress: ["neutral", "In progress"],
};
export function outcomeBadge(outcome) {
  const [cls, label] = OUTCOME[outcome] || ["neutral", outcome];
  return `<span class="badge ${cls}">${esc(label)}</span>`;
}

export const APPLIANCE_ICON = {
  ac: "❄️", washer: "🧺", washer_semi: "🧺", fridge: "🧊", geyser: "🚿", tv: "📺", microwave: "♨️", inverter: "🔋", stove: "🔥",
  water_purifier: "💧", induction: "🍳", furniture: "🛋️",
};

export async function initChrome(active) {
  document.querySelectorAll(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === active));
  try {
    const cfg = await api("/api/config");
    document.querySelectorAll("[data-tenant]").forEach((el) => (el.textContent = `for ${cfg.tenant.name}`));
    const chip = document.getElementById("status-chip");
    if (chip) {
      const brainOk = cfg.brain && cfg.brain !== "offline_demo";
      chip.className = `chip ${brainOk ? "ok" : "warn"}`;
      chip.innerHTML = `<span class="dot"></span>${brainOk ? `Brain: ${esc(cfg.brain_model)}` : "No model key"} · Voice ${cfg.sarvam_ready ? "on" : "off"} · WhatsApp ${cfg.whatsapp_mode === "live" ? "live" : "dry run"}`;
    }
    return cfg;
  } catch (e) {
    console.error(e);
    return null;
  }
}

export const LOGO = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.4L3 18l3 3 6.3-6.3a4 4 0 0 0 5.4-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/><path d="M19 3c1.3 1 2 2.5 2 4M17 5c.6.5 1 1.2 1 2"/></svg>`;
