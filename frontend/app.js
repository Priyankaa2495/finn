/* FINN frontend — vanilla JS, no build step.
   Every string from the API is third-party or derived data: it is inserted via esc() or
   textContent only. No innerHTML with raw API text anywhere. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// ------------------------------------------------------------------ storage
const store = {
  get(k, d) { try { const v = localStorage.getItem("finn." + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("finn." + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

// Access: the server turns `/?key=…` into an httpOnly cookie and redirects to a clean URL.
// JS never sees or stores the key; same-origin fetches carry the cookie automatically.
try { localStorage.removeItem("finn.token"); } catch { /* older builds stored it; clear it */ }

const state = {
  tab: "brief",
  window: store.get("window", 24),
  price: store.get("price", 5),
  volume: store.get("volume", 2),
  brief: null,
  askCtrl: null,
};

// ------------------------------------------------------------------ helpers
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function safeUrl(u) {
  if (!u || typeof u !== "string") return null;     // new URL(null) would resolve to "/null"
  try { const x = new URL(u, location.href); return ["https:", "http:"].includes(x.protocol) ? x.href : null; } catch { return null; }
}
const inr = v => (v == null ? "—" : "₹" + Number(v).toLocaleString("en-IN", { maximumFractionDigits: v < 100 ? 1 : 0 }) + " Cr");
const pct = (v, signed = true) => v == null ? "—" : (signed && v > 0 ? "+" : "") + v.toFixed(1) + "%";
const cls = v => v == null ? "" : v >= 0 ? "up" : "down";
const timeFmt = new Intl.DateTimeFormat("en-IN", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Kolkata" });
const when = iso => { try { return timeFmt.format(new Date(iso)); } catch { return ""; } };

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, credentials: "same-origin" });
  let body = null;
  try { body = await res.json(); } catch { /* non-JSON */ }
  if (res.status === 401) {
    const err = new Error("This FINN is private. Open the access link you were sent (it ends in ?key=…).");
    err.status = 401;
    throw err;
  }
  if (!res.ok) {
    const detail = body && (typeof body.detail === "string" ? body.detail : "Request was rejected");
    const err = new Error(detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ------------------------------------------------------------------ tabs
function showTab(name) {
  stopSpeaking();                       // speech must never outlive the screen it describes
  stopListening();
  state.tab = name;
  for (const b of $$(".tabbar [role=tab]")) {
    const on = b.dataset.tab === name;
    b.setAttribute("aria-selected", String(on));
    $("#tab-" + b.dataset.tab).hidden = !on;
  }
  $("#listen").hidden = name !== "brief";
  if (name === "network" && !$("#net-body").dataset.loaded) loadNetwork();
  if (location.hash !== "#" + name) history.replaceState(null, "", "#" + name);
  window.scrollTo({ top: 0 });
}

// ------------------------------------------------------------------ Brief
function numsTable(rows) {
  const order = [["revenue", "Revenue"], ["ebitda", "EBITDA"], ["pbt", "PBT"], ["pat", "PAT"]];
  const body = order.filter(([k]) => rows && rows[k]).map(([k, label]) => {
    const r = rows[k];
    return `<tr><td>${label}</td><td>${inr(r.value)}</td><td class="${cls(r.qoq)}">${pct(r.qoq)}</td><td class="${cls(r.yoy)}">${pct(r.yoy)}</td></tr>`;
  }).join("");
  if (!body) return "";
  return `<div class="scroll-x"><table class="nums"><thead><tr><th></th><th>Value</th><th>QoQ</th><th>YoY</th></tr></thead><tbody>${body}</tbody></table></div>`;
}

function resultsBlock(rc) {
  if (!rc) return "";
  const primary = rc.source && rc.source.startsWith("Primary");
  const where = rc.page ? ` · p.${esc(rc.page)}` : "";
  const all = (rc.flags || []).filter(f => !f.startsWith("PDF not usable"));
  const checks = all.filter(f => f.includes("vs screener"));
  const notes = all.filter(f => !f.includes("vs screener")).map(f => `<li>${esc(f)}</li>`).join("");
  const empty = !rc.rows || !Object.keys(rc.rows).length;
  return `<div class="results">
    ${empty ? `<p class="summary">${esc(rc.source)}</p>` : numsTable(rc.rows)}
    <span class="prov ${primary ? "" : "fallback"}">${esc(rc.quarter)} · ${esc(rc.source)}${where}${rc.basis ? " · " + esc(rc.basis) : ""}</span>
    ${rc.unit_note && rc.unit_note.includes("converted") ? `<p class="fine">${esc(rc.unit_note)}</p>` : ""}
    ${notes ? `<ul class="flags">${notes}</ul>` : ""}
    ${checks.length ? `<details><summary>${checks.length} prior-period figure${checks.length > 1 ? "s" : ""} differ from screener</summary><ul class="flags">${checks.map(f => `<li>${esc(f)}</li>`).join("")}</ul></details>` : ""}
  </div>`;
}

function pathChips(names) {
  return `<div class="path">${names.map((n, i) => (i ? "<i>→</i>" : "") + `<span>${esc(n)}</span>`).join("")}</div>`;
}

function strengthBar(s) {
  const w = Math.max(4, Math.min(100, Math.round((s || 0) * 100)));
  return `<div class="bar" role="img" aria-label="Link strength ${w} of 100"><span data-w="${w}"></span></div>`;
}
// CSP forbids inline style attributes; widths are applied through the CSSOM after render.
function applyBars(root) { for (const s of $$(".bar > span[data-w]", root)) s.style.width = s.dataset.w + "%"; }

function networkBlock(n) {
  if (!n) return "";
  if (n.kind === "knows_person") {
    const via = n.path.length > 2 ? n.path[1] : null;
    return `<div class="net"><div><b>${via ? esc(via) + " can introduce you" : "You know " + esc(n.person)}</b>${n.confidence === "possible" ? " <span class='tag'>possible match</span>" : ""}</div>
      ${pathChips(n.path)}${strengthBar(n.strength)}
      <div class="fine">${esc(n.hops.slice(1).join(" · "))}</div>
      ${n.insider && n.insider.is_insider ? `<div class="caution">⚠ ${esc(n.insider.caution)}</div>` : ""}</div>`;
  }
  return `<div class="net"><div>Call <b>${esc(n.person)}</b></div>
    <div class="fine">${esc((n.why || []).join(" · "))}</div>
    ${n.insider && n.insider.is_insider ? `<div class="caution">⚠ ${esc(n.insider.caution)}</div>` : ""}</div>`;
}

function card(i) {
  const tier = i.tier === "ACT" ? "act" : i.tier === "KNOW" ? "know" : "";
  const url = safeUrl(i.url);
  const bio = i.bio && (i.bio.name || i.bio.profile) ? `<div class="bio"><b>${esc(i.bio.name || "Incoming")}</b>${i.bio.role ? ", " + esc(i.bio.role) : ""}${i.bio.profile ? `<details><summary>Profile from filing${i.bio.page ? " · p." + esc(i.bio.page) : ""}</summary><p>${esc(i.bio.profile)}</p></details>` : ""}</div>` : "";
  return `<article class="card ${tier}">
    <div class="meta"><span class="sym">${esc(i.symbol)}</span><span class="tag ${tier}">${esc(i.label)}</span>${i.demo ? '<span class="tag demo">DEMO</span>' : ""}<time datetime="${esc(i.filed_at)}">${esc(when(i.filed_at))}</time></div>
    <h3>${esc(i.subject)}</h3>
    ${i.summary ? `<p class="summary">${esc(i.summary)}</p>` : ""}
    ${resultsBlock(i.results_card)}${bio}${networkBlock(i.network)}
    <footer>
      <details><summary>Why it's here · ${esc(i.score)}</summary><ul class="reasons">${i.reasons.map(r => `<li>${esc(r)}</li>`).join("")}</ul></details>
      ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Filing PDF ↗</a>` : ""}
    </footer>
  </article>`;
}

function renderBrief(b) {
  const el = $("#brief-body");
  $("#asof").textContent = b.as_of ? "Filings as of " + when(b.as_of) : "No data yet";
  const alerts = b.alerts.length ? `<div class="alerts">${b.alerts.map(a => `<div class="alert"><span class="k">${esc(a.symbol)}</span><span>${esc(a.message.replace(a.symbol + " ", ""))}</span></div>`).join("")}</div>` : "";
  const section = (title, list) => list.length ? `<h2>${title} · ${list.length}</h2>${list.map(card).join("")}` : "";
  const warm = b.warmups.length ? `<h2>Natural reasons to reconnect</h2><div class="panel">${b.warmups.map(w => `<div class="person"><div class="who"><b>${esc(w.name)}</b><span class="sym">${esc(w.symbol)}</span></div><div class="why">${esc(w.message)}</div></div>`).join("")}</div>` : "";
  const routine = b.routine.length ? `<details class="panel routine"><summary>${b.routine_count} routine filings folded</summary><ul>${b.routine.map(r => `<li><span class="sym">${esc(r.symbol)}</span><span>${esc(r.subject)}</span></li>`).join("")}</ul></details>` : "";
  const nothing = !b.act.length && !b.know.length && !b.alerts.length
    ? `<div class="empty">Nothing material in this window. ${b.routine_count ? "Only routine paperwork." : ""}</div>` : "";
  el.innerHTML = alerts + section("Act on", b.act) + section("Worth knowing", b.know) + nothing + warm + routine;
  applyBars(el);
}

async function loadBrief() {
  const el = $("#brief-body");
  el.innerHTML = `<div class="loading"><span class="pulse-dots"><i></i><i></i><i></i></span><div class="status">Ranking today's filings…</div></div>`;
  try {
    const q = new URLSearchParams({ window: state.window, price: state.price, volume: state.volume });
    state.brief = await api("/api/brief?" + q);
    renderBrief(state.brief);
  } catch (e) {
    el.innerHTML = `<div class="empty">Couldn't load the Brief. ${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ Listen (TTS)
const synth = "speechSynthesis" in window ? window.speechSynthesis : null;
let speaking = false;

function stopSpeaking() {
  if (synth) synth.cancel();
  speaking = false;
  $("#listen").setAttribute("aria-pressed", "false");
}

function speakBrief() {
  if (!synth) { flash("Read-aloud isn't supported in this browser."); return; }
  if (speaking) { stopSpeaking(); return; }
  const lines = (state.brief && state.brief.speech) || [];
  if (!lines.length) { flash("Nothing to read in this window."); return; }
  synth.cancel();
  const voice = synth.getVoices().find(v => v.lang === "en-IN") || synth.getVoices().find(v => v.lang && v.lang.startsWith("en"));
  speaking = true;
  $("#listen").setAttribute("aria-pressed", "true");
  lines.forEach((text, idx) => {
    const u = new SpeechSynthesisUtterance(text.replace(/₹/g, "rupees "));
    if (voice) u.voice = voice;
    u.rate = 1.04;
    if (idx === lines.length - 1) u.onend = () => stopSpeaking();
    u.onerror = () => stopSpeaking();
    synth.speak(u);
  });
}

function flash(msg) {
  const el = state.tab === "ask" ? $("#ask-msg") : $("#asof");
  el.textContent = msg;
}

// ------------------------------------------------------------------ Ask
const STATUSES = ["Reading statements…", "Checking transcripts…", "Grading guidance against results…", "Building the facts pack…", "Matching network…"];

function loadingView() {
  const el = $("#ask-body");
  el.innerHTML = `<div class="loading" role="status" aria-live="polite"><span class="pulse-dots"><i></i><i></i><i></i></span><div class="status">${STATUSES[0]}</div><div class="elapsed">0s</div></div>`;
  const t0 = Date.now();
  let i = 0;
  const timer = setInterval(() => {
    i = Math.min(i + 1, STATUSES.length - 1);
    const s = $(".loading .status", el), e = $(".loading .elapsed", el);
    if (s) s.textContent = STATUSES[i];
    if (e) e.textContent = Math.round((Date.now() - t0) / 1000) + "s" + (Date.now() - t0 > 9000 ? " · first look at a new ticker takes 10–15s" : "");
  }, 2200);
  return () => clearInterval(timer);
}

const STATUS_LABEL = { met: "Met", beat: "Beat", missed: "Missed", on_track: "On track", tracking_below: "Tracking below",
  over_plan: "Over plan", under_plan: "Under plan", needs_judgement: "Needs judgement", pending: "Pending", no_data: "No data" };

function renderResearch(r) {
  const g = r.guidance;
  // One row per (year, metric): the latest statement is graded; earlier ones fold into a
  // single "how it moved" line. Needs-judgement items keep their own rows (never merged).
  const groups = new Map();
  for (const it of g.items) {
    const key = it.verdict.status === "needs_judgement" ? `${it.period}|${it.metric}|${it.sentence_id}` : `${it.period}|${it.metric}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);                       // API order: by call date within a group
  }
  const quote = it => `<blockquote class="quote">“${esc(it.quote)}”<cite>${esc(it.call)} concall, page ${esc(it.page)} · sentence #${esc(it.sentence_id)} · selected by ${esc(it.selected_by)}</cite></blockquote>`;
  const rowsByPeriod = {};
  for (const list of groups.values()) {
    const it = list[list.length - 1];
    const moves = g.drift.filter(d => d.period === it.period && d.metric === it.metric && d.direction !== "maintained");
    const moved = moves.length ? `<ul class="drift">${moves.map(d => `<li><span class="${esc(d.direction)}">${d.direction === "cut" ? "Cut" : "Raised"}</span> ${esc(d.from)} <span class="fine">(${esc(d.from_call)})</span> → ${esc(d.to)} <span class="fine">(${esc(d.to_call)})</span></li>`).join("")}</ul>` : "";
    (rowsByPeriod[it.period] = rowsByPeriod[it.period] || []).push(`
      <div class="promise">
        <div class="line"><span class="what">${esc(it.label)} ${esc(it.target)}</span><span class="verdict v-${esc(it.verdict.status)}">${esc(STATUS_LABEL[it.verdict.status] || it.verdict.status)}</span><span class="fine">${esc(it.call)}</span></div>
        <div class="actual">${esc(it.verdict.text)}</div>${moved}
        <details><summary>Management's words${list.length > 1 ? ` · ${list.length} statements` : ` · p.${esc(it.page)}`}</summary>${list.slice().reverse().map(quote).join("")}</details>
      </div>`);
  }
  const promises = Object.keys(rowsByPeriod).sort().reverse().map(p => `<h3>${esc(p)}</h3>` + rowsByPeriod[p].join("")).join("");
  const drift = "";

  const s = r.financial_snapshot;
  const t = r.trajectory || {};
  const last = r.annual && r.annual.length ? r.annual[r.annual.length - 1] : null;
  const kv = [
    t.revenue_cagr != null && ["Revenue CAGR " + esc(t.from) + "–" + esc(t.to), pct(t.revenue_cagr, false)],
    t.pat_cagr != null && ["PAT CAGR", pct(t.pat_cagr, false)],
    last && last.ebitda_margin != null && ["EBITDA margin " + esc(last.label), pct(last.ebitda_margin, false)],
    last && last.cfo_to_pat != null && ["CFO / PAT " + esc(last.label), last.cfo_to_pat.toFixed(2) + "x"],
    last && last.debt_to_equity != null && ["Debt / Equity", last.debt_to_equity.toFixed(2) + "x"],
    last && last.roce != null && ["ROCE", pct(last.roce, false)],
  ].filter(Boolean).map(([k, v]) => `<div><small>${k}</small><b>${v}</b></div>`).join("");

  const factText = Object.fromEntries(r.facts.map(f => [f.id, f.text]));
  const bbList = pts => pts.map(p => `<li>${esc(p.point)} <span class="cite" title="${esc(p.facts.map(id => id + ": " + (factText[id] || "")).join("\n"))}">${esc(p.facts.join(", "))}</span></li>`).join("");
  const net = (r.network || []).map(n => `<div class="person"><div class="who"><b>${esc(n.person)}</b></div><div class="why">${esc(n.why.join(" · "))}</div>${n.insider.is_insider ? `<div class="caution">⚠ ${esc(n.insider.caution)}</div>` : ""}</div>`).join("");
  const sources = r.sources.map(x => { const u = safeUrl(x.url); return `<li>${u ? `<a href="${esc(u)}" target="_blank" rel="noopener noreferrer">${esc(x.kind)}</a>` : esc(x.kind)}${x.pages ? " · " + esc(x.pages) + " pp" : ""}</li>`; }).join("");

  $("#ask-body").innerHTML = `
    <div class="rhead"><h1>${esc(r.symbol)}</h1>${r.data.synthetic ? '<span class="tag demo">SYNTHETIC DEMO</span>' : `<span class="tag">${esc(r.data.source)}</span>`}</div>
    <h2>Did management deliver?</h2>
    <div class="panel">${promises || '<p class="fine">No quantified guidance found in the last four transcripts.</p>'}
      ${drift ? `<h3>How guidance moved</h3><ul class="drift">${drift}</ul>` : ""}
      <p class="fine">Mode: ${g.mode === "model" ? "model picked sentence IDs; every number checked in code" : "rules only (no model)"} · ${esc(g.candidates)} of ${esc(g.sentences)} sentences considered${g.rejected.length ? " · " + g.rejected.length + " model picks rejected" : ""}</p>
    </div>
    ${s ? `<h2>${esc(s.quarter)} snapshot</h2><div class="panel">${numsTable(s.rows)}<p class="fine">vs ${esc(s.prev || "—")} (QoQ) and ${esc(s.year_ago || "—")} (YoY). Screener tables, ₹ crore.</p></div>` : ""}
    ${kv ? `<h2>Trajectory & balance sheet</h2><div class="kv">${kv}</div>` : ""}
    <h2>Bull vs bear</h2>
    <div class="bb"><div class="panel bull"><h3>Bull</h3><ul>${bbList(r.bull_bear.bull) || "<li class='fine'>No supporting facts.</li>"}</ul></div>
      <div class="panel bear"><h3>Bear</h3><ul>${bbList(r.bull_bear.bear) || "<li class='fine'>No opposing facts.</li>"}</ul></div></div>
    <p class="fine">Every point cites numbered facts computed in code (${esc(r.bull_bear.mode)} mode). Tap a citation to see it.</p>
    ${net ? `<h2>Who you could call</h2><div class="panel">${net}</div>` : ""}
    <h2>Primary sources</h2><ul class="sources">${sources}</ul>`;
}

async function runAsk(raw) {
  const q = (raw || "").trim();
  const msg = $("#ask-msg");
  msg.className = "msg";
  if (!q) { msg.textContent = "Type a ticker or company name."; return; }
  if (state.askCtrl) state.askCtrl.abort();
  let symbol = q.toUpperCase();
  if (!/^[A-Z0-9&-]{1,20}$/.test(symbol)) {
    try {
      const r = await api("/api/resolve?q=" + encodeURIComponent(q));
      if (!r.symbol) { msg.className = "msg err"; msg.textContent = `Couldn't match “${q}” to an NSE ticker. Try the symbol, e.g. DIXON.`; return; }
      symbol = r.symbol;
    } catch (e) { msg.className = "msg err"; msg.textContent = e.message; return; }
  }
  $("#ask-input").value = symbol;
  msg.textContent = "";
  store.set("recent", [symbol, ...store.get("recent", []).filter(s => s !== symbol)].slice(0, 5));
  renderSuggest();
  const ctrl = new AbortController();
  state.askCtrl = ctrl;
  const stop = loadingView();
  try {
    const r = await api("/api/research/" + encodeURIComponent(symbol), { signal: ctrl.signal });
    if (ctrl.signal.aborted) return;
    renderResearch(r);
  } catch (e) {
    if (e.name === "AbortError") return;
    $("#ask-body").innerHTML = "";
    msg.className = "msg err";
    msg.textContent = e.message;
  } finally {
    stop();
  }
}

function renderSuggest() {
  const recent = store.get("recent", []);
  const picks = [...new Set([...recent, "DEMOCHEM", "DIXON", "KAYNES", "DEEPAKNTR"])].slice(0, 6);
  $("#ask-suggest").innerHTML = picks.map(s => `<button type="button" class="chip" data-sym="${esc(s)}">${esc(s)}</button>`).join("");
}

// ------------------------------------------------------------------ Ask by voice
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recog = null;

function stopListening() {
  if (recog) { try { recog.abort(); } catch { /* already stopped */ } recog = null; }
  $("#mic").classList.remove("listening");
}

function listen() {
  const msg = $("#ask-msg");
  msg.className = "msg";
  if (recog) { stopListening(); return; }
  if (!Recognition) { msg.textContent = "Voice input isn't available in this browser. Type a ticker instead."; return; }
  if (!window.isSecureContext) { msg.className = "msg err"; msg.textContent = "Voice needs a secure (https) page. Type a ticker instead."; return; }
  recog = new Recognition();
  recog.lang = "en-IN";
  recog.interimResults = false;
  recog.maxAlternatives = 1;
  $("#mic").classList.add("listening");
  msg.textContent = "Listening… say a company, e.g. “Dixon”.";
  recog.onresult = ev => {
    const said = ev.results[0][0].transcript.replace(/[.?!]$/, "");
    stopListening();
    $("#ask-input").value = said;
    runAsk(said);
  };
  recog.onerror = ev => {
    stopListening();
    msg.className = "msg err";
    msg.textContent = ({
      "not-allowed": "Microphone access is blocked. Allow it in your browser's site settings, or type a ticker.",
      "service-not-allowed": "Microphone access is blocked. Allow it in your browser's site settings, or type a ticker.",
      "no-speech": "Didn't catch that. Tap the mic and try again.",
      "audio-capture": "No microphone found. Type a ticker instead.",
      "network": "Voice recognition needs a connection. Type a ticker instead.",
      "aborted": "",
    })[ev.error] ?? "Voice input stopped. Type a ticker instead.";
  };
  recog.onend = () => { $("#mic").classList.remove("listening"); recog = null; };
  try { recog.start(); } catch { stopListening(); msg.textContent = "Couldn't start the microphone. Type a ticker instead."; }
}

// ------------------------------------------------------------------ Network
function renderNetwork(o, el) {
  const calls = o.who_to_call.map(c => `<div class="person"><div class="who"><b>${esc(c.person)}</b><span class="sym">${esc(c.symbol)}</span></div><div class="why">${esc(c.why.join(" · "))}</div>${c.insider.is_insider ? `<div class="caution">⚠ ${esc(c.insider.caution)}</div>` : ""}</div>`).join("");
  const paths = o.intro_paths.map(p => `<div class="person"><div class="who"><b>${esc(p.person)}</b><span class="fine">${esc(p.role || "")}</span></div>${pathChips(p.path)}${strengthBar(p.strength)}<div class="fine">${esc(p.hops.slice(1).join(" · "))}</div></div>`).join("");
  const warm = o.warmups.map(w => `<div class="person"><div class="who"><b>${esc(w.name)}</b><span class="sym">${esc(w.symbol)}</span></div><div class="why">${esc(w.message)}</div></div>`).join("");
  const blind = o.blind_spots.map(b => `<div class="blind"><span><span class="sym">${esc(b.symbol)}</span> ${esc(b.sector)}</span><span class="fine">${esc(b.weight_pct)}% of book</span></div>`).join("");
  const exp = o.expertise.map(e => `<div class="person"><div class="who"><b>${esc(e.theme)}</b></div><div class="why">${esc(e.people.map(p => p.name).join(", "))}</div></div>`).join("");
  el.innerHTML = `
    <p class="fine lead">${esc(o.contacts)} contacts · matched locally, never sent to a model.</p>
    ${blind ? `<h2>Blind spots · ${o.blind_spots.length}</h2><div class="panel">${blind}<p class="fine">Holdings where nobody you know has worked at the company or a peer.</p></div>` : ""}
    ${warm ? `<h2>Warm up</h2><div class="panel">${warm}</div>` : ""}
    ${paths ? `<h2>Second-degree reach</h2><div class="panel">${paths}</div>` : ""}
    <h2>Who to call, by holding</h2><div class="panel">${calls || '<p class="fine">No links yet.</p>'}</div>
    ${exp ? `<h2>Expertise map</h2><div class="panel">${exp}</div>` : ""}`;
  applyBars(el);
}

async function loadNetwork() {
  const el = $("#net-body");
  el.innerHTML = `<div class="loading"><span class="pulse-dots"><i></i><i></i><i></i></span><div class="status">Mapping your network…</div></div>`;
  try {
    renderNetwork(await api("/api/network/overview"), el);
    el.dataset.loaded = "1";
  } catch (e) {
    el.innerHTML = `<div class="empty">Couldn't load the network. ${esc(e.message)}</div>`;
  }
}

async function importCsv(ev) {
  ev.preventDefault();
  const msg = $("#import-msg");
  const f = $("#import-file").files[0];
  msg.className = "msg";
  if (!f) { msg.textContent = "Choose your Connections.csv first."; return; }
  if (f.size > 5 * 1024 * 1024) { msg.className = "msg err"; msg.textContent = "That file is over 5 MB."; return; }
  const fd = new FormData();
  fd.append("file", f);
  msg.textContent = "Importing…";
  try {
    const r = await api("/api/network/import", { method: "POST", body: fd });
    msg.textContent = `Added ${r.report.added} contacts · ${r.report.emails_dropped} email addresses dropped. ${r.note}`;
    renderNetwork(r.overview, $("#net-body"));
  } catch (e) {
    msg.className = "msg err";
    msg.textContent = e.message;
  }
}

// ------------------------------------------------------------------ settings
function syncSliders() {
  $("#price").value = state.price; $("#volume").value = state.volume;
  $("#price-out").textContent = "±" + Number(state.price).toFixed(1) + "%";
  $("#volume-out").textContent = Number(state.volume).toFixed(1) + "×";
}

// ------------------------------------------------------------------ boot
document.addEventListener("DOMContentLoaded", () => {
  for (const b of $$(".tabbar [role=tab]")) b.addEventListener("click", () => showTab(b.dataset.tab));
  for (const b of $$(".seg button")) {
    b.setAttribute("aria-pressed", String(Number(b.dataset.window) === state.window));
    b.addEventListener("click", () => {
      stopSpeaking();
      state.window = Number(b.dataset.window); store.set("window", state.window);
      $$(".seg button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      loadBrief();
    });
  }
  $("#listen").addEventListener("click", speakBrief);
  if (!synth) $("#listen").title = "Read-aloud isn't supported in this browser";

  const dlg = $("#settings");
  $("#open-settings").addEventListener("click", () => { syncSliders(); dlg.showModal(); });
  $("#price").addEventListener("input", e => { $("#price-out").textContent = "±" + Number(e.target.value).toFixed(1) + "%"; });
  $("#volume").addEventListener("input", e => { $("#volume-out").textContent = Number(e.target.value).toFixed(1) + "×"; });
  dlg.addEventListener("close", () => {
    if (dlg.returnValue !== "save") return;
    state.price = Number($("#price").value); state.volume = Number($("#volume").value);
    store.set("price", state.price); store.set("volume", state.volume);
    stopSpeaking(); loadBrief();
  });

  $("#ask-form").addEventListener("submit", e => { e.preventDefault(); stopListening(); runAsk($("#ask-input").value); });
  $("#mic").addEventListener("click", listen);
  $("#ask-suggest").addEventListener("click", e => { const b = e.target.closest("[data-sym]"); if (b) runAsk(b.dataset.sym); });
  $("#import-form").addEventListener("submit", importCsv);

  document.addEventListener("visibilitychange", () => { if (document.hidden) { stopSpeaking(); stopListening(); } });
  window.addEventListener("pagehide", stopSpeaking);

  api("/api/meta").then(m => {
    const el = $("#mode");
    el.textContent = m.mode === "zero-key" ? "Rules mode" : "Model + rules";
    el.title = m.mode === "zero-key" ? "No model key: every view is computed by deterministic rules" : `Deep-tier fallbacks: ${m.deep_fallbacks}`;
    el.hidden = false;
  }).catch(() => {});

  renderSuggest();
  const start = (location.hash || "#brief").slice(1);
  showTab(["brief", "ask", "network"].includes(start) ? start : "brief");
  loadBrief();
});
