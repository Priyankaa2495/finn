// FINN — mobile-first client. No framework, no build step: three views, one sheet.
const $app = document.getElementById("app");
const $sheet = document.getElementById("sheet");
const $scrim = document.getElementById("scrim");
const S = { hours: 24, meta: null, brief: null, lastAsk: null, theme: null, overview: null };
const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;  // voice Ask where supported

// ---------- utils ---------------------------------------------------------------
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) throw new Error("locked");
  if (!r.ok) throw new Error(`Request failed (${r.status})`);
  return r.json();
}
const cr = (v) => (v == null ? "—" : `₹${Math.round(v).toLocaleString("en-IN")} cr`);
const num = (v, d = 1) => (v == null ? "—" : Number(v).toLocaleString("en-IN", { maximumFractionDigits: d }));
const pct = (v) => (v == null ? `<span class="note">—</span>` : `<span class="${v >= 0 ? "up" : "down"}">${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(1)}%</span>`);
const initials = (n) => n.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
function ago(ts) {
  const h = (Date.now() - new Date(ts).getTime()) / 36e5;
  if (h < 1) return `${Math.max(1, Math.round(h * 60))}m ago`;
  if (h < 24) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}
function toast(msg) {
  const t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
  document.body.appendChild(t); setTimeout(() => t.remove(), 2200);
}
function spark(vals, w = 144, h = 30) {
  const v = vals.filter((x) => x != null); if (v.length < 2) return "";
  const lo = Math.min(...v), hi = Math.max(...v), sx = w / (v.length - 1), sy = (hi - lo) || 1;
  const pts = v.map((x, i) => `${(i * sx).toFixed(1)},${(h - 2 - ((x - lo) / sy) * (h - 4)).toFixed(1)}`).join(" ");
  const up = v[v.length - 1] >= v[v.length - 2];
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--${up ? "up" : "down"})" stroke-width="1.6"/></svg>`;
}

// ---------- sheet ---------------------------------------------------------------
function openSheet(html) {
  $sheet.innerHTML = `<div class="grab"></div>${html}`;
  $sheet.classList.add("on"); $scrim.classList.add("on");
  $sheet.querySelector("button, a, input")?.focus({ preventScroll: true });
}
function closeSheet() { $sheet.classList.remove("on"); $scrim.classList.remove("on"); }
$scrim.onclick = closeSheet;
document.addEventListener("keydown", (e) => e.key === "Escape" && closeSheet());

// ---------- shared pieces -------------------------------------------------------
const PALETTE = ["var(--pen)", "#8C98AB", "var(--up)", "var(--warn)", "#B7C1CF"];
function careerStrip(exp) {
  const now = new Date().getFullYear();
  const segs = exp.filter((e) => e.from).map((e) => ({ ...e, yrs: Math.max(1, (e.to || now) - e.from) }));
  if (!segs.length) return "";
  const tot = segs.reduce((a, s) => a + s.yrs, 0);
  return `<div class="career" aria-label="Career">
    <div class="bar">${segs.map((s, i) => `<span style="width:${(s.yrs / tot) * 100}%;background:${PALETTE[i % 5]}"></span>`).join("")}</div>
    <div class="legend">${segs.map((s, i) => `<span><i style="background:${PALETTE[i % 5]}"></i>${esc(s.role)}, ${esc(s.org)} ${s.from}–${s.to || "now"}</span>`).join("")}</div></div>`;
}

function wayIn(name, p) {
  if (p.direct) return `<div class="wayin"><div class="label">Already in your network</div>
    <div class="reason">${esc(p.direct.name)}, ${esc(p.direct.how_met)}</div></div>`;
  const best = p.paths?.[0];
  if (!best) return `<div class="wayin"><div class="label">Your way in</div><div class="reason">No one in your network overlaps with ${esc(name)} yet.</div></div>`;
  const tgt = `<div class="node target"><div class="dot">${esc(initials(name))}</div><span>${esc(name.split(" ")[0])}</span></div>`;
  const me = `<div class="node me"><div class="dot">You</div><span>&nbsp;</span></div>`;
  const path = best.via
    ? `${me}<div class="edge"></div><div class="node"><div class="dot">${esc(initials(best.via.name))}</div><span>${esc(best.via.name.split(" ")[0])}</span></div><div class="edge"></div>${tgt}`
    : `${me}<div class="edge" style="grid-column: span 3"></div>${tgt}`;
  const alt = p.paths.slice(1, 3).map((x) => x.via ? `${esc(x.via.name)} (${esc(x.link.label)})` : esc(x.why)).join("; ");
  return `<div class="wayin draw"><div class="label">Your way in</div>
    <div class="path" role="img" aria-label="${esc(best.why)}">${path}</div>
    <div class="reason">${esc(best.why)}.</div>
    ${alt ? `<div class="alt">Also: ${alt}</div>` : ""}
    ${best.via ? `<div class="row-actions"><button class="btn small" data-draft="${esc(best.via.id)}" data-reason="${esc(`Want your read on ${name}, who ${best.link.label}`)}">Message ${esc(best.via.name.split(" ")[0])}</button></div>` : ""}
  </div>`;
}

function peopleList(people, { compact = false } = {}) {
  if (!people?.length) return `<p class="note">No one in your network is close to this yet.</p>`;
  return `<div class="people">${people.map((p) => `
    <button class="person" data-person="${esc(p.id)}">
      <span class="avatar">${esc(initials(p.name))}</span>
      <span><span class="nm">${esc(p.name)}</span>
        <span class="w">${esc(p.why || p.headline)}</span>
        ${p.evidence && !compact ? `<span class="w" style="display:block">${esc(p.evidence.join("; "))}</span>` : ""}
        <span class="warmth">Last spoke ${esc(p.last_spoke || "—")}</span>
        ${p.caution ? `<span class="caution">${esc(p.caution)}</span>` : ""}
      </span></button>`).join("")}</div>`;
}

function resultsGrid(r) {
  if (!r) return `<p class="note">Numbers will appear once the quarterly table updates.</p>`;
  if (r.pending) return `<p class="note">${esc(r.note)}</p>`;
  return `<div class="rgrid">
    <div class="h">${esc(r.quarter)}</div><div class="h num">Value</div><div class="h num">QoQ</div><div class="h num">YoY</div>
    ${r.metrics.map((m) => `<div>${esc(m.label)}</div><div class="num">${cr(m.value)}</div><div class="num">${pct(m.qoq)}</div><div class="num">${pct(m.yoy)}</div>`).join("")}
  </div>
  ${r.ebitda_margin != null ? `<p class="note">EBITDA margin ${r.ebitda_margin}%. ${esc(r.metrics.find((m) => m.note)?.note || "")}</p>` : ""}
  ${r.verdict ? `<p style="color:var(--ink);margin-top:8px">${esc(r.verdict)}</p>` : ""}
  ${r.cross_check ? `<p class="note">${esc(r.cross_check)}</p>` : ""}
  <p class="note">${r.synthetic ? "Sample numbers (offline mode). " : ""}Source: ${esc(r.source)}, ${esc(r.basis)}.</p>`;
}

// ---------- BRIEF (push) --------------------------------------------------------
const WINDOWS = [[6, "6h"], [24, "24h"], [72, "3d"], [168, "7d"]];
const winLabel = (h) => (h < 24 ? `${h} hours` : h === 24 ? "24 hours" : `${h / 24} days`);

function actCard(c) {
  let body = "";
  if (c.person) {
    const prof = c.person.profile;
    body += careerStrip(prof.experience || []);
    body += wayIn(prof.name, c.person);
    if (c.person.questions?.length)
      body += `<div class="whybox" style="color:var(--ink-2);margin-top:12px"><b>Worth asking</b></div><ul class="qs">${c.person.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>`;
  }
  if (c.category === "results") body += resultsGrid(c.results);
  if (c.call?.length) body += `<div class="whybox" style="margin-top:14px;color:var(--pen);font-weight:700">Who can give you context</div>${peopleList(c.call, { compact: true })}`;
  return `<article class="card">
    <div class="meta"><span class="tick">${esc(c.symbol)}</span><span class="chip">${esc(c.category_label)}</span><span>${ago(c.ts)}</span>${c.demo ? `<span class="chip demo" title="Synthetic showcase scenario">Demo</span>` : ""}</div>
    <h3>${esc(c.title)}</h3>
    ${c.summary && !c.person && c.category !== "results" && !c.summary.toLowerCase().includes(c.title.replace(/…$/, "").toLowerCase().slice(-40)) ? `<p>${esc(c.summary)}</p>` : ""}
    ${body}
    <div class="foot">
      <button class="linkish" data-why="${esc(c.id)}">Why this is here</button>
      ${c.url ? `<a class="linkish" href="${esc(c.url)}" target="_blank" rel="noopener">Open filing</a>` : ""}
    </div>
    <div class="whybox" id="why-${esc(c.id)}" hidden>Priority ${c.score}/100: ${esc(c.category_label.toLowerCase())}${c.why.length ? ", " + esc(c.why.join(", ")) : ""}. Classified by ${esc(c.cls_method)}.</div>
  </article>`;
}

async function renderBrief() {
  $app.innerHTML = `<div class="loading">Reading the filings…</div>`;
  const [meta, b] = await Promise.all([S.meta || api("/api/meta"), api(`/api/brief?hours=${S.hours}`)]);
  S.meta = meta; S.brief = b;
  const n = b.counts.act;
  const lede = n === 0 ? "Nothing needs you right now." : n === 1 ? "One thing needs you." : `${["", "", "Two", "Three", "Four", "Five", "Six", "Seven"][n] || n} things need you.`;
  const holdings = meta.portfolio.holdings.length;
  const notes = [];
  if (meta.data.live_filings === 0) notes.push("Offline mode: the feed below is synthetic and labelled Demo.");
  else if (meta.data.demo_mode) notes.push("Items marked Demo are synthetic scenarios mixed into the live feed.");
  if (meta.data.prices_synthetic) notes.push("Prices are simulated.");
  $app.innerHTML = `
    <div class="top"><div class="brand">FINN<small>as of ${new Date(b.as_of).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" })}</small></div>
      <div class="top-actions">${"speechSynthesis" in window ? `<button class="icon-btn" id="listen" aria-pressed="false" aria-label="Listen to the brief" title="Listen to the brief"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 10v4h4l5 4V6L8 10H4z"/><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12"/></svg></button>` : ""}
      <button class="icon-btn" id="settings" aria-label="Alert settings"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/></svg></button></div></div>
    <h1 class="lede">${lede}</h1>
    <p class="sub">${b.counts.total} filings across your ${holdings} holdings in the last ${winLabel(b.window_hours)}. ${b.counts.know} worth knowing, ${b.counts.routine} routine ${b.counts.routine === 1 ? "one" : "ones"} folded away.</p>
    <div class="seg" role="group" aria-label="Time window">${WINDOWS.map(([h, l]) => `<button data-hours="${h}" aria-pressed="${h === S.hours}">${l}</button>`).join("")}</div>
    ${notes.length ? `<div class="banner">${esc(notes.join(" "))}</div>` : ""}

    ${b.alerts.length ? `<h2>Unusual moves <span class="n">by your thresholds</span></h2>
      <div class="moves">${b.alerts.map((a) => `<div class="move"><div><b>${esc(a.symbol)}</b> ${pct(a.move_pct)} <span class="n">${esc(new Date(a.date).toLocaleDateString("en-IN", { day: "numeric", month: "short" }))}</span></div>${spark(a.spark)}<div class="why">${esc(a.reasons.join("; "))}</div></div>`).join("")}</div>`
      : `<p class="note">No unusual moves in this window by your triggers (±${esc(meta.settings.price_move_pct)}% on the day, ${esc(meta.settings.volume_multiple)}× volume). Tune them with the sliders icon.</p>`}

    ${b.act.length ? `<h2>Needs you <span class="n">${b.act.length}</span></h2>${b.act.map(actCard).join("")}` : ""}

    ${b.know.length ? `<h2>Worth knowing <span class="n">${b.know.length}</span></h2><div class="rows">${b.know.map((c) => `
      <div class="rowi"><div class="meta"><span class="tick">${esc(c.symbol)}</span><span class="chip">${esc(c.category_label)}</span><span>${ago(c.ts)}</span>${c.demo ? `<span class="chip demo">Demo</span>` : ""}</div>
      <div class="t">${esc(c.title)}</div>${c.call?.length ? `<div class="s">Ask ${esc(c.call[0].name)}: ${esc(c.call[0].why)}</div>` : ""}</div>`).join("")}</div>` : ""}

    ${b.routine.total ? `<details class="fold"><summary>${b.routine.total} routine filings</summary>
      ${b.routine.groups.map((g) => g.items.map((c) => `<div class="rt"><b>${esc(c.symbol)}</b> ${esc(g.label)}: ${esc(c.title)}</div>`).join("")).join("")}</details>` : ""}

    ${b.warmups.length ? `<h2>Reasons to reconnect <span class="n">from today's news</span></h2>${peopleList(b.warmups.slice(0, 3).map((w) => ({ ...w, why: w.reason })))}` : ""}
  `;
  $app.querySelectorAll("[data-hours]").forEach((el) => (el.onclick = () => { S.hours = +el.dataset.hours; renderBrief(); }));
  $app.querySelectorAll("[data-why]").forEach((el) => (el.onclick = () => { const w = document.getElementById(`why-${el.dataset.why}`); w.hidden = !w.hidden; }));
  document.getElementById("settings").onclick = openSettings;
  const listen = document.getElementById("listen");
  if (listen) listen.onclick = () => toggleSpeech(listen, briefScript(b, lede));
}

// ---------- voice ----------------------------------------------------------------
// The brief read aloud, for the commute: the same ranking, said in order of what matters.
// Built from the cards (no model), so it never says anything the screen doesn't.
function briefScript(b, lede) {
  const say = [lede];
  b.act.slice(0, 5).forEach((c, i) => {
    let line = `${i === 0 ? "First" : i === b.act.length - 1 || i === 4 ? "Finally" : "Next"}, ${c.company || c.symbol}: ${c.title.replace(/…$/, "")}.`;
    if (c.demo) line += " This one is a demo scenario.";
    if (c.call?.length) line += ` ${c.call[0].name} is best placed to explain it.`;
    else if (c.person?.paths?.length) line += " You have a route to them through your network.";
    say.push(line);
  });
  if (b.act.length > 5) say.push(`${b.act.length - 5} more need you, on screen.`);
  if (b.alerts.length) say.push("Unusual moves: " + b.alerts.slice(0, 3).map((a) => `${a.symbol} ${a.move_pct >= 0 ? "up" : "down"} ${Math.abs(a.move_pct).toFixed(1)} percent`).join("; ") + ".");
  if (b.know.length) say.push(`${b.know.length} more worth knowing, on screen.`);
  if (b.routine.total) say.push(`${b.routine.total} routine ${b.routine.total === 1 ? "filing" : "filings"} folded away.`);
  return say.join(" ");
}

function toggleSpeech(btn, text) {
  const synth = window.speechSynthesis;
  if (synth.speaking) { synth.cancel(); btn.setAttribute("aria-pressed", "false"); return; }
  const u = new SpeechSynthesisUtterance(text);
  u.lang = "en-IN"; u.rate = 1.05;
  u.onend = u.onerror = () => btn.setAttribute("aria-pressed", "false");
  btn.setAttribute("aria-pressed", "true");
  synth.speak(u);
}

// ---------- settings ------------------------------------------------------------
const CATS = ["results", "M&A", "management_change", "litigation", "fund_raise", "capex", "pledging", "credit_rating", "insider_trading", "press_release", "dividend"];
const CAT_LABEL = { results: "Results", "M&A": "M&A", management_change: "Management", litigation: "Litigation", fund_raise: "Fund raise", capex: "Capex", pledging: "Pledging", credit_rating: "Credit rating", insider_trading: "Insider trades", press_release: "Press releases", dividend: "Dividends" };
function openSettings() {
  const s = { ...S.meta.settings };
  const draw = () => openSheet(`<h3>What counts as unusual</h3><p class="note">FINN watches your holdings after every close and only interrupts you past these lines.</p>
    <label for="pm">Price move: ±${s.price_move_pct}% in a day</label><input id="pm" type="range" min="1" max="10" step="0.5" value="${s.price_move_pct}">
    <label for="vm">Volume: ${s.volume_multiple}× the ${s.lookback}-day average</label><input id="vm" type="range" min="1.2" max="5" step="0.1" value="${s.volume_multiple}">
    <label>Announcement types you follow (ranked higher)</label><div class="togs">${CATS.map((c) => `<button data-care="${c}" aria-pressed="${s.care_about.includes(c)}">${CAT_LABEL[c]}</button>`).join("")}</div>
    <label>Muted (always folded away)</label><div class="togs">${CATS.map((c) => `<button data-mute="${c}" aria-pressed="${s.muted.includes(c)}">${CAT_LABEL[c]}</button>`).join("")}</div>
    <div class="row-actions"><button class="btn" id="save">Save settings</button><button class="btn ghost" id="cancel">Cancel</button></div>`);
  draw();
  const bind = () => {
    $sheet.querySelector("#pm").oninput = (e) => { s.price_move_pct = +e.target.value; $sheet.querySelector("label[for=pm]").textContent = `Price move: ±${s.price_move_pct}% in a day`; };
    $sheet.querySelector("#vm").oninput = (e) => { s.volume_multiple = +e.target.value; $sheet.querySelector("label[for=vm]").textContent = `Volume: ${s.volume_multiple}× the ${s.lookback}-day average`; };
    $sheet.querySelectorAll("[data-care]").forEach((b) => (b.onclick = () => { const c = b.dataset.care; s.care_about = s.care_about.includes(c) ? s.care_about.filter((x) => x !== c) : [...s.care_about, c]; b.setAttribute("aria-pressed", s.care_about.includes(c)); }));
    $sheet.querySelectorAll("[data-mute]").forEach((b) => (b.onclick = () => { const c = b.dataset.mute; s.muted = s.muted.includes(c) ? s.muted.filter((x) => x !== c) : [...s.muted, c]; b.setAttribute("aria-pressed", s.muted.includes(c)); }));
    $sheet.querySelector("#cancel").onclick = closeSheet;
    $sheet.querySelector("#save").onclick = async () => {
      S.meta.settings = await api("/api/settings", { method: "PUT", body: JSON.stringify(s) });
      closeSheet(); toast("Settings saved"); renderBrief();
    };
  };
  bind();
}

// ---------- person / draft sheets -------------------------------------------------
async function openPerson(id) {
  const p = await api(`/api/network/person/${encodeURIComponent(id)}`);
  openSheet(`<h3>${esc(p.name)}</h3><p class="note">${esc(p.headline)}${p.city ? `, ${esc(p.city)}` : ""}</p>
    <p style="margin:10px 0 0">${esc(p.how_met)}. Last spoke ${esc(p.last_spoke)}.</p>
    ${careerStrip(p.experience)}
    <ul class="tl">${[...p.experience].reverse().map((e) => `<li><b>${esc(e.role)}</b>, ${esc(e.org)} <span class="note">${e.from || "?"}–${e.to || "now"}</span></li>`).join("")}
    ${p.education.map((e) => `<li>${esc(e.degree)}, ${esc(e.school)} <span class="note">${e.to || ""}</span></li>`).join("")}</ul>
    ${p.tags?.length ? `<p class="note">Go-to for: ${esc(p.tags.join(", "))}</p>` : ""}
    <div class="row-actions"><button class="btn" data-draft="${esc(p.id)}" data-reason="${esc("catching up")}">Draft a note</button></div>`);
}

async function openDraft(id, reason) {
  openSheet(`<div class="loading">Drafting…</div>`);
  const d = await api("/api/draft", { method: "POST", body: JSON.stringify({ person_id: id, reason }) });
  openSheet(`<h3>Draft</h3><p class="note">Only their first name and the reason left your network to write this.</p>
    <textarea class="draft" id="dtext">${esc(d.text)}</textarea>
    <div class="row-actions"><a class="btn" id="wa" target="_blank" rel="noopener">Open in WhatsApp</a><button class="btn ghost" id="copy">Copy text</button></div>`);
  const t = $sheet.querySelector("#dtext"), wa = $sheet.querySelector("#wa");
  const sync = () => (wa.href = `https://wa.me/?text=${encodeURIComponent(t.value)}`); sync(); t.oninput = sync;
  $sheet.querySelector("#copy").onclick = async () => { await navigator.clipboard.writeText(t.value); toast("Copied"); };
}

document.addEventListener("click", (e) => {
  const d = e.target.closest("[data-draft]"); if (d) { e.preventDefault(); openDraft(d.dataset.draft, d.dataset.reason); return; }
  const p = e.target.closest("[data-person]"); if (p) { openPerson(p.dataset.person); return; }
  const q = e.target.closest("[data-q]"); if (q) { location.hash = `#ask/${encodeURIComponent(q.dataset.q)}`; }
});

// ---------- ASK (pull) ----------------------------------------------------------
const SUGGEST = ["DEMOCHEM", "NAVINFLUOR", "CGPOWER", "Who should I call about Dixon?", "Who knows Arvind Kulkarni?", "Experts in rural lending", "Blind spots"];

function trajectoryChart(t) {
  if (!t?.revenue?.length) return "";
  const W = 340, H = 140, P = 22, n = t.revenue.length, bw = (W - P) / n;
  const maxR = Math.max(...t.revenue.filter(Boolean));
  const opm = t.opm || [], oLo = Math.min(...opm.filter((x) => x != null), 0), oHi = Math.max(...opm.filter((x) => x != null), 1);
  const bars = t.revenue.map((v, i) => { const h = ((v || 0) / maxR) * (H - 40); return `<rect x="${P + i * bw + 3}" y="${H - 20 - h}" width="${bw - 6}" height="${h}" rx="2" fill="var(--pen-soft)" stroke="var(--pen)" stroke-width=".6"/>`; }).join("");
  const line = opm.map((v, i) => v == null ? null : `${P + i * bw + bw / 2},${H - 26 - ((v - oLo) / ((oHi - oLo) || 1)) * (H - 60)}`).filter(Boolean).join(" ");
  const labels = t.periods.map((p, i) => i % 2 === n % 2 ? "" : `<text x="${P + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${esc(p.slice(-4))}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Revenue bars and operating margin line by fiscal year">${bars}
    <polyline points="${line}" fill="none" stroke="var(--ink)" stroke-width="1.6"/>${labels}
    <text x="${P}" y="10">Revenue (bars), operating margin (line)</text></svg>`;
}

function driftChart(d, items) {
  const actual = items.find((i) => i.metric === d.metric && `FY${i.fy}` === d.fy && i.check.actual != null)?.check.actual;
  const W = 340, H = 120, P = 30, hist = d.history, vals = hist.flatMap((h) => [h.target[0], h.target[1]]).filter((x) => x != null).concat(actual ?? []);
  const lo = Math.min(...vals) - 2, hi = Math.max(...vals) + 2, sx = (W - P - 70) / Math.max(1, hist.length - 1);
  const y = (v) => 14 + (1 - (v - lo) / (hi - lo)) * (H - 40);
  const marks = hist.map((h, i) => { const x = P + i * sx, a = y(h.target[0]), b = y(h.target[1] ?? h.target[0]);
    const lab = h.target[1] != null && h.target[1] !== h.target[0] ? `${h.target[0]}–${h.target[1]}%` : `${h.target[0]}%`;
    return `<line x1="${x}" x2="${x}" y1="${b}" y2="${a + 0.01}" stroke="var(--pen)" stroke-width="7" stroke-linecap="round"/><text x="${x + 8}" y="${(a + b) / 2 + 4}" style="fill:var(--pen);font-weight:600">${lab}</text><text x="${x}" y="${H - 6}" text-anchor="middle">${esc(h.call)}</text>`; }).join("");
  const act = actual != null ? `<line x1="${P - 14}" x2="${W - 4}" y1="${y(actual)}" y2="${y(actual)}" stroke="var(--down)" stroke-dasharray="4 3" stroke-width="1.5"/><text x="${P - 14}" y="${y(actual) + 14}" style="fill:var(--down)">actual so far ${actual}%</text>` : "";
  const name = { ebitda_margin: "EBITDA margin", revenue_growth: "Revenue growth", pat_margin: "PAT margin", volume: "Volume growth",
    capex: "Capex", utilisation: "Utilisation" }[d.metric] || d.metric;
  return `<p style="margin:14px 0 0;font-weight:650">${esc(name)} guidance for ${esc(d.fy)}: ${esc(d.trend)} across calls</p>
    <svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(name)} guidance ${esc(d.trend)} over successive calls">${act}${marks}</svg>`;
}

const STATUS = { delivered: "Delivered", on_track: "On track", close: "Close", slightly_behind: "Slightly behind", behind: "Behind", missed: "Missed", too_early: "Too early", judgement: "Needs judgement", unclear: "Unclear" };
function renderResearch(r) {
  if (r.error) return `<p class="empty">${esc(r.error)} Live research needs the server to reach screener.in; in offline mode try DEMOCHEM.</p>`;
  const s = r.snapshot, g = r.guidance;
  const flags = (sec) => (sec?.flags?.length ? `<ul class="flags">${sec.flags.map(([k, t]) => `<li class="${k === "+" ? "pos" : k === "-" ? "neg" : "neu"}">${esc(t)}</li>`).join("")}</ul>` : "");
  return `
    <div class="rh"><div class="meta"><span class="tick">${esc(r.symbol)}</span>${r.industry?.length ? `<span>${esc(r.industry.slice(-1)[0])}</span>` : ""}<span>${esc(r.basis || "")}</span></div>
      <h1>${esc(r.name)}</h1></div>
    ${r.synthetic ? `<div class="banner">Fictional company with synthetic filings, for offline demo. Point FINN at any NSE symbol when online.</div>` : ""}

    <h2>Did management deliver?</h2>
    ${g.items.length ? `<p class="verdict">${esc(g.verdict || "")}</p>
      ${g.drift.map((d) => driftChart(d, g.items)).join("")}
      <div style="margin-top:10px">${g.items.slice(0, 8).map((it) => `<div class="promise">
        <div style="display:flex;justify-content:space-between;gap:10px;align-items:flex-start"><q>${esc(it.quote)}</q><span class="status st-${esc(it.check.status)}">${STATUS[it.check.status] || esc(it.check.status)}</span></div>
        <div class="m">${esc(it.call)} call, page ${it.page}. ${esc(it.check.detail || "")}</div></div>`).join("")}</div>`
      : `<p class="note">${g.calls_found ? "No explicit numeric guidance found in the last calls." : "No concall transcripts available for this company."}</p>`}

    <h2>Snapshot</h2>
    <div class="tiles">
      <div class="tile"><div class="k">Market cap</div><div class="v">${s.market_cap_cr ? cr(s.market_cap_cr) : "—"}</div></div>
      <div class="tile"><div class="k">P/E</div><div class="v">${num(s.pe)}</div></div>
      <div class="tile"><div class="k">${s.lender ? "ROE" : "ROCE"}</div><div class="v">${num(s.lender ? s.roe : s.roce)}%</div></div>
      <div class="tile"><div class="k">Revenue, TTM</div><div class="v">${cr(s.revenue_ttm)}</div></div>
      <div class="tile"><div class="k">PAT, TTM</div><div class="v">${cr(s.pat_ttm)}</div></div>
      <div class="tile"><div class="k">${s.lender ? "Fin. margin" : "Op. margin"} ${esc((s.last_fy || "").replace(/^\w+ \d\d(\d\d)$/, "FY$1"))}</div><div class="v">${num(s.opm_last_fy)}%</div></div>
    </div>
    ${r.about ? `<p style="color:var(--ink-2)">${esc(r.about.slice(0, 420))}${r.about.length > 420 ? "…" : ""}</p>` : ""}
    ${r.mix?.segments?.length ? `<div style="margin-top:6px">${r.mix.segments.map((m) => `<div class="cov" style="grid-template-columns:150px 1fr;margin:6px 0"><span>${esc(m.name)} <span class="note">${m.share_pct ?? "?"}%</span></span><div class="bar"><span style="width:${m.share_pct || 0}%"></span></div></div>`).join("")}<p class="note">${esc(r.mix.period || "")} from the investor presentation.</p></div>` : ""}

    <h2>Trajectory</h2>
    ${r.trajectory ? `<p class="note">Revenue CAGR ${num(r.trajectory.revenue_cagr_3y)}% over 3 years, ${num(r.trajectory.revenue_cagr_5y)}% over 5. PAT CAGR ${num(r.trajectory.pat_cagr_3y)}% over 3.</p>${trajectoryChart(r.trajectory)}${flags(r.trajectory)}` : `<p class="note">No annual history.</p>`}

    <h2>Balance sheet and cash</h2>
    ${flags(r.balance_sheet)}${flags(r.cash)}
    ${r.cash && !r.cash.lender ? `<p class="note">Operating cash as % of profit, by year: ${r.cash.conversion_pct.map((c, i) => `${esc(r.cash.periods[i].slice(-4))} ${c ?? "—"}%`).join(", ")}.</p>` : ""}

    <h2>Bull and bear</h2>
    <div class="bb"><div><h4 class="up">Bull</h4><ul>${(r.bull_bear.bull || []).map((p) => `<li>${esc(p.point)}</li>`).join("") || "<li>Nothing in the numbers argues for it yet.</li>"}</ul></div>
      <div><h4 class="down">Bear</h4><ul>${(r.bull_bear.bear || []).map((p) => `<li>${esc(p.point)}</li>`).join("") || "<li>Nothing in the numbers argues against it yet.</li>"}</ul></div></div>
    <p class="note">${r.bull_bear.method === "rules" ? "Built from the computed flags above." : "Written by the model from the facts above only."}</p>

    <h2>Who can help you understand it</h2>${peopleList(r.people)}

    <h2>Sources</h2>${r.sources.map((x) => `<div class="src">${esc(x.type)}${x.date ? `, ${esc(x.date)}` : ""} ${x.url && !x.url.startsWith("fixture") ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">open</a>` : `<span class="note">(fixture)</span>`}</div>`).join("")}
    <p class="note" style="margin-top:12px">Primary sources only: company filings and its own concall transcripts. No news, no broker notes.</p>`;
}

function renderDiligence(d) {
  return `<div class="rh"><h1>${esc(d.name)}</h1><p class="note">Career and your routes in</p></div>
    ${careerStrip(d.timeline)}${wayIn(d.name, d)}
    ${d.questions?.length ? `<h2>Worth asking</h2><ul class="qs">${d.questions.map((q) => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}`;
}

async function renderAsk(q) {
  $app.innerHTML = `<div class="askbar"><form id="askf"><input id="q" name="q" autocomplete="off" placeholder="A company, a person or a theme" value="${esc(q || "")}" aria-label="Ask FINN">${SpeechRec ? `<button class="icon-btn" type="button" id="mic" aria-pressed="false" aria-label="Ask by voice" title="Ask by voice"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></svg></button>` : ""}<button class="btn" type="submit">Ask</button></form></div>
    <div id="out">${q ? `<div class="loading">Reading primary sources…</div>` : `
      <h1 class="lede" style="font-size:28px">Ask about any NSE company, anyone you might meet, or a theme.</h1>
      <p class="sub">Research uses only filings and the company's own concall transcripts. People answers come from your network.</p>
      <div class="sugg">${SUGGEST.map((s) => `<button data-q="${esc(s)}">${esc(s)}</button>`).join("")}</div>`}</div>`;
  document.getElementById("askf").onsubmit = (e) => { e.preventDefault(); const v = e.target.q.value.trim(); if (v) location.hash = `#ask/${encodeURIComponent(v)}`; };
  const mic = document.getElementById("mic");
  if (mic) mic.onclick = () => {
    // Speak a question ("who should I call about Polycab"); it goes through the same Ask routing.
    const rec = new SpeechRec();
    rec.lang = "en-IN"; rec.interimResults = false; rec.maxAlternatives = 1;
    const input = document.getElementById("q");
    rec.onresult = (ev) => { input.value = ev.results[0][0].transcript; document.getElementById("askf").requestSubmit(); };
    rec.onerror = (ev) => toast(ev.error === "not-allowed" ? "Microphone access is off for this site." : "Didn't catch that. Try again or type it.");
    rec.onend = () => mic.setAttribute("aria-pressed", "false");
    mic.setAttribute("aria-pressed", "true");
    try { rec.start(); } catch (err) { mic.setAttribute("aria-pressed", "false"); }
  };
  if (!q) return;
  const out = document.getElementById("out");
  // A first look at a company reads its statements and four transcripts (~20s); say what's happening.
  const steps = ["Reading primary sources…", "Fetching standardised financials…", "Reading the last four concall transcripts…",
    "Checking guidance against reported quarters…", "Finding who in your network knows it… (first look takes ~20s, then it's cached)"];
  let step = 0;
  const ticker = setInterval(() => {
    const el = out.querySelector(".loading");
    if (!el || !out.isConnected) return clearInterval(ticker);
    el.textContent = steps[Math.min(++step, steps.length - 1)];
  }, 3500);
  try {
    const r = await api("/api/ask", { method: "POST", body: JSON.stringify({ q }) });
    if (r.intent === "research") out.innerHTML = renderResearch(r.data);
    else if (r.intent === "who_to_call") out.innerHTML = `<div class="rh"><h1>${esc(r.data.org.name)}</h1><p class="note">People who have worked there, at a competitor, supplier or customer, ranked by how much they'd know and how easy they are to call.</p></div>${peopleList(r.data.people)}
      <div class="row-actions"><button class="btn ghost" data-q="${esc(r.data.org.ticker || r.data.org.name)}">Research ${esc(r.data.org.name)}</button></div>`;
    else if (r.intent === "expertise") out.innerHTML = `<div class="rh"><h1>Go-to people for ${esc(r.data.theme)}</h1></div>${peopleList(r.data.people)}`;
    else if (r.intent === "coverage") out.innerHTML = `<div class="rh"><h1>Blind spots</h1><p class="note">Holdings where nobody in your network is close.</p></div>${coverage(r.data)}`;
    else if (r.intent === "person") { out.innerHTML = ""; openPerson(r.id); }
    else if (r.intent === "diligence") out.innerHTML = renderDiligence(r.data);
    else out.innerHTML = `<p class="empty">FINN couldn't tell what you meant. Try an NSE symbol like INFY, a name, or "experts in specialty chemicals".</p>`;
  } catch (e) { out.innerHTML = `<p class="empty">${esc(e.message)}</p>`; }
}

// ---------- NETWORK -------------------------------------------------------------
function coverage(rows, limit = 99) {
  return `<div class="cov">${rows.slice(0, limit).map((r) => `
    <span><b>${esc(r.symbol)}</b><br><span class="note">${r.weight}% of book</span></span>
    <div><div class="bar ${r.score < 0.3 ? "weak" : ""}"><span style="width:${Math.min(100, (r.score / 0.8) * 100)}%"></span></div>
    <div class="who" style="margin-top:4px">${r.best ? `${esc(r.best.name)}: ${esc(r.best.why)}` : "No one yet"}</div></div>`).join("")}</div>`;
}

async function renderNetwork() {
  $app.innerHTML = `<div class="loading">Looking across your network…</div>`;
  const o = S.overview || (S.overview = await api("/api/network/overview"));
  const weak = o.coverage.filter((c) => c.score < 0.3);
  const themes = ["specialty chemicals", "rural lending", "semiconductors", "quick commerce", "banking", "electronics manufacturing", "pharma", "consumer"];
  $app.innerHTML = `
    <div class="top"><div class="brand">FINN<small>your network</small></div></div>
    <h1 class="lede">${weak.length ? `${weak.length} holdings have nobody close to them.` : "Every holding has someone close."}</h1>
    <p class="sub">${o.size} people you know, ${o.second_degree} more you can reach through them. Matching runs on this server and never leaves it.</p>

    <h2>Blind spots <span class="n">weakest coverage first</span></h2>${coverage(o.coverage, 6)}
    <details class="fold"><summary>All ${o.coverage.length} holdings</summary>${coverage(o.coverage.slice(6))}</details>

    ${o.warmups.length ? `<h2>Reasons to reconnect <span class="n">their world just made news</span></h2>${peopleList(o.warmups.map((w) => ({ ...w, why: w.reason })))}` : ""}

    <h2>Go-to people by theme</h2>
    <div class="themes">${themes.map((t) => `<button data-theme="${esc(t)}" aria-pressed="${S.theme === t}">${esc(t)}</button>`).join("")}</div>
    <div id="experts"></div>

    <h2>Grow it</h2>
    <p class="note">Import your own LinkedIn export (Settings, Data privacy, Get a copy of your data, Connections). Email addresses are dropped on import.</p>
    <div class="row-actions"><label class="btn ghost" for="csv">Import Connections.csv</label><input id="csv" type="file" accept=".csv" hidden></div>`;
  const showTheme = async (t) => {
    S.theme = t;
    $app.querySelectorAll("[data-theme]").forEach((b) => b.setAttribute("aria-pressed", b.dataset.theme === t));
    const r = await api(`/api/network/expertise?theme=${encodeURIComponent(t)}`);
    document.getElementById("experts").innerHTML = peopleList(r.people);
  };
  $app.querySelectorAll("[data-theme]").forEach((b) => (b.onclick = () => showTheme(b.dataset.theme)));
  if (S.theme) showTheme(S.theme);
  document.getElementById("csv").onchange = async (e) => {
    const fd = new FormData(); fd.append("file", e.target.files[0]);
    const res = await fetch("/api/network/import", { method: "POST", body: fd });
    const r = await res.json().catch(() => ({}));
    e.target.value = "";
    if (!res.ok) return toast(r.detail || `Import failed (${res.status})`);
    toast(`Added ${r.added} people`); S.overview = null; renderNetwork();
  };
}

// ---------- router ----------------------------------------------------------------
async function route() {
  closeSheet();
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();  // leaving the Brief stops the read-out
  const [tab, arg] = (location.hash.slice(1) || "brief").split("/");
  document.querySelectorAll("nav.tabs a").forEach((a) => (a.dataset.tab === tab ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")));
  window.scrollTo(0, 0);
  try {
    if (tab === "ask") await renderAsk(arg ? decodeURIComponent(arg) : "");
    else if (tab === "network") await renderNetwork();
    else await renderBrief();
  } catch (e) {
    $app.innerHTML = e.message === "locked"
      ? `<h1 class="lede">This prototype is private.</h1><p class="sub">Open the link with your access key, for example /?key=…</p>`
      : `<p class="empty">Something went wrong: ${esc(e.message)}. Pull to refresh.</p>`;
  }
}
window.addEventListener("hashchange", route);
route();
