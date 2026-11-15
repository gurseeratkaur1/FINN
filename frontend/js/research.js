import { api } from "./api.js";
import { h, clear, badge, citePill, errorBox } from "./util.js";

const DEMO = ["RELIANCE", "BAJFINANCE", "TCS", "ITC"];
const STATUS = {
  "Beat": "bg-emerald-100 text-emerald-800 ring-emerald-300 dark:bg-emerald-950 dark:text-emerald-300 dark:ring-emerald-800",
  "On Track": "bg-blue-100 text-blue-800 ring-blue-300 dark:bg-blue-950 dark:text-blue-300 dark:ring-blue-800",
  "Delayed / Missed": "bg-rose-100 text-rose-800 ring-rose-300 dark:bg-rose-950 dark:text-rose-300 dark:ring-rose-800",
  "Partially Disclosed": "bg-amber-100 text-amber-800 ring-amber-300 dark:bg-amber-950 dark:text-amber-300 dark:ring-amber-800",
};

export function mountResearch(root, ctx) {
  const state = { ticker: null, data: null, loading: false, abort: null, timer: null };
  const input = h("input", { type: "search", placeholder: "Any NSE ticker, e.g. INFY", "aria-label": "NSE ticker", autocapitalize: "characters", autocomplete: "off", spellcheck: "false",
    class: "min-w-0 flex-1 rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm uppercase dark:border-slate-700 dark:bg-slate-900",
    onkeydown: (e) => { if (e.key === "Enter") go(input.value); } });
  const pills = h("div", { class: "mt-3 flex flex-wrap gap-2" });
  const out = h("div", { class: "mt-4 space-y-4" });
  root.append(
    h("div", { class: "flex gap-2" }, input, h("button", { class: "rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700", onclick: () => go(input.value) }, "Research")),
    pills, out);

  function paintPills() {
    clear(pills);
    DEMO.forEach((t) => pills.append(h("button", { type: "button", "aria-pressed": String(state.ticker === t),
      class: `rounded-full px-3.5 py-1.5 text-sm font-medium ${state.ticker === t ? "bg-indigo-600 text-white" : "bg-white text-slate-700 ring-1 ring-slate-300 dark:bg-slate-900 dark:text-slate-200 dark:ring-slate-700"}`,
      onclick: () => go(t) }, t)));
  }

  async function go(raw) {
    const ticker = (raw || "").trim().toUpperCase().replace(/\.NS$/, "");
    if (!ticker) return;
    if (!/^[A-Z0-9&-]{1,20}$/.test(ticker)) { clear(out).append(errorBox("That doesn't look like an NSE ticker. Use letters and digits, e.g. TCS.")); return; }
    state.abort?.abort(); clearInterval(state.timer);
    state.ticker = ticker; state.data = null; state.loading = true; input.value = ticker; paintPills();
    const ctl = (state.abort = new AbortController());
    const started = Date.now();
    const status = h("p", { class: "text-sm text-slate-600 dark:text-slate-300" });
    const tick = () => { status.textContent = `Fetching Screener.in annual report & concalls and writing the analysis… ${Math.round((Date.now() - started) / 1000)}s. Uncached tickers take 1–2 min.`; };
    tick(); state.timer = setInterval(tick, 1000);
    clear(out).append(h("div", { class: "rounded-2xl border border-slate-200 bg-white p-5 dark:border-slate-800 dark:bg-slate-900", role: "status" },
      h("div", { class: "mb-2 flex items-center gap-3" }, h("span", { class: "h-5 w-5 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" }), h("strong", {}, `Researching ${ticker}`)), status,
      h("button", { class: "mt-3 text-sm text-indigo-600 underline", onclick: () => { ctl.abort(); clearInterval(state.timer); clear(out); state.loading = false; } }, "Cancel")));
    try {
      state.data = await api(`/api/research/${encodeURIComponent(ticker)}`, { signal: ctl.signal });
      clearInterval(state.timer); state.loading = false; render();
    } catch (e) {
      clearInterval(state.timer); state.loading = false;
      if (e.name === "AbortError") return;
      clear(out).append(errorBox(e.status === 404 ? `Couldn't find ${ticker} on Screener.in, or it has no annual report / concall PDFs. Check the NSE symbol.` : e.message, () => go(ticker)));
    }
  }

  const stripCites = (text, cites) => cites.reduce((t, c) => t.split(`[${c}]`).join(""), text).replace(/\s+/g, " ").trim();

  function bullets(items) {
    if (!items?.length) return h("p", { class: "text-sm text-slate-500" }, "Nothing reliably supported by the sources.");
    return h("ul", { class: "space-y-3" }, items.map((b) => h("li", { class: "text-sm text-slate-800 dark:text-slate-200" },
      h("span", {}, stripCites(b.text, b.citations)), " ", h("span", { class: "inline-flex flex-wrap gap-1 align-middle" }, b.citations.map((c) => citePill(c))))));
  }

  function section(title, items, open = false) {
    return h("details", { class: "group rounded-2xl border border-slate-200 bg-white shadow-sm dark:border-slate-800 dark:bg-slate-900", open },
      h("summary", { class: "flex cursor-pointer list-none items-center justify-between p-4 text-base font-semibold" }, title, h("span", { class: "text-slate-400 transition group-open:rotate-180", "aria-hidden": "true" }, "⌄")),
      h("div", { class: "px-4 pb-4" }, bullets(items)));
  }

  function pvdCard(items, notes) {
    return h("section", { class: "overflow-hidden rounded-2xl border-2 border-indigo-300 bg-white shadow-md dark:border-indigo-700 dark:bg-slate-900", "aria-label": "Promised versus delivered" },
      h("div", { class: "bg-gradient-to-r from-indigo-600 to-violet-600 p-4 text-white" },
        h("h2", { class: "text-lg font-bold" }, "Promised vs. Delivered"),
        h("p", { class: "text-sm text-indigo-100" }, "What management said it would do, checked against what the later numbers show.")),
      notes.length > 0 && h("div", { class: "border-b border-amber-200 bg-amber-50 px-4 py-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950 dark:text-amber-200" }, notes.join(" ")),
      h("div", { class: "divide-y divide-slate-100 dark:divide-slate-800" }, items.length ? items.map((p, i) => h("article", { class: "space-y-2 p-4" },
        h("div", { class: "flex items-start justify-between gap-3" },
          h("h3", { class: "text-sm font-semibold text-slate-900 dark:text-slate-100" }, `${i + 1}. ${p.commitment}`),
          h("span", { class: `shrink-0 rounded-full px-2.5 py-1 text-xs font-bold ring-1 ${STATUS[p.status] || STATUS["Partially Disclosed"]}` }, p.status)),
        h("div", { class: "rounded-lg bg-slate-50 p-3 text-sm dark:bg-slate-800/60" },
          h("p", { class: "text-xs font-semibold uppercase tracking-wide text-slate-500" }, "Management said"),
          p.source_quote ? h("p", { class: "italic text-slate-700 dark:text-slate-300" }, `“${p.source_quote}”`) : h("p", { class: "text-xs italic text-slate-500" }, "Exact quote could not be verified; see the cited page."),
          h("div", { class: "mt-1" }, citePill(p.source_citation))),
        h("div", { class: "rounded-lg bg-slate-50 p-3 text-sm dark:bg-slate-800/60" },
          h("p", { class: "text-xs font-semibold uppercase tracking-wide text-slate-500" }, "What happened"),
          h("p", { class: "text-slate-800 dark:text-slate-200" }, p.actual_delivery),
          h("div", { class: "mt-1 flex flex-wrap gap-1" }, p.delivery_citation ? p.delivery_citation.split(/,\s(?=[A-Z])/).map((c) => citePill(c)) : h("span", { class: "text-xs text-slate-500" }, "No supporting source found"))),
        h("p", { class: "text-sm text-slate-600 dark:text-slate-400" }, h("span", { class: "font-semibold" }, "Analyst note: "), p.analyst_note)))
        : h("p", { class: "p-4 text-sm text-slate-500" }, "No measurable commitments could be verified from the available documents.")));
  }

  function render() {
    const d = state.data; clear(out);
    const q = d.quality || {};
    out.append(
      h("div", {}, h("h2", { class: "text-xl font-bold" }, d.name || d.ticker), h("p", { class: "text-xs text-slate-500" }, `${d.ticker} · generated ${new Date(d.generated_at).toLocaleString("en-IN")} · sources: ${d.sources.map((s) => s.label).join(", ")}`)),
      pvdCard(d.promised_vs_delivered, d.notes || []),
      section("Overview", d.overview, true), section("Financial health", d.financial_health, true),
      section("Growth catalysts", d.growth_catalysts), section("Key risks", d.key_risks),
      h("p", { class: "px-1 text-xs text-slate-500" }, "AI-generated from company documents; page numbers are PDF page positions. Statuses are a judgement, so check the cited page. Not investment advice."));
  }

  paintPills();
  return { open: go };
}
