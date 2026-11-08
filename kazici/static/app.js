/* kazici web UI — plain JS, no build step */
"use strict";

// ------------------------------------------------------------ helpers
const $ = (s, r = document) => r.querySelector(s);
const main = $("#main");

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of kids.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const ICON = {
  play: '<path d="M7 5v14l11-7z"/>',
  star: '<path d="M12 3l2.2 5.6L20 9.5l-4.5 3.8L17 19l-5-3-5 3 1.5-5.7L4 9.5l5.8-.9z"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  back: '<path d="M15 6l-6 6 6 6"/>',
  chev: '<path d="M9 6l6 6-6 6"/>',
  save: '<path d="M5 4h11l3 3v13H5z"/><path d="M8 4v5h7V4M8 20v-6h8v6"/>',
  trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  dl: '<path d="M12 4v11M7 10l5 5 5-5M5 20h14"/>',
  copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',
  stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.3-5.7M20 4v5h-5"/>',
  wand: '<path d="M4 20L16 8M14 4v2M20 10h-2M18 4l-1.5 1.5M15 3l1 1"/>',
  box: '<path d="M4 7l8-4 8 4v10l-8 4-8-4z"/><path d="M4 7l8 4 8-4M12 11v10"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
};
const icon = (n) => h("span", { html: `<svg viewBox="0 0 24 24">${ICON[n]}</svg>`, style: "display:contents" });

async function api(path, opts = {}) {
  const o = { headers: {}, ...opts };
  if (o.body && typeof o.body !== "string") { o.body = JSON.stringify(o.body); o.headers["Content-Type"] = "application/json"; }
  const r = await fetch(path, o);
  let data = null;
  const txt = await r.text();
  try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) { const e = new Error((data && data.error) || `HTTP ${r.status}`); e.status = r.status; throw e; }
  return data;
}

function toast(msg, kind = "") {
  const t = h("div", { class: `toast ${kind}` }, msg);
  $("#toasts").append(t);
  setTimeout(() => t.remove(), kind === "err" ? 6000 : 3200);
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("kazici-" + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("kazici-" + k, JSON.stringify(v)); } catch {} },
};

const fmtNum = (n) => (n == null ? "–" : Number(n).toLocaleString("en-US"));
const fmtSize = (b) => (b < 1024 ? `${b} B` : b < 1048576 ? `${(b / 1024).toFixed(1)} KB` : `${(b / 1048576).toFixed(1)} MB`);
function fmtTime(t) {
  if (!t) return "–";
  const d = typeof t === "number" ? new Date(t * 1000) : new Date(t);
  return d.toLocaleString("en-US", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
}
function fmtDur(s) {
  s = Math.max(0, Math.round(s));
  return s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`;
}
const lines = (s) => (s || "").split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };

function statusPill(s) {
  const map = { OK: "ok", AT_LIMIT: "warn", PROBLEM: "err", "NO_RULE": "err", "ROBOTS_BLOCKED": "warn", done: "ok", error: "err", cancelled: "warn", running: "acc live" };
  const label = { done: "done", error: "error", cancelled: "cancelled", running: "running" }[s] || s;
  return h("span", { class: `pill ${map[s] || ""}` }, h("i", { class: "dot" }), label == null || label === "" ? "?" : String(label));
}

function pageHead(title, desc, ...right) {
  return h("div", { class: "page-head" }, h("div", {}, h("h1", {}, title), desc ? h("p", {}, desc) : null), h("div", { class: "row" }, ...right));
}
function crumb(href, label) {
  return h("a", { class: "crumb", href }, icon("back"), label);
}
function field(label, input, hint) {
  return h("label", { class: "f" }, h("span", {}, label, hint ? h("span", { class: "hint" }, "  " + hint) : null), input);
}
function emptyState(text, ic = "box") {
  return h("div", { class: "empty" }, h("div", { html: `<svg viewBox="0 0 24 24">${ICON[ic]}</svg>` }), h("div", {}, text));
}
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied to clipboard"); } catch { toast("Could not copy", "err"); }
}
function download(name, text, type) {
  const a = h("a", { href: URL.createObjectURL(new Blob([text], { type })), download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ------------------------------------------------------------ theme
function applyTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = t;
  try { localStorage.setItem("kazici-theme", t); } catch {}
  document.querySelectorAll("[data-theme-set]").forEach((b) => b.classList.toggle("on", b.dataset.themeSet === t));
}
document.querySelectorAll("[data-theme-set]").forEach((b) => b.addEventListener("click", () => applyTheme(b.dataset.themeSet)));
applyTheme((() => { try { return localStorage.getItem("kazici-theme") || "system"; } catch { return "system"; } })());

// ------------------------------------------------------------ job tracking
function classifyLine(l) {
  if (/error|failed|could not learn|robots\.txt (disallow|block|forbid)/i.test(l)) return "l-err";
  if (/ ! |Warning|UNVERIFIED|PROBLEM|no matching rule/.test(l)) return "l-warn";
  if (/\bVERIFIED\b|(decision|verdict): accept|Total \d/.test(l)) return "l-ok";
  if (/^\S+\s{2}\s{2,}/.test(l)) return "l-dim";
  return "";
}

/** Tracks a job live inside a card. onDone(job) is called when it finishes, to draw the result. */
function jobCard(jobId, onDone) {
  const title = h("span", { class: "t" }, "…");
  const pill = h("span", {});
  const time = h("span", { class: "time" });
  const cancel = h("button", { class: "btn sm ghost danger", onclick: async () => {
    cancel.disabled = true;
    try { await api(`/api/jobs/${jobId}/cancel`, { method: "POST" }); } catch (e) { toast(e.message, "err"); }
  } }, icon("stop"), "Cancel");
  const log = h("pre", { class: "log" });
  const resultArea = h("div", {});
  const card = h("div", { class: "card" }, h("div", { class: "job-head" }, pill, title, time, cancel), log, resultArea);
  let since = 0, first = true;

  async function tick() {
    if (!first && !card.isConnected) return; // left the page
    first = false;
    try {
      const j = await api(`/api/jobs/${jobId}?since=${since}`);
      title.textContent = j.title;
      pill.replaceChildren(statusPill(j.status));
      time.textContent = fmtDur((j.finished || Date.now() / 1000) - j.started);
      const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 30;
      if (since === 0 && j.log.length) log.replaceChildren();
      if (j.log_from > since) log.append(h("span", { class: "l-warn" }, `… ${j.log_from - since} old lines skipped\n`));
      for (const l of j.log) log.append(h("span", { class: classifyLine(l) }, l + "\n"));
      if (!log.childNodes.length) log.textContent = "Starting…\n";
      since = j.log_from + j.log.length;
      if (atBottom) log.scrollTop = log.scrollHeight;
      if (j.status !== "running") {
        cancel.remove();
        if (j.status === "error") resultArea.append(h("div", { class: "result-box err" }, j.error));
        onDone && onDone(j, resultArea);
        refreshBadge();
        return;
      }
    } catch (e) {
      if (/no such job/.test(e.message)) { log.textContent = "This job is no longer on the server (the server may have been restarted)."; cancel.remove(); return; }
    }
    setTimeout(tick, 900);
  }
  tick();
  return card;
}

async function refreshBadge() {
  try {
    const { jobs } = await api("/api/jobs");
    const n = jobs.filter((j) => j.status === "running").length;
    const b = $("#jobBadge");
    b.hidden = !n; b.textContent = n;
  } catch {}
}
setInterval(refreshBadge, 4000);

// Result rendering (in the forms and on the Jobs page)
function renderJobResult(j, area) {
  if (j.status !== "done" || !j.result) return;
  const r = j.result;
  if (j.kind === "run") {
    const rows = r.rows || [];
    area.append(h("div", { class: "result-box" + (r.broken && r.broken.length ? "" : " ok") },
      h("div", { class: "row between" },
        h("div", {}, h("b", {}, `${fmtNum(r.total_items)} items`), " scraped → ", h("span", { class: "mono" }, r.output)),
        h("a", { class: "btn sm primary", href: `#/outputs/${encodeURIComponent(r.output)}` }, icon("open"), "Open result")),
      rows.length ? h("div", { class: "table-wrap", style: "margin-top:12px;background:var(--surface)" }, h("table", {},
        h("thead", {}, h("tr", {}, h("th", {}, "URL"), h("th", {}, "Rule"), h("th", { class: "num" }, "Items"), h("th", { class: "num" }, "Rounds"), h("th", {}, "Status"))),
        h("tbody", {}, rows.map((x) => h("tr", {},
          h("td", {}, h("div", { class: "cell", title: x.url }, x.url)),
          h("td", {}, x.rule ? h("a", { href: `#/rules/${encodeURIComponent(x.rule)}` }, x.rule) : "–"),
          h("td", { class: "num" }, fmtNum(x.items)), h("td", { class: "num" }, x.rounds), h("td", {}, statusPill(x.status))))))) : null,
      (r.broken || []).map((b) => h("div", { class: "issues", style: "margin-top:10px" },
        h("b", {}, b.url), ` (${b.rule}) — the rule may be broken:`, h("ul", { class: "list-plain" }, b.issues.map((i) => h("li", {}, i))))),
    ));
  } else if (j.kind === "learn") {
    area.append(h("div", { class: "result-box" + (r.accepted ? " ok" : "") },
      h("div", { class: "row between" },
        h("div", {}, statusPill(r.accepted ? "OK" : "AT_LIMIT"), " ", h("b", {}, r.rule), r.accepted ? " verified and saved" : " saved unverified — please check"),
        h("div", { class: "row" },
          h("a", { class: "btn sm", href: `#/rules/${encodeURIComponent(r.rule)}` }, "Open rule"),
          h("button", { class: "btn sm primary", onclick: () => goRun(r.search ? [] : [j.params.url], r.rule) }, icon("play"), "Run now"))),
      h("div", { class: "small muted", style: "margin-top:8px" },
        `${r.llm_calls} LLM calls · ${r.attempts} attempts · ${r.fetch} · ${r.pagination} · est. $${r.cost_usd}`),
      h("div", { class: "small muted mono", style: "margin-top:4px;word-break:break-all" }, "URL pattern: " + r.url_pattern)));
  } else if (j.kind === "simplify") {
    area.append(h("div", { class: "result-box" },
      h("div", { class: "row between", style: "margin-bottom:10px" },
        h("span", { class: "small muted" }, `${fmtNum(r.raw_chars)} → ${fmtNum(r.chars)} characters (the view the LLM sees)`),
        h("button", { class: "btn sm", onclick: () => copyText(r.text) }, icon("copy"), "Kopyala")),
      h("pre", { class: "view" }, r.text)));
  }
}

function goRun(urls, rule) {
  store.set("run-prefill", { urls: urls.join("\n"), rule: rule || "" });
  location.hash = "#/run";
}

// ------------------------------------------------------------ page: Run
async function pageRun() {
  const pre = store.get("run-prefill", null);
  store.set("run-prefill", null);
  const saved = store.get("run-form", {});
  const urls = h("textarea", { class: "mono", rows: 5, placeholder: "https://site.com/category/...\nhttps://site.com/other-category/...", value: pre ? pre.urls : saved.urls || "" });
  const ruleSel = h("select", {}, h("option", { value: "" }, "Automatic (match by URL)"));
  // Page limit: the rule's own limit, all pages or a custom number
  const limitMode = h("select", {},
    h("option", { value: "default" }, "System default"),
    h("option", { value: "all" }, "All"),
    h("option", { value: "custom" }, "Custom number…"));
  const limitNum = h("input", { type: "number", min: 1, placeholder: "pages", value: saved.limit_num || "", style: "max-width:96px" });
  limitMode.value = ["default", "all", "custom"].includes(saved.limit_mode) ? saved.limit_mode : "default";
  const syncLimit = () => { limitNum.hidden = limitMode.value !== "custom"; if (!limitNum.hidden) limitNum.focus(); };
  limitMode.addEventListener("change", syncLimit);
  limitNum.hidden = limitMode.value !== "custom";
  const maxPages = h("div", { class: "row", style: "gap:6px;flex-wrap:nowrap" }, limitMode, limitNum);
  const limitValue = () => limitMode.value === "custom" ? limitNum.value : limitMode.value;
  const conc = h("input", { type: "number", min: 1, max: 8, value: saved.concurrency || 2 });
  const outName = h("input", { type: "text", class: "mono", placeholder: "result-date.json" });
  const showBrowser = h("input", { type: "checkbox" });
  const ignoreRobots = h("input", { type: "checkbox" });
  const match = h("div", { class: "match" });
  const jobs = h("div", { class: "stack" });
  const go = h("button", { class: "btn primary", type: "submit" }, icon("play"), "Run");
  // Search rules: terms and query limit
  const queries = h("textarea", { class: "mono", rows: 3, placeholder: "one term per line (if empty: the rule's own terms, or a full sweep)", value: saved.queries || "" });
  const qMode = h("select", {},
    h("option", { value: "default" }, "System default"),
    h("option", { value: "all" }, "Unlimited"),
    h("option", { value: "custom" }, "Custom number…"));
  const qNum = h("input", { type: "number", min: 1, placeholder: "queries", value: saved.q_num || "", style: "max-width:96px" });
  qMode.value = ["default", "all", "custom"].includes(saved.q_mode) ? saved.q_mode : "default";
  qNum.hidden = qMode.value !== "custom";
  qMode.addEventListener("change", () => { qNum.hidden = qMode.value !== "custom"; if (!qNum.hidden) qNum.focus(); });
  const qValue = () => qMode.value === "custom" ? qNum.value : qMode.value;
  const searchInfo = h("div", { class: "small muted" });
  const searchBox = h("div", { class: "grid cols-2", hidden: true },
    field("Search terms", queries, "a search rule searches each term separately and merges the results"),
    h("div", { class: "stack" }, field("Query limit", h("div", { class: "row", style: "gap:6px;flex-wrap:nowrap" }, qMode, qNum), "a full sweep can send many queries"), searchInfo));
  const urlsPlaceholder = urls.placeholder;
  const rulesByName = {};
  let matched = [];
  const METHOD_LABEL = { empty: "empty search", wildcard: "wildcard search", options: "every option in the list", prefix: "letter prefixes" };
  function syncSearch() {
    const names = ruleSel.value ? [ruleSel.value] : matched;
    const sr = names.map((n) => rulesByName[n]).filter((r) => r && r.search);
    searchBox.hidden = !sr.length;
    urls.placeholder = ruleSel.value && sr.length ? "search rule: may be left empty (the rule's search page is used)" : urlsPlaceholder;
    if (!sr.length) return;
    const s = sr[0].search;
    searchInfo.textContent = s.terms === "given"
      ? `Rule's terms: ${s.queries.join(", ") || "–"}`
      : `If no terms are given, everything is swept (${METHOD_LABEL[s.method] || s.method}).`;
  }

  api("/api/rules").then(({ rules }) => {
    for (const r of rules) { rulesByName[r.name] = r; ruleSel.append(h("option", { value: r.name }, `${r.name} — ${r.site}${r.search ? " · search" : ""}`)); }
    ruleSel.value = pre ? pre.rule : "";
    syncSearch();
  }).catch(() => {});

  const checkMatch = debounce(async () => {
    const us = lines(urls.value);
    syncSearch();
    if (ruleSel.value) { match.replaceChildren("The selected rule will be used for all URLs."); return; }
    if (!us.length) { matched = []; syncSearch(); match.replaceChildren(); return; }
    const res = await Promise.all(us.slice(0, 6).map((u) => api(`/api/match?url=${encodeURIComponent(u)}`).catch(() => ({ rule: null }))));
    const found = res.filter((x) => x.rule);
    matched = found.map((x) => x.rule);
    syncSearch();
    if (!found.length) match.replaceChildren(h("span", { class: "pill err" }, h("i", { class: "dot" }), "no matching rule"), "Create a rule on the Learn page first, or pick a rule.");
    else match.replaceChildren(h("span", { class: "pill ok" }, h("i", { class: "dot" }), "rule found"),
      ...[...new Set(found.map((x) => x.rule))].map((n) => h("b", {}, n)),
      ...(found.length < res.length ? [h("span", {}, ` · no rule for ${res.length - found.length} URL(s)`)] : []));
  }, 350);
  urls.addEventListener("input", checkMatch);
  ruleSel.addEventListener("change", checkMatch);
  checkMatch();

  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    if (limitMode.value === "custom" && !(+limitNum.value >= 1)) { toast("Custom page limit must be at least 1", "err"); limitNum.focus(); return; }
    if (!searchBox.hidden && qMode.value === "custom" && !(+qNum.value >= 1)) { toast("Custom query limit must be at least 1", "err"); qNum.focus(); return; }
    const body = { urls: lines(urls.value), rule: ruleSel.value, max_pages: limitValue(), concurrency: conc.value,
      out: outName.value.trim(), show_browser: showBrowser.checked, ignore_robots: ignoreRobots.checked,
      queries: searchBox.hidden ? [] : lines(queries.value), max_queries: searchBox.hidden ? "default" : qValue() };
    store.set("run-form", { urls: urls.value, limit_mode: limitMode.value, limit_num: limitNum.value, concurrency: conc.value,
      queries: queries.value, q_mode: qMode.value, q_num: qNum.value });
    go.disabled = true;
    try {
      const j = await api("/api/run", { method: "POST", body });
      jobs.prepend(jobCard(j.id, renderJobResult));
      refreshBadge();
    } catch (err) { toast(err.message, "err"); }
    go.disabled = false;
  } },
    h("div", { class: "grid" },
      field("Pages", urls, "one URL per line; lines starting with # are skipped"),
      match,
      searchBox,
      h("div", { class: "grid cols-4" },
        field("Rule", ruleSel), field("Page limit", maxPages), field("Concurrent URLs", conc), field("Output file", outName)),
      h("details", { class: "adv" }, h("summary", {}, icon("chev"), "Advanced"),
        h("div", { class: "row", style: "gap:22px;margin-top:12px" },
          h("label", { class: "check" }, showBrowser, "Show the browser window"),
          h("label", { class: "check" }, ignoreRobots, "Ignore robots.txt"))),
      h("div", { class: "row end" }, go)));

  main.replaceChildren(
    pageHead("Run", "Runs a saved rule without an LLM on one or more pages, following all their pages."),
    form, jobs);
  urls.focus();
}

// ------------------------------------------------------------ page: Learn
const GOAL_EXAMPLES = ["link, name and price of all products", "link, title, author and price of all books",
  "title, link and date of all news items", "title, price and location of all listings", "all quotes, their authors and tags",
  "title and link of videos titled 'cat'", "name and link of all profiles"];

async function pageLearn() {
  const saved = store.get("learn-form", {});
  const url = h("input", { type: "url", class: "mono", placeholder: "https://site.com/category/...", value: saved.url || "" });
  const goal = h("textarea", { rows: 2, placeholder: "e.g. link, name and price of all products", value: saved.goal || "" });
  const name = h("input", { type: "text", class: "mono", placeholder: "automatic", value: "" });
  const also = h("textarea", { class: "mono", rows: 2, placeholder: "another page with the same layout (optional)" });
  const maxIter = h("input", { type: "number", min: 1, max: 12, value: 8 });
  const testPages = h("input", { type: "number", min: 1, max: 10, value: 3 });
  const model = h("select", {}, h("option", { value: "claude-haiku-5-5" }, "Haiku 5.5 (fast, cheap)"), h("option", { value: "claude-sonnet-5-5" }, "Sonnet 5.5 (stronger)"));
  const showBrowser = h("input", { type: "checkbox" });
  const ignoreRobots = h("input", { type: "checkbox" });
  const searchMode = h("select", {},
    h("option", { value: "auto" }, "Automatic (search if needed)"),
    h("option", { value: "on" }, "With search"),
    h("option", { value: "off" }, "No search"));
  searchMode.value = ["auto", "on", "off"].includes(saved.search) ? saved.search : "auto";
  const jobs = h("div", { class: "stack" });
  const go = h("button", { class: "btn primary", type: "submit" }, icon("star"), "Learn rule");
  const peek = h("button", { class: "btn", type: "button", onclick: async () => {
    if (!url.value.trim()) return toast("Enter a URL first", "err");
    try {
      const j = await api("/api/simplify", { method: "POST", body: { url: url.value.trim() } });
      jobs.prepend(jobCard(j.id, renderJobResult));
    } catch (e) { toast(e.message, "err"); }
  } }, icon("eye"), "Simplified view");

  const form = h("form", { class: "card", onsubmit: async (e) => {
    e.preventDefault();
    store.set("learn-form", { url: url.value, goal: goal.value, search: searchMode.value });
    go.disabled = true;
    try {
      const j = await api("/api/learn", { method: "POST", body: {
        url: url.value.trim(), goal: goal.value.trim(), name: name.value.trim(), also: lines(also.value),
        max_iter: maxIter.value, test_pages: testPages.value, model: model.value,
        show_browser: showBrowser.checked, ignore_robots: ignoreRobots.checked, search: searchMode.value } });
      jobs.prepend(jobCard(j.id, renderJobResult));
      refreshBadge();
      toast("Learning started — usually takes 1-2 minutes");
    } catch (err) { toast(err.message, "err"); }
    go.disabled = false;
  } },
    h("div", { class: "grid" },
      field("Sample page", url, "the page that lists the data, or the page with the search box"),
      field("Requested data", goal),
      h("div", { class: "chips" }, GOAL_EXAMPLES.map((g) => h("button", { type: "button", class: "chip", onclick: () => { goal.value = g; goal.focus(); } }, g))),
      h("div", { class: "grid cols-3" }, field("Rule name", name, "file name"),
        field("Search", searchMode, "the term is taken from the request; if none is named, everything is swept"),
        field("Extra sample pages", also, "the rule is tested on these too")),
      h("details", { class: "adv" }, h("summary", {}, icon("chev"), "Advanced"),
        h("div", { class: "grid cols-3" }, field("Max LLM calls", maxIter), field("Test pages", testPages), field("Starting model", model)),
        h("div", { class: "row", style: "gap:22px;margin-top:12px" },
          h("label", { class: "check" }, showBrowser, "Show the browser window"),
          h("label", { class: "check" }, ignoreRobots, "Ignore robots.txt"))),
      h("div", { class: "row between" }, peek, go)));

  main.replaceChildren(
    pageHead("Learn", "Looks at the page, then writes and tests a rule by trial and error. The LLM is used only at this stage; once the rule is saved, Run works without an LLM."),
    form, jobs);
  (url.value ? goal : url).focus();
}

// ------------------------------------------------------------ page: Rules
async function pageRules() {
  const search = h("input", { type: "search", placeholder: "Search: name, site, goal…", style: "max-width:300px" });
  const grid = h("div", { class: "rule-grid" });
  main.replaceChildren(
    pageHead("Rules", "Learned, fixed rules. Click a rule to edit its JSON.",
      search, h("a", { class: "btn primary", href: "#/learn" }, icon("star"), "New rule")),
    grid);
  let rules = [];
  try { ({ rules } = await api("/api/rules")); } catch (e) { toast(e.message, "err"); }
  function draw() {
    const q = search.value.toLowerCase().trim();
    const list = rules.filter((r) => !q || [r.name, r.site, r.goal, r.fields.join(" ")].join(" ").toLowerCase().includes(q));
    grid.replaceChildren(...list.map((r) => h("a", { class: "card rule-card", href: `#/rules/${encodeURIComponent(r.name)}` },
      h("h3", {}, h("span", {}, r.name), r.verified ? h("span", { class: "pill ok" }, "verified") : h("span", { class: "pill warn" }, "unverified")),
      h("div", { class: "site" }, r.site, r.learned_at ? ` · ${fmtTime(r.learned_at)}` : ""),
      h("div", { class: "goal" }, r.goal),
      h("div", { class: "chips" }, h("span", { class: "tag" }, r.fetch), h("span", { class: "tag" }, r.pagination), r.search ? h("span", { class: "tag" }, "search") : null),
      h("div", { class: "fields", title: r.fields.join(", ") }, r.fields.join(" · ")))));
    if (!list.length) grid.replaceChildren(h("div", { class: "card", style: "grid-column:1/-1" }, emptyState(rules.length ? "No rule matches the search." : "No rules yet. Create your first rule on the Learn page.")));
  }
  search.addEventListener("input", draw);
  draw();
}

function searchSummary(s) {
  const how = s.how === "url" ? h("code", {}, s.url_template) : h("span", {}, "form: ", h("code", {}, s.input ? s.input.value : "?"));
  const e = s.enum || {};
  const all = { empty: "empty search", wildcard: "wildcard search", options: `${(e.options || []).length} options`, prefix: `${e.min_chars || 1}-letter prefixes` }[e.method] || "?";
  const terms = s.terms === "given" ? `terms: ${(s.queries || []).join(", ") || "–"}` : `all: ${all}`;
  return [how, ` · ${terms} · at most ${s.max_queries === 0 ? "unlimited" : (s.max_queries ?? 200)} queries`];
}

async function pageRule(name) {
  let text;
  try { text = await (await fetch(`/api/rules/${encodeURIComponent(name)}`)).text(); }
  catch (e) { return toast(e.message, "err"); }
  let obj;
  try { obj = JSON.parse(text); } catch { obj = null; }
  if (!obj || obj.error) { main.replaceChildren(crumb("#/rules", "Rules"), h("div", { class: "card" }, emptyState("Rule not found."))); return; }

  const editor = h("textarea", { class: "mono editor", spellcheck: "false", value: JSON.stringify(obj, null, 2) });
  const errBox = h("div", {});
  const dirty = h("span", { class: "small muted" });
  editor.addEventListener("input", () => { dirty.textContent = "unsaved changes"; });
  editor.addEventListener("keydown", (e) => {
    if (e.key === "Tab") { e.preventDefault(); editor.setRangeText("  ", editor.selectionStart, editor.selectionEnd, "end"); }
    if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); save(); }
  });

  async function save() {
    errBox.replaceChildren();
    let body;
    try { body = JSON.parse(editor.value); } catch (e) { errBox.replaceChildren(h("div", { class: "err-box" }, "JSON error: " + e.message)); return; }
    try {
      let r;
      try { r = await api(`/api/rules/${encodeURIComponent(name)}`, { method: "PUT", body }); }
      catch (e) {
        if (e.status !== 409 || !confirm(e.message + "\n\nOverwrite it?")) throw e;
        r = await api(`/api/rules/${encodeURIComponent(name)}?overwrite=1`, { method: "PUT", body });
      }
      toast("Rule saved");
      dirty.textContent = "";
      if (r.name !== name) location.hash = `#/rules/${encodeURIComponent(r.name)}`;
    } catch (e) { errBox.replaceChildren(h("div", { class: "err-box" }, e.message)); }
  }
  function format() {
    try { editor.value = JSON.stringify(JSON.parse(editor.value), null, 2); errBox.replaceChildren(); }
    catch (e) { errBox.replaceChildren(h("div", { class: "err-box" }, "JSON error: " + e.message)); }
  }
  async function del() {
    if (!confirm(`Delete rule '${name}'? This cannot be undone.`)) return;
    try { await api(`/api/rules/${encodeURIComponent(name)}`, { method: "DELETE" }); toast("Rule deleted"); location.hash = "#/rules"; }
    catch (e) { toast(e.message, "err"); }
  }

  const m = obj.meta || {};
  const pg = obj.pagination || {};
  const ex = obj.expect || {};
  const kv = (k, v) => v == null || v === "" ? null : h("tr", {}, h("td", { class: "muted", style: "width:38%" }, k), h("td", { style: "word-break:break-all" }, v));
  const sel = (s) => s ? h("code", {}, `${s.kind}: ${s.value}`) : "–";

  main.replaceChildren(
    crumb("#/rules", "Rules"),
    pageHead(obj.name, obj.goal,
      m.sample_url ? h("button", { class: "btn primary", onclick: () => goRun(obj.search ? [] : [m.sample_url, ...(m.also || [])], obj.name) }, icon("play"), "Run") : null),
    h("div", { class: "split" },
      h("div", { class: "stack" },
        h("div", { class: "card" },
          h("h2", {}, "Summary"),
          h("div", { class: "table-wrap" }, h("table", {}, h("tbody", {},
            kv("Site", obj.site),
            kv("Status", m.verified ? statusPill("OK") : h("span", { class: "pill warn" }, "unverified")),
            kv("Fetch", obj.fetch === "http" ? "http (fast, no browser)" : "browser"),
            kv("Item selector", sel(obj.item)),
            kv("Pagination", [pg.type, pg.param ? ` ?${pg.param}=` : "", pg.selector ? " " : "", pg.selector ? sel(pg.selector) : "", pg.max_pages === 0 ? " · unlimited" : ` · at most ${pg.max_pages} pages`]),
            kv("URL pattern", h("code", {}, obj.url_pattern)),
            obj.search ? kv("Search", searchSummary(obj.search)) : null,
            kv("Expectations", `per page ≥${ex.min_items_per_page ?? 1}, total ≥${ex.min_total ?? 1}${ex.unique_by ? `, unique by: ${ex.unique_by}` : ""}`),
            kv("Sample page", m.sample_url ? h("a", { href: m.sample_url, target: "_blank", rel: "noreferrer" }, m.sample_url) : null),
            kv("Learned", m.learned_at ? `${fmtTime(m.learned_at)} · ${m.llm_calls ?? "?"} LLM calls` : null))))),
        h("div", { class: "card" },
          h("h2", {}, "Fields", h("span", { class: "sub" }, `${(obj.fields || []).length}`)),
          h("div", { class: "table-wrap" }, h("table", {},
            h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Selector"), h("th", {}, "Value"))),
            h("tbody", {}, (obj.fields || []).map((f) => h("tr", {},
              h("td", {}, h("b", {}, f.name), f.required ? h("span", { class: "muted" }, " *") : null),
              h("td", { style: "word-break:break-all" }, f.selector ? h("code", {}, f.selector.value) : h("span", { class: "muted" }, "the item itself")),
              h("td", {}, h("span", { class: "tag" }, f.attr || "text"), " ", (f.transforms || []).filter((t) => t !== "strip").map((t) => h("span", { class: "muted small" }, t + " ")))))))))),
      h("div", { class: "card" },
        h("div", { class: "row between", style: "margin-bottom:10px" },
          h("h2", { style: "margin:0" }, "JSON"), dirty),
        editor, errBox,
        h("div", { class: "row between", style: "margin-top:12px" },
          h("button", { class: "btn danger", onclick: del }, icon("trash"), "Delete"),
          h("div", { class: "row" },
            h("button", { class: "btn ghost", onclick: format }, icon("wand"), "Format"),
            h("button", { class: "btn ghost", onclick: () => copyText(editor.value) }, icon("copy"), "Copy"),
            h("button", { class: "btn primary", onclick: save }, icon("save"), "Save"))))));
}

// ------------------------------------------------------------ page: Results
async function pageOutputs() {
  const body = h("div", { class: "card", style: "padding:0;overflow:hidden" }, h("div", { class: "empty" }, h("span", { class: "spin" })));
  main.replaceChildren(pageHead("Results", "JSON outputs of runs (the output/ folder)."), body);
  let outputs = [];
  try { ({ outputs } = await api("/api/outputs")); } catch (e) { toast(e.message, "err"); }
  if (!outputs.length) { body.replaceChildren(emptyState("No results yet. Run a rule from the Run page.")); return; }
  body.replaceChildren(h("div", { class: "table-wrap", style: "border:0;border-radius:0" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "File"), h("th", {}, "Source"), h("th", {}, "Rule"), h("th", { class: "num" }, "Items"), h("th", {}, "Status"), h("th", {}, "Date"), h("th", { class: "num" }, "Size"))),
    h("tbody", {}, outputs.map((o) => h("tr", { class: "click", onclick: () => { location.hash = `#/outputs/${encodeURIComponent(o.name)}`; } },
      h("td", {}, h("b", { class: "mono" }, o.name)),
      h("td", {}, h("div", { class: "cell", style: "max-width:260px", title: (o.urls || []).join("\n") }, (o.urls || [])[0] || "–", (o.urls || []).length > 1 ? ` +${o.urls.length - 1}` : "")),
      h("td", {}, (o.rules || []).join(", ") || "–"),
      h("td", { class: "num" }, fmtNum(o.total_items)),
      h("td", {}, o.error ? statusPill("PROBLEM") : [...new Set(o.statuses || [])].map((s) => statusPill(s))),
      h("td", { class: "muted" }, fmtTime(o.mtime)),
      h("td", { class: "num muted" }, fmtSize(o.size))))))));
}

function cellValue(v) {
  if (v == null || v === "") return h("span", { class: "muted" }, "–");
  if (Array.isArray(v)) return v.map((x) => typeof x === "object" ? JSON.stringify(x) : String(x)).join(" · ");
  if (typeof v === "object") return JSON.stringify(v);
  if (typeof v === "string" && /^https?:\/\//.test(v)) return h("a", { href: v, target: "_blank", rel: "noreferrer", title: v }, v.replace(/^https?:\/\/(www\.)?/, ""));
  if (typeof v === "number") return v.toLocaleString("en-US");
  return String(v);
}

function toCSV(items, cols) {
  const esc = (v) => {
    if (v == null) return "";
    if (Array.isArray(v)) v = v.join(" | ");
    else if (typeof v === "object") v = JSON.stringify(v);
    if (typeof v === "string" && /^[=+\-@\t\r]/.test(v)) v = "'" + v; // Excel formula injection
    v = String(v);
    return /[";\n\r]/.test(v) ? `"${v.replace(/"/g, '""')}"` : v;
  };
  return "﻿" + [cols.join(";"), ...items.map((it) => cols.map((c) => esc(it[c])).join(";"))].join("\r\n");
}

async function pageOutput(name) {
  main.replaceChildren(crumb("#/outputs", "Results"), h("div", { class: "card" }, h("div", { class: "empty" }, h("span", { class: "spin" }))));
  let data;
  try { data = await api(`/api/outputs/${encodeURIComponent(name)}`); }
  catch (e) { main.replaceChildren(crumb("#/outputs", "Results"), h("div", { class: "card" }, emptyState(e.message))); return; }
  const results = data.results || [];
  const multi = results.length > 1;
  const all = results.flatMap((r, ri) => (r.items || []).map((it) => multi ? { _source: ri, ...it } : it));
  const cols = [...new Set(all.flatMap((it) => Object.keys(it)))].filter((c) => c !== "_source");
  const plainName = name.replace(/\.json$/, "");

  async function del() {
    if (!confirm(`Delete file '${name}'? This cannot be undone.`)) return;
    try { await api(`/api/outputs/${encodeURIComponent(name)}`, { method: "DELETE" }); toast("Deleted"); location.hash = "#/outputs"; }
    catch (e) { toast(e.message, "err"); }
  }

  // per-source summaries
  const resultCards = results.map((r, i) => {
    const st = r.status || (r.issues && r.issues.length ? "PROBLEM" : r.stop_reason === "max_pages" ? "AT_LIMIT" : "OK");
    const fills = Object.entries(r.fill_rates || {});
    return h("div", { class: "card" },
      h("div", { class: "job-head" }, statusPill(st), h("span", { class: "t", title: r.url }, multi ? `${i + 1}. ` : "", r.url),
        r.rule ? h("a", { class: "tag", href: `#/rules/${encodeURIComponent(r.rule)}` }, r.rule) : null),
      h("div", { class: "stats", style: "margin-top:14px" },
        h("div", { class: "stat" }, h("div", { class: "k" }, "Unique items"), h("div", { class: "v" }, fmtNum(r.total_unique ?? (r.items || []).length))),
        h("div", { class: "stat" }, h("div", { class: "k" }, "Pages / rounds"), h("div", { class: "v" }, fmtNum(r.rounds ?? (r.pages || []).length))),
        h("div", { class: "stat" }, h("div", { class: "k" }, "Fetch"), h("div", { class: "v", style: "font-size:17px;padding-top:4px" }, r.fetch || "–")),
        h("div", { class: "stat" }, h("div", { class: "k" }, "Stop reason"), h("div", { class: "v", style: "font-size:17px;padding-top:4px", title: r.stop_reason }, r.stop_reason || "–"))),
      fills.length ? h("div", { style: "margin-top:16px" }, h("div", { class: "small muted", style: "margin-bottom:8px;font-weight:550" }, "Field fill rates"),
        h("div", { class: "fill-list" }, fills.map(([k, v]) => [h("span", { class: "mono" }, k),
          h("div", { class: `bar ${v >= 0.9 ? "ok" : v >= 0.5 ? "warn" : "err"}` }, h("i", { style: `width:${Math.round(v * 100)}%` })),
          h("span", {}, `${Math.round(v * 100)}%`)]))) : null,
      (r.issues || []).length ? h("div", { class: "issues", style: "margin-top:12px" }, h("b", {}, "Issues"), h("ul", { class: "list-plain" }, r.issues.map((x) => h("li", {}, x)))) : null,
      (r.notes || []).length ? h("div", { class: "small muted", style: "margin-top:10px" }, h("ul", { class: "list-plain" }, r.notes.map((x) => h("li", {}, x)))) : null,
      (r.errors || []).length ? h("div", { class: "issues", style: "margin-top:10px" }, h("ul", { class: "list-plain" }, r.errors.slice(0, 5).map((x) => h("li", {}, x)))) : null);
  });

  // item table
  const PER = 100;
  let page = 0, sortCol = null, sortDir = 1;
  const search = h("input", { type: "search", placeholder: "Search items…", style: "max-width:260px" });
  const srcSel = multi ? h("select", { style: "max-width:220px" }, h("option", { value: "" }, "All sources"),
    results.map((r, i) => h("option", { value: i }, `${i + 1}. ${r.url.replace(/^https?:\/\/(www\.)?/, "").slice(0, 40)}`))) : null;
  const tableBox = h("div", {});
  const pager = h("div", { class: "pager" });
  const countLbl = h("span", { class: "small muted" });

  function filtered() {
    const q = search.value.toLowerCase().trim();
    let list = all;
    if (srcSel && srcSel.value !== "") list = list.filter((it) => it._source === +srcSel.value);
    if (q) list = list.filter((it) => JSON.stringify(it).toLowerCase().includes(q));
    if (sortCol) {
      list = [...list].sort((a, b) => {
        const x = a[sortCol], y = b[sortCol];
        if (x == null) return 1; if (y == null) return -1;
        return (typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y))) * sortDir;
      });
    }
    return list;
  }
  function draw() {
    const list = filtered();
    const pages = Math.max(1, Math.ceil(list.length / PER));
    page = Math.min(page, pages - 1);
    const slice = list.slice(page * PER, page * PER + PER);
    countLbl.textContent = `${fmtNum(list.length)} / ${fmtNum(all.length)} items`;
    if (!all.length) { tableBox.replaceChildren(emptyState("This file has no items.")); pager.replaceChildren(); return; }
    tableBox.replaceChildren(h("div", { class: "table-wrap", style: "max-height:70vh" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "#"), cols.map((c) => h("th", { style: "cursor:pointer", title: "Sort",
        onclick: () => { if (sortCol === c) sortDir = -sortDir; else { sortCol = c; sortDir = 1; } draw(); } },
        c, sortCol === c ? (sortDir > 0 ? " ↑" : " ↓") : "")))),
      h("tbody", {}, slice.map((it, i) => h("tr", {}, h("td", { class: "idx" }, page * PER + i + 1),
        cols.map((c) => h("td", { class: typeof it[c] === "number" ? "num" : "" }, h("div", { class: "cell", title: typeof it[c] === "string" ? it[c] : "" }, cellValue(it[c]))))))))));
    pager.replaceChildren(
      h("button", { class: "btn sm", disabled: page === 0, onclick: () => { page--; draw(); } }, "‹ Previous"),
      `${page + 1} / ${pages}`,
      h("button", { class: "btn sm", disabled: page >= pages - 1, onclick: () => { page++; draw(); } }, "Next ›"));
  }
  search.addEventListener("input", debounce(() => { page = 0; draw(); }, 200));
  srcSel && srcSel.addEventListener("change", () => { page = 0; draw(); });

  main.replaceChildren(
    crumb("#/outputs", "Results"),
    pageHead(name, `${fmtNum(data.total_items ?? all.length)} items · ${results.length} sources · ${fmtTime(data.created)}`,
      h("a", { class: "btn", href: `/api/outputs/${encodeURIComponent(name)}?download=1` }, icon("dl"), "JSON"),
      h("button", { class: "btn", onclick: () => download(plainName + ".csv", toCSV(filtered(), cols), "text/csv;charset=utf-8") }, icon("dl"), "CSV"),
      h("button", { class: "btn", onclick: () => copyText(JSON.stringify(filtered().map(({ _source, ...x }) => x), null, 2)) }, icon("copy"), "Copy"),
      h("button", { class: "btn ghost danger", onclick: del, title: "Delete" }, icon("trash"))),
    h("div", { class: "stack" }, resultCards,
      h("div", { class: "card" },
        h("div", { class: "row between", style: "margin-bottom:12px" },
          h("div", { class: "row" }, h("h2", { style: "margin:0" }, "Items"), countLbl),
          h("div", { class: "row" }, srcSel, search)),
        tableBox, pager)));
  draw();
}

// ------------------------------------------------------------ page: Jobs
async function pageJobs(openId) {
  const list = h("div", { class: "card", style: "padding:0;overflow:hidden" });
  const detail = h("div", {});
  main.replaceChildren(pageHead("Jobs", "Learn, run and simplify jobs started in this server session.",
    h("button", { class: "btn ghost", onclick: () => pageJobs(openId) }, icon("refresh"), "Refresh")), detail, list);
  if (openId) detail.append(jobCard(openId, renderJobResult), h("div", { style: "height:16px" }));
  let jobs = [];
  try { ({ jobs } = await api("/api/jobs")); } catch (e) { toast(e.message, "err"); }
  if (!jobs.length) { list.replaceChildren(emptyState("No jobs yet.", "refresh")); return; }
  const kindLbl = { learn: "Learn", run: "Run", simplify: "Simplify" };
  list.replaceChildren(h("div", { class: "table-wrap", style: "border:0;border-radius:0" }, h("table", {},
    h("thead", {}, h("tr", {}, h("th", {}, "Type"), h("th", {}, "Job"), h("th", {}, "Status"), h("th", {}, "Started"), h("th", { class: "num" }, "Duration"))),
    h("tbody", {}, jobs.map((j) => h("tr", { class: "click", style: j.id === openId ? "background:var(--accent-soft)" : "",
      onclick: () => { location.hash = `#/jobs/${j.id}`; } },
      h("td", {}, h("span", { class: "tag" }, kindLbl[j.kind] || j.kind)),
      h("td", {}, h("div", { class: "cell" }, j.title)),
      h("td", {}, statusPill(j.status)),
      h("td", { class: "muted" }, fmtTime(j.started)),
      h("td", { class: "num muted" }, fmtDur((j.finished || Date.now() / 1000) - j.started))))))));
}

// ------------------------------------------------------------ stop server
$("#powerBtn").addEventListener("click", async () => {
  let running = 0;
  try { running = (await api("/api/jobs")).jobs.filter((j) => j.status === "running").length; } catch {}
  const msg = running
    ? `${running} job(s) still running. Stopping the server cancels them and their partial results are not saved.\n\nStop anyway?`
    : "Stop the server? To start it again, use the launcher (desktop shortcut) or the 'python -m kazici web' command.";
  if (!confirm(msg)) return;
  try { await api("/api/shutdown", { method: "POST" }); } catch (e) { return toast(e.message, "err"); }
  const status = h("p", { class: "muted" }, h("span", { class: "spin" }), "  Shutting down…");
  document.body.append(h("div", { class: "stopped" }, h("div", { class: "card" },
    h("div", { html: '<svg viewBox="0 0 24 24"><path d="M12 3v8M6.4 6.4a8 8 0 1 0 11.2 0"/></svg>' }),
    h("h2", { style: "justify-content:center;margin-top:8px" }, "Stopping server"), status)));
  // poll until the server has really shut down
  for (let i = 0; i < 40; i++) {
    await new Promise((r) => setTimeout(r, 500));
    try { await fetch("/api/jobs", { cache: "no-store" }); } catch {
      status.replaceChildren("Server stopped. You can close this tab. To start it again, use the ",
        h("b", {}, "kazici"), " shortcut on the desktop.");
      return;
    }
  }
  status.textContent = "The server is still responding; jobs may be taking a while to shut down.";
});

// ------------------------------------------------------------ routing
const ROUTES = [
  [/^#\/run$/, () => pageRun(), "run"],
  [/^#\/learn$/, () => pageLearn(), "learn"],
  [/^#\/rules$/, () => pageRules(), "rules"],
  [/^#\/rules\/(.+)$/, (m) => pageRule(decodeURIComponent(m[1])), "rules"],
  [/^#\/outputs$/, () => pageOutputs(), "outputs"],
  [/^#\/outputs\/(.+)$/, (m) => pageOutput(decodeURIComponent(m[1])), "outputs"],
  [/^#\/jobs$/, () => pageJobs(), "jobs"],
  [/^#\/jobs\/(.+)$/, (m) => pageJobs(m[1]), "jobs"],
];
function route() {
  const hash = location.hash || "#/run";
  for (const [rx, fn, nav] of ROUTES) {
    const m = hash.match(rx);
    if (m) {
      document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === nav));
      window.scrollTo(0, 0);
      fn(m);
      return;
    }
  }
  location.hash = "#/run";
}
window.addEventListener("hashchange", route);
route();
refreshBadge();
