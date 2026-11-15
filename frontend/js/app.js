import { api } from "./api.js";
import { h, clear } from "./util.js";
import { mountToday } from "./today.js";
import { mountResearch } from "./research.js";
import { mountChat } from "./chat.js";

const TABS = [
  { id: "today", label: "Today", icon: "M12 3v2m0 14v2m9-9h-2M5 12H3m15.4-6.4-1.4 1.4M7 17l-1.4 1.4m12.8 0L17 17M7 7 5.6 5.6M12 8a4 4 0 100 8 4 4 0 000-8z" },
  { id: "research", label: "Research", icon: "M21 21l-4.3-4.3M11 18a7 7 0 100-14 7 7 0 000 14z" },
  { id: "chat", label: "Ask FINN", icon: "M8 10h8M8 14h5m-9 6 3-3h11a2 2 0 002-2V6a2 2 0 00-2-2H6a2 2 0 00-2 2v14z" },
];

const portfolio = { companies: [] };
const ctx = {
  companies: () => portfolio.companies,
  byTicker: (t) => portfolio.companies.find((c) => c.ticker === t),
  openResearch: (t) => { show("research"); views.research.open(t); },
  toast: (msg) => {
    const el = h("div", { class: "fixed left-1/2 top-4 z-50 -translate-x-1/2 rounded-full bg-slate-900 px-4 py-2 text-sm text-white shadow-lg", role: "status" }, msg);
    document.body.append(el); setTimeout(() => el.remove(), 2200);
  },
};

const panels = {}, views = {}, tabButtons = [];
const icon = (d) => { const s = document.createElementNS("http://www.w3.org/2000/svg", "svg"); s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "h-6 w-6"); s.setAttribute("fill", "none"); s.setAttribute("stroke", "currentColor"); s.setAttribute("stroke-width", "1.8"); s.setAttribute("stroke-linecap", "round"); s.setAttribute("stroke-linejoin", "round"); s.setAttribute("aria-hidden", "true"); const p = document.createElementNS("http://www.w3.org/2000/svg", "path"); p.setAttribute("d", d); s.append(p); return s; };

function show(id) {
  TABS.forEach((t) => { panels[t.id].hidden = t.id !== id; });
  tabButtons.forEach(({ id: bid, el }) => { const on = bid === id; el.setAttribute("aria-current", on ? "page" : "false"); el.classList.toggle("text-indigo-600", on); el.classList.toggle("dark:text-indigo-400", on); el.classList.toggle("text-slate-500", !on); });
  history.replaceState(null, "", "#" + id); window.scrollTo(0, 0);
}

function tabButton(t, cls) {
  const el = h("button", { class: cls, onclick: () => show(t.id) }, icon(t.icon), h("span", {}, t.label));
  tabButtons.push({ id: t.id, el }); return el;
}

async function boot() {
  const main = document.getElementById("main");
  document.getElementById("topnav").append(...TABS.map((t) => tabButton(t, "flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium hover:bg-slate-100 dark:hover:bg-slate-800 [&>svg]:h-5 [&>svg]:w-5")));
  document.getElementById("bottomnav").append(...TABS.map((t) => tabButton(t, "flex flex-1 flex-col items-center gap-0.5 py-2 text-xs font-medium")));
  TABS.forEach((t) => { panels[t.id] = h("section", { id: `tab-${t.id}`, hidden: true, "aria-label": t.label }); main.append(panels[t.id]); });

  try { portfolio.companies = (await api("/api/portfolio")).companies; } catch (e) { console.warn("portfolio load failed", e); }
  views.today = mountToday(panels.today, ctx);
  views.research = mountResearch(panels.research, ctx);
  views.chat = mountChat(panels.chat, ctx);
  const start = location.hash.slice(1);
  show(TABS.some((t) => t.id === start) ? start : "today");
}
boot();
