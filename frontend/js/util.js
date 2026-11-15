// Tiny DOM helpers. All text goes through textContent, so model output can't inject HTML.
export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "html") throw new Error("html attr not allowed");
    else el.setAttribute(k, v === true ? "" : v);
  }
  append(el, children);
  return el;
}
export function append(el, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
export const clear = (el) => { el.replaceChildren(); return el; };

export const fmtPct = (v, d = 1) => (v === null || v === undefined ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(d)}%`);
export const fmtNum = (v, d = 0) => (v === null || v === undefined ? "–" : v.toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d }));
export const pctColor = (v) => (v === null || v === undefined ? "text-slate-500" : v >= 0 ? "text-emerald-600 dark:text-emerald-400" : "text-rose-600 dark:text-rose-400");

export function timeAgo(iso) {
  const d = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : iso + "+05:30");
  const mins = Math.round((Date.now() - d.getTime()) / 60000);
  if (mins < 60) return `${Math.max(mins, 1)}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  if (mins < 14400) return `${Math.round(mins / 1440)}d ago`;
  return d.toLocaleDateString("en-IN", { day: "numeric", month: "short" });
}

export const CATEGORY_LABEL = {
  results: "Results", "M&A": "M&A", fund_raise: "Fund raise", management_change: "Management change",
  litigation: "Litigation", insider_trading: "Insider trading", earnings_call: "Earnings call",
  investor_meetings: "Investor meetings", credit_rating: "Credit rating", dividend: "Dividend",
  bonus_split: "Bonus / split", capex: "Capex", press_release: "Press release", routine: "Routine",
  unclassified: "Other",
};

export const badge = (text, cls = "") =>
  h("span", { class: `inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${cls}` }, text);

export function spinner(label) {
  return h("div", { class: "flex items-center gap-3 p-4 text-sm text-slate-600 dark:text-slate-300", role: "status" },
    h("span", { class: "h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-indigo-600" }), label);
}
export const errorBox = (msg, retry) =>
  h("div", { class: "m-4 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-800 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200", role: "alert" },
    h("p", {}, msg), retry && h("button", { class: "mt-2 rounded-lg bg-rose-600 px-3 py-1.5 text-white", onclick: retry }, "Try again"));

/** Citation pill. `onclick` optional (chat opens the excerpt). */
export function citePill(label, onclick) {
  return h(onclick ? "button" : "span", {
    class: "inline-flex items-center rounded-md border border-indigo-200 bg-indigo-50 px-1.5 py-0.5 text-[11px] font-medium text-indigo-700 dark:border-indigo-800 dark:bg-indigo-950 dark:text-indigo-300" + (onclick ? " hover:bg-indigo-100 dark:hover:bg-indigo-900" : ""),
    type: onclick ? "button" : null, onclick,
  }, label);
}
