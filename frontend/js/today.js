import { api } from "./api.js";
import { h, clear, fmtPct, fmtNum, pctColor, timeAgo, badge, spinner, errorBox, CATEGORY_LABEL } from "./util.js";

const DEFAULT_CATS = ["results", "M&A", "management_change", "litigation", "fund_raise", "credit_rating"];
const SEV = { high: "border-rose-300 bg-rose-50 dark:border-rose-800 dark:bg-rose-950/40", medium: "border-amber-300 bg-amber-50 dark:border-amber-800 dark:bg-amber-950/40" };
const ALERT_ICON = { volume_spike: "Volume spike", price_move: "Price move", material_filing: "Filing" };

export function mountToday(root, ctx) {
  const state = { tier: "MUST_READ", ticker: "", today: null, all: null, alerts: null, settings: null, drawer: false, error: null, loading: true };

  const alertsBox = h("section", { "aria-label": "Live alerts" });
  const drawerBox = h("div");
  const filtersBox = h("div", { class: "sticky top-0 z-10 -mx-4 bg-slate-50/95 px-4 py-2 backdrop-blur dark:bg-slate-950/95 md:top-14" });
  const listBox = h("section", { class: "space-y-3", "aria-label": "Filings" });
  root.append(alertsBox, drawerBox, filtersBox, listBox);

  async function load() {
    state.loading = true; state.error = null; render();
    try {
      const [today, settings] = await Promise.all([api("/api/today?days=7&limit=100"), api("/api/settings")]);
      state.today = today; state.settings = settings; state.alerts = today.alerts;
      state.all = null;
    } catch (e) { state.error = e; }
    state.loading = false; render();
  }
  async function loadAll() {
    state.loading = true; render();
    try { state.all = (await api(`/api/announcements?limit=100${state.ticker ? "&ticker=" + state.ticker : ""}`)).announcements; }
    catch (e) { state.error = e; }
    state.loading = false; render();
  }

  // ---------------- alerts strip + settings drawer ---------------- //
  function renderAlerts() {
    clear(alertsBox);
    if (!state.alerts) return;
    const n = state.alerts.length;
    alertsBox.append(h("div", { class: "mb-2 flex items-center justify-between" },
      h("h2", { class: "text-sm font-semibold text-slate-900 dark:text-slate-100" }, `Live alerts `, badge(String(n), "bg-rose-600 text-white")),
      h("button", { class: "rounded-lg border border-slate-300 px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-200 dark:hover:bg-slate-800",
        "aria-expanded": String(state.drawer), onclick: () => { state.drawer = !state.drawer; renderDrawer(); renderAlerts(); } },
        state.drawer ? "Close settings" : "Alert settings")));
    if (!n) { alertsBox.append(h("p", { class: "rounded-xl border border-dashed border-slate-300 p-3 text-sm text-slate-500 dark:border-slate-700" }, "No alerts at your current thresholds.")); return; }
    alertsBox.append(h("div", { class: "-mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-2" },
      state.alerts.map((a) => h("article", { class: `w-72 shrink-0 snap-start rounded-xl border p-3 ${SEV[a.severity] || SEV.medium}` },
        h("div", { class: "mb-1 flex items-center gap-2 text-xs" },
          badge(a.ticker, "bg-slate-900 text-white dark:bg-slate-100 dark:text-slate-900"),
          h("span", { class: "text-slate-600 dark:text-slate-300" }, ALERT_ICON[a.type] || a.type),
          a.severity === "high" && badge("High", "bg-rose-600 text-white")),
        h("p", { class: "text-sm font-semibold text-slate-900 dark:text-slate-100" }, a.headline),
        h("p", { class: "mt-1 line-clamp-4 text-xs text-slate-700 dark:text-slate-300" }, a.why)))));
  }

  function renderDrawer() {
    clear(drawerBox);
    if (!state.drawer || !state.settings) return;
    const s = state.settings;
    const draft = { volume_multiple: s.volume_multiple, price_move_pct: s.price_move_pct, categories: new Set(s.categories) };
    const status = h("p", { class: "text-xs text-slate-500", "aria-live": "polite" });
    const num = (label, key, step, unit) => {
      const out = h("span", { class: "w-14 text-right text-sm font-semibold tabular-nums" }, draft[key].toFixed(1) + unit);
      const input = h("input", { type: "range", min: key === "volume_multiple" ? "1" : "0.5", max: key === "volume_multiple" ? "10" : "15", step, value: String(draft[key]), class: "flex-1 accent-indigo-600", "aria-label": label,
        oninput: (e) => { draft[key] = parseFloat(e.target.value); out.textContent = draft[key].toFixed(1) + unit; } });
      return h("label", { class: "flex items-center gap-3 text-sm" }, h("span", { class: "w-28 text-slate-700 dark:text-slate-300" }, label), input, out);
    };
    const catBox = h("div", { class: "flex flex-wrap gap-2" });
    const paintCats = () => { clear(catBox); (s.available_categories || DEFAULT_CATS).filter((c) => !["routine", "unclassified"].includes(c)).forEach((c) => {
      const on = draft.categories.has(c);
      catBox.append(h("button", { type: "button", "aria-pressed": String(on),
        class: `rounded-full border px-3 py-1 text-xs font-medium ${on ? "border-indigo-600 bg-indigo-600 text-white" : "border-slate-300 text-slate-600 dark:border-slate-700 dark:text-slate-300"}`,
        onclick: () => { on ? draft.categories.delete(c) : draft.categories.add(c); paintCats(); } }, CATEGORY_LABEL[c] || c)); }); };
    paintCats();
    const save = async (payload) => {
      status.textContent = "Saving…";
      try {
        const r = await api("/api/settings", { method: "PUT", body: payload });
        state.settings = { ...state.settings, ...r.settings }; state.alerts = r.alerts;
        status.textContent = `Saved. ${r.alerts.length} alert${r.alerts.length === 1 ? "" : "s"} now.`;
        renderAlerts(); renderDrawer(); state.drawer = true; drawerBox.append(); ctx.toast?.("Alert settings saved");
      } catch (e) { status.textContent = e.message; }
    };
    drawerBox.append(h("div", { class: "my-3 space-y-4 rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900" },
      h("h3", { class: "text-sm font-semibold" }, "Alert settings"),
      num("Volume multiple", "volume_multiple", "0.1", "x"),
      num("Price move", "price_move_pct", "0.5", "%"),
      h("div", {}, h("p", { class: "mb-2 text-xs font-medium text-slate-500" }, "Watched filing categories (Must Read filings always alert)"), catBox),
      h("div", { class: "flex flex-wrap items-center gap-2" },
        h("button", { class: "rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700", onclick: () => save({ volume_multiple: draft.volume_multiple, price_move_pct: draft.price_move_pct, categories: [...draft.categories] }) }, "Save & update alerts"),
        h("button", { class: "rounded-lg border border-slate-300 px-4 py-2 text-sm dark:border-slate-700", onclick: () => save({ volume_multiple: 2.0, price_move_pct: 5.0, categories: DEFAULT_CATS }) }, "Reset defaults"),
        status)));
  }

  // ---------------- filters ---------------- //
  function renderFilters() {
    clear(filtersBox);
    const pill = (id, label) => h("button", { type: "button", "aria-pressed": String(state.tier === id),
      class: `whitespace-nowrap rounded-full px-3.5 py-1.5 text-sm font-medium ${state.tier === id ? "bg-indigo-600 text-white" : "bg-white text-slate-700 ring-1 ring-slate-300 dark:bg-slate-900 dark:text-slate-200 dark:ring-slate-700"}`,
      onclick: () => { state.tier = id; id === "ALL" ? loadAll() : render(); } }, label);
    const select = h("select", { "aria-label": "Filter by company", class: "w-full rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900",
      onchange: (e) => { state.ticker = e.target.value; state.tier === "ALL" ? loadAll() : render(); } },
      h("option", { value: "" }, "All companies"), ...ctx.companies().map((c) => h("option", { value: c.ticker, selected: c.ticker === state.ticker }, c.ticker)));
    filtersBox.append(h("div", { class: "flex flex-wrap items-center gap-2" }, pill("MUST_READ", "Must Read"), pill("WORTH_A_LOOK", "Worth a Look"), pill("ALL", "All"), h("div", { class: "w-full sm:ml-auto sm:w-44" }, select)));
  }

  // ---------------- filing cards ---------------- //
  function visibleRows() {
    if (state.tier === "ALL") return state.all || [];
    const rows = state.tier === "MUST_READ" ? state.today.must_read : state.today.worth_a_look;
    return rows.filter((r) => !state.ticker || r.ticker === state.ticker);
  }

  function resultsTable(res) {
    const m = res.metrics, v = m.values;
    const rows = [["Revenue", "revenue"], ["EBITDA", "ebitda"], ["PBT", "pbt"], ["PAT", "pat"]];
    const cell = (x, cls = "") => h("td", { class: `px-2 py-1 text-right tabular-nums ${cls}` }, x);
    return h("div", { class: "mt-3 overflow-x-auto rounded-xl border border-slate-200 dark:border-slate-800" },
      h("p", { class: "px-3 pt-2 text-xs text-slate-500" }, `${res.period} · ${res.basis || ""} · ${res.unit || "as filed"}`),
      h("table", { class: "w-full text-xs" },
        h("thead", {}, h("tr", { class: "text-slate-500" }, ["", "Latest", "QoQ", "YoY"].map((t, i) => h("th", { class: `px-2 py-1 font-medium ${i ? "text-right" : "text-left"}` }, t)))),
        h("tbody", {}, rows.map(([label, k]) => h("tr", { class: "border-t border-slate-100 dark:border-slate-800" },
          h("td", { class: "px-2 py-1 font-medium" }, label + (k === "ebitda" && m.ebitda_margin_pct != null ? ` (${m.ebitda_margin_pct.toFixed(1)}%)` : "")),
          cell(fmtNum(v.current_qtr[k], 0)), cell(fmtPct(m[`${k}_qoq_pct`]), pctColor(m[`${k}_qoq_pct`])), cell(fmtPct(m[`${k}_yoy_pct`]), pctColor(m[`${k}_yoy_pct`])))))),
      m.ebitda_derived && h("p", { class: "px-3 pb-2 text-[11px] text-slate-500" }, "EBITDA derived from PBT + finance costs + depreciation."));
  }

  function mgmtBlock(changes) {
    return h("div", { class: "mt-3 space-y-2" }, changes.map((c) => {
      const kind = { joining: ["Joining", "bg-emerald-100 text-emerald-800 dark:bg-emerald-950 dark:text-emerald-300"], resigning: ["Resigning", "bg-rose-100 text-rose-800 dark:bg-rose-950 dark:text-rose-300"],
        retiring: ["Retiring", "bg-amber-100 text-amber-800 dark:bg-amber-950 dark:text-amber-300"], reappointed: ["Re-appointed", "bg-blue-100 text-blue-800 dark:bg-blue-950 dark:text-blue-300"] }[c.change_type] || ["Change", "bg-slate-100 text-slate-700"];
      return h("div", { class: "rounded-xl border border-slate-200 p-3 dark:border-slate-800" },
        h("div", { class: "flex flex-wrap items-center gap-2" }, h("span", { class: "font-semibold text-sm" }, c.person_name || "Unnamed"), badge(kind[0], kind[1])),
        h("p", { class: "text-xs text-slate-500" }, [c.role, c.effective_date && `effective ${c.effective_date}`].filter(Boolean).join(" · ")),
        c.bio && c.bio_status !== "not_needed" && h("p", { class: "mt-1 line-clamp-3 text-xs text-slate-700 dark:text-slate-300" }, c.bio));
    }));
  }

  function card(r) {
    const company = ctx.byTicker(r.ticker), px = company?.price;
    const detail = r.results ? resultsTable(r.results) : r.management_changes?.length ? mgmtBlock(r.management_changes) : null;
    const tierCls = r.tier === "MUST_READ" ? "bg-rose-600 text-white" : r.tier === "WORTH_A_LOOK" ? "bg-amber-500 text-white" : "bg-slate-200 text-slate-700 dark:bg-slate-700 dark:text-slate-200";
    return h("article", { class: "rounded-2xl border border-slate-200 bg-white p-4 shadow-sm dark:border-slate-800 dark:bg-slate-900" },
      h("div", { class: "flex flex-wrap items-center gap-2" },
        h("button", { class: "rounded-md bg-slate-900 px-2 py-0.5 text-xs font-bold text-white dark:bg-slate-100 dark:text-slate-900", title: `Research ${r.ticker}`, onclick: () => ctx.openResearch(r.ticker) }, r.ticker),
        px && h("span", { class: `text-xs font-semibold tabular-nums ${pctColor(px.change_1d_pct)}` }, `${fmtPct(px.change_1d_pct, 2)} 1D`),
        px && px.volume_ratio != null && badge(`Vol ${px.volume_ratio.toFixed(1)}x`, px.volume_ratio >= 1.5 ? "bg-orange-100 text-orange-800 dark:bg-orange-950 dark:text-orange-300" : "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300"),
        badge(CATEGORY_LABEL[r.category] || r.category, "bg-indigo-50 text-indigo-700 dark:bg-indigo-950 dark:text-indigo-300"),
        state.tier === "ALL" && badge(r.tier.replace(/_/g, " "), tierCls),
        h("span", { class: "ml-auto text-xs text-slate-500" }, timeAgo(r.announced_at))),
      h("h3", { class: "mt-2 text-sm font-semibold text-slate-900 dark:text-slate-100" }, r.subject),
      r.summary && h("p", { class: "mt-1 text-sm text-slate-700 dark:text-slate-300" }, h("span", { class: "font-medium text-slate-900 dark:text-slate-100" }, "Why it matters: "), r.summary),
      detail,
      r.pdf_url && h("a", { class: "mt-3 inline-flex items-center gap-1 text-sm font-medium text-indigo-600 hover:underline dark:text-indigo-400", href: r.pdf_url, target: "_blank", rel: "noopener noreferrer" }, "Open filing PDF ↗"));
  }

  function renderList() {
    clear(listBox);
    if (state.error) { listBox.append(errorBox(state.error.message, load)); return; }
    if (state.loading && !state.today) { listBox.append(spinner("Loading today’s filings…")); return; }
    if (state.loading) listBox.append(spinner("Loading…"));
    const rows = visibleRows();
    if (!rows.length && !state.loading) listBox.append(h("p", { class: "rounded-xl border border-dashed border-slate-300 p-6 text-center text-sm text-slate-500 dark:border-slate-700" }, "No filings match this filter in the last 7 days."));
    rows.forEach((r) => listBox.append(card(r)));
  }

  function render() { renderAlerts(); renderDrawer(); renderFilters(); renderList(); }
  load();
  return { refresh: load };
}
