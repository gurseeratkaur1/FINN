import { api } from "./api.js";
import { h, clear, citePill, spinner } from "./util.js";

const STARTERS = [
  ["Compare Bajaj Finance's ROA/ROE guidance vs actual delivery", "BAJFINANCE"],
  ["What happened in Reliance's latest results and Jio ARPU?", "RELIANCE"],
  ["Summarize recent Must-Read alerts in my portfolio", ""],
  ["What are TCS's key growth catalysts & risks?", "TCS"],
];

export function mountChat(root, ctx) {
  const history = [];                      // {role, text, citations?, tickers?, error?}
  const thread = h("div", { class: "space-y-4 pb-4", "aria-live": "polite" });
  const chips = h("div", { class: "flex flex-wrap gap-2" });
  const select = h("select", { "aria-label": "Company context", class: "min-w-0 rounded-lg border border-slate-300 bg-white px-2 py-1.5 text-sm dark:border-slate-700 dark:bg-slate-900" });
  const input = h("textarea", { rows: "1", placeholder: "Ask about a company…", "aria-label": "Your question", maxlength: "2000",
    class: "max-h-32 min-w-0 flex-1 resize-none rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900",
    onkeydown: (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input.value); } },
    oninput: () => { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 128) + "px"; } });
  const sendBtn = h("button", { class: "rounded-xl bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 disabled:opacity-50", onclick: () => send(input.value) }, "Send");
  const composer = h("div", { class: "sticky bottom-[4.5rem] -mx-4 border-t border-slate-200 bg-slate-50/95 px-4 py-3 backdrop-blur dark:border-slate-800 dark:bg-slate-950/95 md:bottom-0" },
    h("div", { class: "flex items-end gap-2" }, input, sendBtn));
  root.append(
    h("div", { class: "mb-3 flex items-center gap-2 text-sm" }, h("span", { class: "text-slate-600 dark:text-slate-300" }, "Context:"), select),
    chips, thread, composer);

  function paintSelect() {
    clear(select);
    select.append(h("option", { value: "" }, "Auto-detect"), ...ctx.companies().map((c) => h("option", { value: c.ticker }, `${c.ticker} – ${c.name}`)));
  }

  function paintChips() {
    clear(chips);
    if (history.length) return;
    chips.append(h("p", { class: "w-full text-xs font-medium text-slate-500" }, "Try one of these"),
      ...STARTERS.map(([q, t]) => h("button", { class: "rounded-2xl border border-slate-300 bg-white px-3 py-2 text-left text-sm text-slate-700 hover:bg-slate-100 dark:border-slate-700 dark:bg-slate-900 dark:text-slate-200 dark:hover:bg-slate-800",
        onclick: () => { select.value = t; send(q); } }, q)));
  }

  /** **bold**, "- " bullets and [citation] pills. Everything is built with text nodes. */
  function renderReply(text, citations) {
    const byLabel = new Map((citations || []).map((c) => [c.label, c]));
    const inline = (line, panel) => {
      const parts = [];
      line.split(/(\[[^\]]+\]|\*\*[^*]+\*\*)/).forEach((tok) => {
        if (!tok) return;
        if (tok.startsWith("**") && tok.endsWith("**")) parts.push(h("strong", {}, tok.slice(2, -2)));
        else if (tok.startsWith("[") && byLabel.has(tok.slice(1, -1))) { const c = byLabel.get(tok.slice(1, -1)); parts.push(citePill(c.label, () => panel(c))); parts.push(" "); }
        else parts.push(tok);
      });
      return parts;
    };
    const excerpt = h("div", { class: "mt-2 hidden rounded-lg border border-indigo-200 bg-indigo-50 p-3 text-xs text-slate-700 dark:border-indigo-800 dark:bg-indigo-950/50 dark:text-slate-200" });
    const show = (c) => {
      clear(excerpt).append(h("p", { class: "font-semibold" }, c.label), h("p", { class: "mt-1 whitespace-pre-line" }, c.excerpt + "…"),
        c.url && h("a", { class: "mt-1 inline-block text-indigo-600 underline dark:text-indigo-400", href: c.url, target: "_blank", rel: "noopener noreferrer" }, "Open source ↗"));
      excerpt.classList.remove("hidden");
    };
    const body = h("div", { class: "space-y-2 text-sm leading-relaxed" });
    let list = null;
    text.split("\n").forEach((raw) => {
      const line = raw.trim();
      if (!line) { list = null; return; }
      const m = line.match(/^[-*•]\s+(.*)/);
      if (m) { if (!list) body.append((list = h("ul", { class: "list-disc space-y-1 pl-5" }))); list.append(h("li", {}, inline(m[1], show))); }
      else { list = null; body.append(h("p", {}, inline(line, show))); }
    });
    return h("div", {}, body, excerpt);
  }

  function paintThread(pending) {
    clear(thread);
    history.forEach((m) => {
      if (m.role === "user") thread.append(h("div", { class: "flex justify-end" }, h("p", { class: "max-w-[85%] whitespace-pre-line rounded-2xl rounded-br-sm bg-indigo-600 px-4 py-2 text-sm text-white" }, m.text)));
      else thread.append(h("div", { class: "flex" }, h("div", { class: `max-w-[95%] rounded-2xl rounded-bl-sm border px-4 py-3 shadow-sm ${m.error ? "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-900 dark:bg-rose-950 dark:text-rose-200" : "border-slate-200 bg-white dark:border-slate-800 dark:bg-slate-900"}` },
        m.error ? h("p", { class: "text-sm" }, m.text) : [
          m.tickers?.length > 0 && h("p", { class: "mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500" }, `FINN · ${m.tickers.join(", ")}`),
          renderReply(m.text, m.citations),
          m.citations?.length > 0 && h("div", { class: "mt-3 border-t border-slate-100 pt-2 dark:border-slate-800" }, h("p", { class: "mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-500" }, "Sources"),
            h("div", { class: "flex flex-wrap gap-1" }, m.citations.map((c) => (c.url ? h("a", { href: c.url, target: "_blank", rel: "noopener noreferrer" }, citePill(c.label)) : citePill(c.label)))))])));
    });
    if (pending) thread.append(spinner("FINN is reading the filings and transcripts…"));
    thread.lastElementChild?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  async function send(text) {
    text = (text || "").trim();
    if (!text || sendBtn.disabled) return;
    history.push({ role: "user", text }); input.value = ""; input.style.height = "auto";
    sendBtn.disabled = true; paintChips(); paintThread(true);
    try {
      const ctl = new AbortController(); const timer = setTimeout(() => ctl.abort(), 150000);
      let r; try { r = await api("/api/chat", { method: "POST", body: { message: text, ticker: select.value || null }, signal: ctl.signal }); } finally { clearTimeout(timer); }
      history.push({ role: "assistant", text: r.reply, citations: r.citations, tickers: r.tickers });
    } catch (e) { history.push({ role: "assistant", error: true, text: e.name === "AbortError" ? "The AI service is taking too long (it may be overloaded). Please try again in a minute." : e.message }); }
    sendBtn.disabled = false; paintThread(false);
  }

  paintSelect(); paintChips();
  return { refreshCompanies: paintSelect };
}
