/* Swarm Negotiator war-room. Vanilla JS, no build step, no CDN. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = (n) => (n == null ? "—" : "₹" + Number(n).toLocaleString("en-IN"));
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const LANG = { kn: "KN", hi: "HI", ta: "TA", mr: "MR", en: "EN" };
  const CATEGORY = { cab_outstation: "🚙 Cab", mini_truck: "🚚 Mini-truck", ac_repair: "❄️ AC repair" };
  const STATUS_LABEL = { PENDING: "QUEUED", DIALING: "DIALING", CONNECTED: "CONNECTED", QUOTED: "QUOTED", COUNTERED: "COUNTERED", HOLD: "ON HOLD", WON: "WON", RELEASED: "RELEASED", ENDED: "ENDED", FAILED: "FAILED" };

  const S = { config: null, preset: null, mode: store.get("mode", "simulation"), session: null, ws: null, poll: null, seenTicks: new Set(), tickTimer: null, lastT: 0, lastTAt: 0,
    chat: { id: null, task: {}, ready: false }, geo: null, providers: [], provSel: new Set(), provInfo: null };
  const md = (t) => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>");

  // ------------------------------------------------------------------ API
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    const tok = $("#adminToken")?.value || store.get("adminToken", "");
    if (tok) headers["X-Admin-Token"] = tok;
    const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) }, body: opts.body ? JSON.stringify(opts.body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
    return data;
  }

  // ------------------------------------------------------------------ brief panel
  function renderPresets() {
    const box = $("#presetChips");
    box.innerHTML = Object.entries(S.config.presets).map(([k, p]) => `<button class="chip" data-k="${esc(k)}">${esc(p.title)}</button>`).join("");
    $$(".chip", box).forEach((b) => b.addEventListener("click", () => applyPreset(b.dataset.k)));
  }
  function applyPreset(key) {
    const p = S.config.presets[key];
    if (!p) return;
    S.preset = key;
    $$(".chip").forEach((c) => c.classList.toggle("active", c.dataset.k === key));
    $("#briefText").value = p.brief;
    fillRequest(p.request);
    $("#parsedBy").textContent = "preset";
  }
  function fillRequest(r) {
    S.request = { ...r };
    $("#target").value = r.target_price ?? "";
    $("#ceiling").value = r.walkaway_ceiling ?? "";
    $("#summary").value = r.summary ?? "";
  }
  async function parseBrief() {
    const text = $("#briefText").value.trim();
    if (text.length < 3) return;
    $("#parseBtn").disabled = true;
    $("#parsedBy").textContent = "parsing…";
    try {
      const r = await api("/api/parse_brief", { method: "POST", body: { text } });
      fillRequest(r);
      S.preset = null;
      $$(".chip").forEach((c) => c.classList.remove("active"));
      $("#parsedBy").textContent = r.parsed_by === "gemini" ? "parsed by Gemini" : "parsed by rules";
    } catch (e) {
      $("#parsedBy").textContent = "parse failed: " + e.message;
    } finally {
      $("#parseBtn").disabled = false;
    }
  }

  function setupMic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn = $("#micBtn");
    if (!SR) { btn.title = "Speech input not supported in this browser"; btn.disabled = true; return; }
    let rec = null;
    btn.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = $("#micLang").value;
      rec.interimResults = true;
      rec.continuous = false;
      const base = "";
      rec.onresult = (ev) => {
        $("#briefText").value = base + [...ev.results].map((r) => r[0].transcript).join(" ");
      };
      rec.onend = () => { btn.classList.remove("rec"); rec = null; parseBrief(); };
      rec.onerror = () => { btn.classList.remove("rec"); rec = null; };
      btn.classList.add("rec");
      rec.start();
    });
  }

  function setupChatMic() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    const btn = $("#chatMic");
    if (!SR) { btn.disabled = true; btn.title = "Speech input not supported in this browser"; return; }
    let rec = null;
    btn.addEventListener("click", () => {
      if (rec) { rec.stop(); return; }
      rec = new SR();
      rec.lang = $("#chatMicLang").value;
      rec.interimResults = true;
      rec.onresult = (ev) => { $("#chatInput").value = [...ev.results].map((r) => r[0].transcript).join(" "); };
      rec.onend = () => { btn.classList.remove("rec"); rec = null; if ($("#chatInput").value.trim()) sendChat(); };
      rec.onerror = () => { btn.classList.remove("rec"); rec = null; };
      btn.classList.add("rec");
      rec.start();
    });
  }

  function setMode(mode) {
    S.mode = mode;
    store.set("mode", mode);
    $$("#modeSeg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
    $("#hybridFields").hidden = mode !== "hybrid";
    $("#liveFields").hidden = mode !== "live";
    const c = S.config || {};
    const sync = c.bolna_sync || {};
    const why = sync.state && sync.state !== "ok" ? ` (Bolna setup: ${sync.state} — ${sync.detail || ""})` : "";
    const hints = {
      simulation: "3 AI-simulated vendors negotiate against each other through the same shared-memory engine. No phone, no cost.",
      hybrid: c.bolna_ready ? "Your phone rings as Vendor A via Bolna + Sarvam. Vendors B & C are simulated; their quotes are used against you live." : "⚠ Hybrid needs BOLNA_API_KEY + a synced agent (⚙︎ Setup)." + why,
      live: c.bolna_ready ? "Dials 3 real numbers via Bolna in staggered waves (t=0, +8s, +16s)." : "⚠ Live needs BOLNA_API_KEY + a synced agent (⚙︎ Setup)." + why,
    };
    $("#modeHint").textContent = hints[mode];
    if (S.providers.length) updateCallHint();
    $("#launchBtn").textContent = mode === "simulation" ? "🚀 Launch swarm · 3 simulated calls" : mode === "hybrid" ? "📞 Launch · ring my phone + 2 AI vendors" : "📞 Launch · 3 real calls";
  }

  async function launch() {
    const btn = $("#launchBtn");
    btn.disabled = true;
    const target = parseInt($("#target").value, 10);
    const ceiling = parseInt($("#ceiling").value, 10);
    if (!(target > 0) || !(ceiling >= target)) {
      alert("Set a target and a walk-away ceiling (ceiling ≥ target).");
      btn.disabled = false;
      return;
    }
    const body = {
      preset: S.preset || undefined,
      mode: S.mode,
      request: { ...(S.request || {}), target_price: target, walkaway_ceiling: ceiling, summary: $("#summary").value || undefined },
    };
    delete body.request.parsed_by;
    if (S.mode === "hybrid") {
      body.live_phone = $("#livePhone").value;
      body.live_language = $("#liveLang").value;
      store.set("livePhone", body.live_phone);
    }
    if (S.mode === "live") {
      body.vendor_phones = $$("#liveFields .lp").map((i) => i.value.trim()).filter(Boolean);
      body.vendor_names = $$("#liveFields .ln").map((i) => i.value.trim());
      body.vendor_languages = $$("#liveFields .ll").map((i) => i.value);
    }
    try {
      const session = await api("/api/session", { method: "POST", body });
      startWarRoom(session);
    } catch (e) {
      alert("Could not start: " + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ 1. chat intake
  function addMsg(role, text) {
    const el = document.createElement("div");
    el.className = `msg ${role}`;
    el.innerHTML = md(text);
    $("#chatLog").appendChild(el);
    $("#chatLog").scrollTop = $("#chatLog").scrollHeight;
    return el;
  }
  async function sendChat(raw) {
    const text = (raw ?? $("#chatInput").value).trim();
    if (/^find providers$/i.test(text)) { await findProviders(); return; }
    if (/use my (current |live )?location/i.test(text)) { $("#chatInput").value = ""; await useLocation(); return; }
    if (text) addMsg("user", text);
    $("#chatInput").value = "";
    const typing = addMsg("bot typing", "…");
    try {
      const r = await api("/api/chat", { method: "POST", body: { conversation_id: S.chat.id, message: text, lat: S.geo?.lat, lng: S.geo?.lng } });
      Object.assign(S.chat, { id: r.conversation_id, task: r.task, ready: r.ready });
      typing.remove();
      addMsg("bot", r.reply);
      renderChips(r.suggestions || []);
      renderTaskCard();
      if (!S.geo && !S.geoDenied && NEAR_ME.test(r.task?.location || "")) await useLocation();
    } catch (e) {
      typing.remove();
      addMsg("bot", "Sorry, something went wrong: " + e.message);
    }
  }
  function renderChips(list) {
    $("#chatChips").innerHTML = list.map((t) => `<button class="chip" type="button">${esc(t)}</button>`).join("");
    $$("#chatChips .chip").forEach((b) => b.addEventListener("click", () => sendChat(b.textContent)));
  }
  function renderTaskCard() {
    const t = S.chat.task || {};
    $("#taskCard").hidden = !t.service_key;
    if (!t.service_key) return;
    $("#tService").value = t.service_label || "";
    $("#tLocation").value = t.pickup && t.drop ? `${t.pickup} → ${t.drop}` : (t.location || "");
    $("#tWhen").value = t.when || "";
    $("#tDetails").value = t.details || "";
    $("#tTarget").value = t.target_price || "";
    $("#tCeiling").value = t.walkaway_ceiling || "";
    const appt = t.goal === "appointment";
    $$(".budget-field").forEach((el) => { el.hidden = appt; });
    $("#tGoal").textContent = appt ? "Appointment: I'll book a slot and get the fee. No haggling with clinics."
      : "Negotiation: your target and max are never told to the AI agent. It can't be talked above them.";
    $("#findBtn").disabled = !S.chat.ready;
  }
  function readTaskCard() {
    const t = S.chat.task;
    t.service_label = $("#tService").value.trim() || t.service_label;
    const loc = $("#tLocation").value.trim();
    if (loc.includes("→")) { [t.pickup, t.drop] = loc.split("→").map((x) => x.trim()); t.location = t.pickup; } else if (loc) { t.location = loc; }
    t.when = $("#tWhen").value.trim() || t.when;
    t.details = $("#tDetails").value.trim();
    if (t.goal !== "appointment") {
      const tgt = parseInt($("#tTarget").value, 10), max = parseInt($("#tCeiling").value, 10);
      if (tgt > 0) t.target_price = tgt;
      if (max > 0) t.walkaway_ceiling = Math.max(max, t.target_price || 0);
    }
  }
  const NEAR_ME = /^\W*(near(by| me| you)?|around me|here|(my|your) (current |live )?location|current location)\W*$/i;
  // Live GPS position from the browser (asks permission once). Resolves to null if denied/unavailable.
  function getGeo() {
    if (S.geo) return Promise.resolve(S.geo);
    if (!navigator.geolocation || S.geoDenied) return Promise.resolve(null);
    return new Promise((resolve) => navigator.geolocation.getCurrentPosition((p) => {
      S.geo = { lat: p.coords.latitude, lng: p.coords.longitude };
      $("#geoBtn").classList.add("active");
      resolve(S.geo);
    }, () => { S.geoDenied = true; resolve(null); }, { timeout: 10000, maximumAge: 120000, enableHighAccuracy: true }));
  }
  async function useLocation() {
    if (!navigator.geolocation) { addMsg("bot", "Location isn't available in this browser."); return; }
    S.geoDenied = false;
    if (!(await getGeo())) { addMsg("bot", "Couldn't get your location. Just tell me the area instead."); return; }
    addMsg("bot", "📍 Got your live location. I'll search near you.");
    if (S.chat.id) {
      if (S.chat.task && NEAR_ME.test(S.chat.task.location || "")) S.chat.task.location = "";
      await sendChat("");  // the server names your area from the GPS position
    }
  }

  // ------------------------------------------------------------------ 2. providers
  async function findProviders() {
    if (!S.chat.ready) { addMsg("bot", "I still need a couple of details first."); return; }
    readTaskCard();
    const btn = $("#findBtn");
    btn.disabled = true; btn.textContent = "Searching maps…";
    try {
      await getGeo();  // live location: results ranked by distance from where you are
      const r = await api("/api/providers/search", { method: "POST", body: { task: S.chat.task, lat: S.geo?.lat, lng: S.geo?.lng } });
      S.providers = r.providers || [];
      S.provInfo = r;
      S.provSel = new Set(S.providers.slice(0, 3).map((p) => p.id));
      renderProviders();
      $("#providersPanel").hidden = false;
      $("#providersPanel").scrollIntoView({ behavior: "smooth", block: "start" });
      if (!S.providers.length) addMsg("bot", "I couldn't find providers for that. Try a broader area or different words.");
    } catch (e) {
      alert("Search failed: " + e.message);
    } finally {
      btn.disabled = false; btn.textContent = "🔎 Find the best-rated providers nearby";
    }
  }
  function renderProviders() {
    const r = S.provInfo || {};
    const t = S.chat.task || {};
    $("#provTitle").textContent = `2 · Best-rated ${t.service_label || "providers"} near ${(t.location || "you").split(",")[0]}`;
    $("#provSource").textContent = r.source ? `source: ${r.providers?.[0]?.source || r.source}` : "";
    const banner = $("#testBanner");
    banner.hidden = !r.test_mode;
    banner.textContent = `🧪 Test mode: every call rings ${r.test_phone || "your test phone"}, not the business. Set CALL_REAL_NUMBERS=true to call real numbers.`;
    $("#provList").innerHTML = S.providers.map((p, i) => {
      const meta = [
        p.rating != null ? `<span class="stars">★ ${p.rating.toFixed(1)}</span> (${Number(p.reviews || 0).toLocaleString("en-IN")})` : "no ratings",
        p.distance_km != null ? `${p.distance_km} km` : "",
        p.open_now === true ? "open now" : p.open_now === false ? "closed now" : "",
      ].filter(Boolean).join(" · ");
      const phone = p.phone ? `<a href="tel:${esc(p.phone)}">${esc(p.phone_local || p.phone)}</a>` : "no listed number";
      const links = [
        p.website ? `<a href="${esc(p.website)}" target="_blank" rel="noopener">🌐 website</a>` : "",
        p.maps_uri ? `<a href="${esc(p.maps_uri)}" target="_blank" rel="noopener">🗺️ map</a>` : "",
      ].filter(Boolean).join(" · ");
      const hours = (p.hours || []).length
        ? `<details class="pmeta phours"><summary>🕒 Opening hours</summary>${p.hours.map((h) => esc(h)).join("<br>")}</details>` : "";
      return `<label class="prov ${S.provSel.has(p.id) ? "sel" : ""}" data-id="${esc(p.id)}">
        <input type="checkbox" ${S.provSel.has(p.id) ? "checked" : ""}>
        <div><div class="pname">${esc(p.name)}</div><div class="pmeta">${p.category ? `${esc(p.category)} · ` : ""}${meta}</div>
          <div class="pmeta">📍 ${esc(p.address || "")}</div>
          <div class="pmeta">📞 ${phone}${links ? ` · ${links}` : ""}</div>
          ${p.test_number ? `<div class="pmeta">↪ our call rings <b>${esc(p.dial_phone)}</b>, not the business</div>` : ""}
          ${hours}</div>
        <div class="rank">#${i + 1}</div></label>`;
    }).join("");
    $$("#provList .prov").forEach((row) => {
      $("input", row).addEventListener("change", (ev) => {
        const id = row.dataset.id;
        if (ev.target.checked) {
          if (S.provSel.size >= 5) { ev.target.checked = false; alert("Up to 5 providers at a time."); return; }
          S.provSel.add(id);
        } else S.provSel.delete(id);
        row.classList.toggle("sel", ev.target.checked);
        updateCallHint();
      });
    });
    updateCallHint();
  }
  function updateCallHint() {
    const n = S.provSel.size;
    const test = S.provInfo?.test_phone || "your test phone";
    $("#callBtn").textContent = S.mode === "simulation" ? `🤖 Simulate ${n} call${n === 1 ? "" : "s"}` : `📞 Call ${n} selected`;
    $("#callBtn").disabled = n === 0;
    $("#callHint").textContent = {
      simulation: "Sim mode: the selected providers are played by AI. No phone calls, no cost.",
      hybrid: `Hybrid: #1 rings ${test} for real (you play the provider); the others are simulated and their quotes are used on the call.`,
      live: `Live: every selected provider is called${S.provInfo?.test_mode ? ` on ${test}, one after another` : ""}. After you confirm, the winner gets a confirmation call.`,
    }[S.mode];
  }
  async function callProviders() {
    readTaskCard();
    const chosen = S.providers.filter((p) => S.provSel.has(p.id));
    if (!chosen.length) return;
    const btn = $("#callBtn");
    btn.disabled = true;
    try {
      const session = await api("/api/session", { method: "POST", body: { task: S.chat.task, providers: chosen, mode: S.mode, language: $("#callLang").value } });
      startWarRoom(session);
    } catch (e) {
      alert("Could not start calls: " + e.message);
    } finally {
      btn.disabled = false;
    }
  }

  // ------------------------------------------------------------------ war room
  function startWarRoom(session) {
    S.session = session;
    S.seenTicks = new Set();
    $("#tickerTrack").innerHTML = '<span class="muted">Waiting for the first quote…</span>';
    $("#briefPanel").hidden = true;
    $("#chatPanel").hidden = true;
    $("#providersPanel").hidden = true;
    $("#warRoom").hidden = false;
    $("#ticker").hidden = false;
    history.replaceState(null, "", "#s=" + session.session_id);
    render(session);
    connect(session.session_id);
    clearInterval(S.tickTimer);
    S.tickTimer = setInterval(() => {
      if (!S.session) return;
      const t = S.lastT + (Date.now() - S.lastTAt) / 1000;
      if (S.session.status !== "LOCKED" && S.session.status !== "CLOSED") $("#sbTime").textContent = Math.round(t) + "s";
    }, 500);
  }

  function connect(sid) {
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    clearInterval(S.poll);
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/session/${sid}`);
    S.ws = ws;
    const conn = $("#conn");
    ws.onopen = () => { conn.className = "conn on"; $("span", conn).textContent = "live"; };
    ws.onmessage = (m) => {
      if (m.data === "pong") return;
      try { const msg = JSON.parse(m.data); if (msg.session) { render(msg.session, msg.event); } } catch { /* ignore */ }
    };
    ws.onclose = () => {
      conn.className = "conn off"; $("span", conn).textContent = "polling";
      S.poll = setInterval(async () => { try { render(await api(`/api/session/${sid}`)); } catch { /* ignore */ } }, 2000);
      setTimeout(() => { if (S.session?.session_id === sid) connect(sid); }, 4000);
    };
    const ping = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); else clearInterval(ping); }, 20000);
  }

  function render(session, event) {
    S.session = session;
    S.lastT = session.t; S.lastTAt = Date.now();
    const r = session.request;
    const appt = r.goal === "appointment";
    $("#sbCategory").textContent = `${CATEGORY[r.category] || "🧰 " + (r.service_label || r.category)} · ${session.mode.toUpperCase()} MODE`;
    $("#sbJob").textContent = r.summary;
    $("#sbMeta").textContent = appt ? `${r.location || ""} · preferred ${r.when || "any time"} · booking a slot, no haggling`
      : `${r.when || ""} · target ${inr(r.target_price)} · walk-away ${inr(r.walkaway_ceiling)} (never told to the AI)`;
    const best = session.current_best_quote;
    const bestLeg = best ? session.legs.find((l) => l.leg_id === best.leg_id) : null;
    $("#sbBest").textContent = best ? (appt ? (bestLeg?.slot || "—") : inr(best.price)) : "—";
    $("#sbBestVendor").textContent = best ? best.vendor_name + (appt && best.price ? ` · fee ${inr(best.price)}` : "") : "";
    $("#sbBest").previousElementSibling.textContent = appt ? "Best slot" : "Best all-in";
    if (appt) {
      const held = session.legs.filter((l) => l.slot).length;
      $("#sbSaved").textContent = `${held}/${session.legs.length}`;
      $("#sbSaved").previousElementSibling.textContent = "Slots offered";
      $("#sbSaved").nextElementSibling.textContent = "providers with a slot";
    } else {
      $("#sbSaved").textContent = session.savings_vs_worst_first_quote ? inr(session.savings_vs_worst_first_quote) : "—";
    }
    $("#sbVersion").textContent = "state v" + session.state_version;
    if (session.status === "LOCKED") $("#sbTime").textContent = Math.round(session.t) + "s ✓";
    const lockable = !session.locked_leg_id && best && (!r.has_budget || best.price <= r.walkaway_ceiling);
    $("#lockBestBtn").disabled = !lockable;
    $("#lockBestBtn").textContent = session.locked_leg_id ? (appt ? "Appointment confirmed ✓" : "Deal locked ✓")
      : (appt ? "Confirm best slot" : "Lock best deal & send UPI token");

    const cards = $("#cards");
    session.legs.forEach((leg) => {
      let card = $(`#card-${leg.leg_id}`);
      if (!card) {
        card = document.createElement("article");
        card.id = `card-${leg.leg_id}`;
        card.className = "card";
        cards.appendChild(card);
      }
      renderCard(card, leg, session);
    });
    renderTicker(session, event);
    if (session.whatsapp && session.locked_leg_id && !$("#lockModal").open && !S.modalShownFor) {
      S.modalShownFor = session.session_id;
      showLockModal(session);
    }
    if (session.payment?.status === "paid" && $("#lockModal").open) showLockModal(session);
  }

  function renderCard(card, leg, session) {
    const r = session.request;
    const prevScroll = $(".transcript", card);
    const atBottom = !prevScroll || prevScroll.scrollHeight - prevScroll.scrollTop - prevScroll.clientHeight < 40;
    const keepTop = prevScroll ? prevScroll.scrollTop : 0;
    const unit = leg.first_model && leg.first_model !== "flat";
    const unitLabel = unit ? leg.first_model.replace("per_", "") : "";
    const hidden = leg.first_hidden || 0;
    const saved = leg.first_all_in && leg.best_all_in && leg.first_all_in > leg.best_all_in ? leg.first_all_in - leg.best_all_in : 0;
    const flags = [];
    if (hidden) flags.push(`<span class="flag hidden">⚠ ${inr(hidden)} hidden add-ons caught</span>`);
    if (unit && leg.first_all_in !== leg.first_stated) flags.push(`<span class="flag unit">${inr(leg.first_stated)}/${unitLabel} → ${inr(leg.first_all_in)} total</span>`);
    if (saved) flags.push(`<span class="flag save">▼ ${inr(saved)} negotiated</span>`);
    if (r.has_budget && leg.best_all_in && leg.best_all_in > r.walkaway_ceiling) flags.push(`<span class="flag guard">🛡 above ceiling: blocked</span>`);
    for (const x of leg.excluded || []) flags.push(`<span class="flag excl" title="Billed separately by the vendor: not in the price, outside your budget">➕ ${esc(x.item)} ${x.amount ? inr(x.amount) : "at actuals"}: not included</span>`);
    const captured = [
      ...(leg.extras || []).filter((x) => x.kind !== "excluded").map((x) => `${x.item}: ${x.status === "included" ? "included" : (x.amount ? inr(x.amount) + " " : "") + "extra"}`),
      ...(leg.details || []),
    ];
    const appt = r.goal === "appointment";
    const pv = leg.provider || {};
    const pmeta = [pv.rating != null ? `★ ${Number(pv.rating).toFixed(1)} (${Number(pv.reviews || 0).toLocaleString("en-IN")})` : "",
      pv.distance_km != null ? `${pv.distance_km} km` : "", pv.test_number ? "🧪 test number" : ""].filter(Boolean).join(" · ");
    card.className = "card" + (leg.status === "WON" ? " won" : "") + (leg.status === "RELEASED" ? " released" : "");
    const levT = new Set((session.arbitrage_log || []).filter((a) => a.to_leg === leg.leg_id).map((a) => a.t));
    const seen = Number(card.dataset.seen || 0);
    card.dataset.seen = String((leg.transcript || []).length);
    const bubbles = (leg.transcript || []).map((t, i) => {
      const who = t.role === "agent" ? "🤖 NEGOTIATOR" : t.role === "vendor" ? "🧑 " + leg.vendor_name.toUpperCase() : "";
      const lev = t.role === "agent" && levT.has(t.t) ? " lev" : "";
      return `<div class="bubble ${esc(t.role)}${lev}${i >= seen ? " new" : ""}">${who ? `<span class="who">${esc(who)}</span>` : ""}${esc(t.text)}</div>`;
    }).join("");
    const canLock = !session.locked_leg_id && leg.status !== "FAILED" && (appt ? !!leg.slot
      : leg.best_all_in && (!r.has_budget || leg.best_all_in <= r.walkaway_ceiling));
    const priceBoxes = appt ? `
        <div class="pbox"><div class="l">Slot</div><div class="v">${esc(leg.slot || "—")}</div></div>
        <div class="pbox"><div class="l">Fee</div><div class="v">${inr(leg.best_all_in)}</div></div>
        <div class="pbox now"><div class="l">Rating</div><div class="v">${pv.rating != null ? "★ " + Number(pv.rating).toFixed(1) : "—"}</div></div>` : `
        <div class="pbox"><div class="l">Said</div><div class="v">${unit ? inr(leg.first_stated) + "/" + unitLabel : inr(leg.first_stated)}</div></div>
        <div class="pbox"><div class="l">Real all-in</div><div class="v ${saved ? "strike" : ""}">${inr(leg.first_all_in)}</div></div>
        <div class="pbox now"><div class="l">Now</div><div class="v">${inr(leg.best_all_in)}</div></div>`;
    const lockLabel = leg.status === "WON" ? "✓ Winner" : appt ? (leg.slot ? `Confirm ${leg.slot}` : "Waiting for a slot") : "Lock " + inr(leg.best_all_in);
    card.innerHTML = `
      <div class="card-head">
        <div>
          <div class="vname">${esc(leg.vendor_name)}</div>
          ${pmeta ? `<div class="pmeta-card">${esc(pmeta)}</div>` : ""}
          <div class="tags"><span class="tag">${esc(LANG[leg.language] || leg.language)}</span>
            <span class="tag ${leg.mode === "live" ? "live" : ""}">${leg.mode === "live" ? "📞 LIVE" : "AI SIM"}</span>
            <span class="tag">LEG ${esc(leg.leg_id)}</span>${leg.rounds ? `<span class="tag">ROUND ${leg.rounds}</span>` : ""}</div>
        </div>
        <span class="badge st-${esc(leg.status)}">${esc(STATUS_LABEL[leg.status] || leg.status)}</span>
      </div>
      <div class="prices">${priceBoxes}
      </div>
      <div class="flags">${flags.join("")}</div>
      ${captured.length ? `<ul class="captured" title="Everything the vendor said that matters">${captured.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
      <div class="transcript">${bubbles || '<div class="bubble system">Waiting to dial…</div>'}</div>
      ${leg.recording_url ? `<audio controls preload="none" src="${esc(leg.recording_url)}"></audio>` : ""}
      ${leg.error ? `<div class="err">${esc(leg.error)}</div>` : ""}
      <div class="card-foot"><button class="btn win" data-lock="${esc(leg.leg_id)}" ${canLock ? "" : "disabled"}>${esc(lockLabel)}</button></div>`;
    const tr = $(".transcript", card);
    tr.scrollTop = atBottom ? tr.scrollHeight : keepTop;
    $("[data-lock]", card).addEventListener("click", () => lockWinner(leg.leg_id));
  }

  function renderTicker(session, event) {
    const log = session.arbitrage_log || [];
    const track = $("#tickerTrack");
    if (!log.length) return;
    const names = Object.fromEntries(session.legs.map((l) => [l.leg_id, l.vendor_name]));
    let fresh = null;
    const html = log.slice().reverse().map((a) => {
      const key = `${a.state_version}-${a.to_leg}`;
      const isNew = !S.seenTicks.has(key);
      if (isNew) { S.seenTicks.add(key); fresh = fresh || a; }
      const what = a.counter ? `counter <b>${inr(a.counter)}</b>` : "asked to beat it";
      return `<span class="tick ${isNew ? "new" : ""}">t=${Math.round(a.t)}s · ${esc(names[a.from_leg] || a.from_leg)} <b>${inr(a.competitor_price)}</b> → <span class="to">${esc(names[a.to_leg] || a.to_leg)}</span>: ${what}</span>`;
    }).join("");
    track.innerHTML = html;
    if (fresh && event && (event.type === "quote" || event.type === "status_check")) flashLeverage(fresh, names);
  }

  function flashLeverage(a, names) {
    const src = $(`#card-${a.from_leg}`), dst = $(`#card-${a.to_leg}`);
    src?.classList.add("source"); dst?.classList.add("target");
    setTimeout(() => { src?.classList.remove("source"); dst?.classList.remove("target"); }, 1800);
    const f = $("#flash");
    f.hidden = false;
    f.innerHTML = `⚡ Leverage injected: ${esc(names[a.from_leg])} ${inr(a.competitor_price)} → ${esc(names[a.to_leg])}${a.counter ? " · counter " + inr(a.counter) : ""}`;
    f.style.animation = "none"; void f.offsetWidth; f.style.animation = "";
    clearTimeout(S.flashT);
    S.flashT = setTimeout(() => { f.hidden = true; }, 1900);
    if (navigator.vibrate) navigator.vibrate(60);
  }

  // ------------------------------------------------------------------ lock + checkout
  async function lockWinner(legId) {
    const s = S.session;
    if (!s) return;
    const id = legId || s.current_best_quote?.leg_id;
    if (!id) return;
    try {
      const res = await api(`/api/session/${s.session_id}/lock`, { method: "POST", body: { leg_id: id } });
      S.modalShownFor = s.session_id;
      render(res.session);
      showLockModal(res.session);
    } catch (e) {
      alert("Guardrail: " + e.message);
    }
  }

  function showLockModal(session) {
    const leg = session.legs.find((l) => l.leg_id === session.locked_leg_id);
    const p = session.payment || {};
    const wa = session.whatsapp || {};
    const paid = p.status === "paid";
    const appt = session.request.goal === "appointment";
    $("#lmTitle").textContent = appt ? "Appointment confirmed 🎉" : paid ? "Token advance paid ✓" : "Deal locked 🎉";
    const confirmNote = leg?.mode === "live" ? `<div class="small">📞 Calling ${esc(leg.vendor_name)} back now to confirm it with them.</div>` : "";
    if (appt) {
      $("#lmBody").innerHTML = `<div class="deal"><div class="muted small">${esc(session.request.summary)}</div>
        <div><b>${esc(leg?.vendor_name)}</b></div><div class="big">${esc(leg?.slot || "")}</div>
        <div class="small">${leg?.best_all_in ? "Fee " + inr(leg.best_all_in) : "Fee to be confirmed"} · other providers released politely</div>${confirmNote}</div>`;
      $("#lmActions").innerHTML = session.whatsapp?.link ? `<a class="btn win" href="${esc(session.whatsapp.link)}" target="_blank" rel="noopener">Send WhatsApp summary</a>` : "";
      if (!$("#lockModal").open) $("#lockModal").showModal();
      return;
    }
    const mockUpi = p.provider === "mock" ? `
      <div class="upi"><div class="qr" aria-hidden="true"></div>
        <div><b>Razorpay Test Checkout (mock)</b><div style="font-size:12px">Test mode · no real money moves<br>Amount ${inr(p.amount)}${p.error ? `<br><span style="color:#b91c1c">${esc(p.error)}</span>` : ""}</div></div></div>` : "";
    $("#lmBody").innerHTML = `
      <div class="deal">
        <div class="muted small">${esc(session.request.summary)}</div>
        <div><b>${esc(leg?.vendor_name)}</b> · all-inclusive</div>
        <div class="big">${inr(leg?.best_all_in)}</div>
        <div class="small">UPI token advance: <b>${inr(p.amount)}</b> · saved <b class="good">${inr(session.savings_vs_worst_first_quote)}</b> vs highest first quote</div>
        ${(leg?.excluded || []).length ? `<div class="small">Not included, pay separately (outside your budget): <b>${leg.excluded.map((x) => esc(x.item) + (x.amount ? " " + inr(x.amount) : " (at actuals)")).join(", ")}</b></div>` : ""}
        <div class="small muted">Runner-ups released politely · your number shared with 0 vendors</div>${confirmNote}
      </div>${mockUpi}`;
    const actions = [];
    if (p.provider === "razorpay" && p.url) actions.push(`<a class="btn primary" href="${esc(p.url)}" target="_blank" rel="noopener">Pay ${inr(p.amount)} (Razorpay ${p.test_mode ? "test" : ""})</a>`);
    if (p.provider === "mock" && !paid) actions.push(`<button type="button" class="btn primary" id="mockPayBtn">Pay ${inr(p.amount)} (test)</button>`);
    if (wa.link) actions.push(`<a class="btn win" href="${esc(wa.link)}" target="_blank" rel="noopener">${wa.sent ? "WhatsApp sent ✓ · open" : "Send WhatsApp summary"}</a>`);
    $("#lmActions").innerHTML = actions.join("");
    $("#mockPayBtn")?.addEventListener("click", async () => {
      try { render(await api(`/api/session/${session.session_id}/mock_pay`, { method: "POST" })); } catch (e) { alert(e.message); }
    });
    const dlg = $("#lockModal");
    if (!dlg.open) dlg.showModal();
  }

  // ------------------------------------------------------------------ admin
  async function openAdmin() {
    try { S.config = await api("/api/config"); } catch { /* keep old */ }
    const c = S.config;
    const rows = [
      ["Public URL", c.public_base_url || c.detected_base_url + " (auto)"],
      ["Bolna API key", c.bolna_api_key_set ? "✓ set" : "✗ missing"],
      ["Bolna agents", Object.keys(c.bolna_agent_ids || {}).length ? JSON.stringify(c.bolna_agent_ids) : "none yet"],
      ["Telephony", c.bolna_telephony_provider],
      ["Call LLM", (c.bolna_llm_active || c.bolna_llm_model) + (c.bolna_llm_active && c.bolna_llm_active !== c.bolna_llm_model
        ? ` (wanted ${c.bolna_llm_model})` : "") + (c.bolna_llm_note ? ` · ${c.bolna_llm_note}` : "")],
      ["Counter-offers per call", String(c.max_counter_rounds ?? 1)],
      ["Your test phone", c.live_test_phone_set ? "✓ set" : "enter in Hybrid form"],
      ["Gemini (brief parsing" + (c.sim_llm_rephrase ? " + sim wording)" : ")"),
        !c.gemini_enabled ? "off (rules fallback)" : Object.keys(c.gemini?.rate_limited_models || {}).length
          ? "⚠ free daily quota used up, using rules until it resets" : "✓ " + c.gemini_model],
      ["Razorpay", c.razorpay_enabled ? "✓ test keys" : "mock checkout"],
      ["WhatsApp Cloud API", c.whatsapp_api_enabled ? "✓" : "wa.me link"],
      ["Tool auth secret", c.tool_auth_enabled ? "✓" : "off"],
      ["Bolna auto-setup", `${c.bolna_sync?.state || "?"}: ${c.bolna_sync?.detail || ""}`],
    ];
    $("#cfgList").innerHTML = rows.map(([k, v]) => `<div><span class="muted">${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    $("#adminToken").value = store.get("adminToken", "");
    $("#adminModal").showModal();
  }
  async function adminCall(dry) {
    store.set("adminToken", $("#adminToken").value);
    const out = $("#adminOut");
    out.hidden = false;
    out.textContent = dry ? "Building payload…" : "Calling Bolna…";
    try {
      const langs = $("#syncLangs").value.split(",").map((s) => s.trim()).filter(Boolean);
      const res = dry ? await api(`/api/admin/agent_payload?lang=${encodeURIComponent(langs[0] || "hi")}`)
        : await api("/api/admin/sync_agent", { method: "POST", body: { languages: langs } });
      out.textContent = (res.env_hint ? `✓ Done. Add this to Railway variables so it survives restarts:\n${res.env_hint}\n\n` : "") + JSON.stringify(res, null, 2);
      if (!dry) S.config = await api("/api/config");
      setMode(S.mode);
    } catch (e) {
      out.textContent = "✗ " + e.message;
    }
  }

  // ------------------------------------------------------------------ boot
  async function boot() {
    S.config = await api("/api/config");
    renderPresets();
    applyPreset(S.config.default_preset);
    $("#livePhone").value = store.get("livePhone", "");
    setMode(S.mode);
    setupMic();
    $$("#modeSeg button").forEach((b) => b.addEventListener("click", async () => {
      setMode(b.dataset.mode);
      try { S.config = await api("/api/config"); setMode(b.dataset.mode); } catch { /* keep cached config */ }
    }));
    $("#parseBtn").addEventListener("click", parseBrief);
    $("#launchBtn").addEventListener("click", launch);
    $("#lockBestBtn").addEventListener("click", () => lockWinner());
    $("#newBtn").addEventListener("click", () => {
      if (S.ws) { S.ws.onclose = null; S.ws.close(); }
      clearInterval(S.poll); clearInterval(S.tickTimer);
      S.session = null; S.modalShownFor = null;
      $("#cards").innerHTML = "";
      $("#warRoom").hidden = true; $("#ticker").hidden = true; $("#chatPanel").hidden = false;
      $("#providersPanel").hidden = !S.providers.length;
      history.replaceState(null, "", location.pathname);
    });
    $("#adminBtn").addEventListener("click", openAdmin);
    $("#chatSend").addEventListener("click", () => sendChat());
    $("#chatInput").addEventListener("keydown", (e) => { if (e.key === "Enter") sendChat(); });
    $("#findBtn").addEventListener("click", findProviders);
    $("#callBtn").addEventListener("click", callProviders);
    $("#geoBtn").addEventListener("click", useLocation);
    $("#classicBtn").addEventListener("click", () => { $("#briefPanel").hidden = !$("#briefPanel").hidden; });
    setupChatMic();
    sendChat("");
    $("#previewBtn").addEventListener("click", () => adminCall(true));
    $("#checkBtn").addEventListener("click", async () => {
      store.set("adminToken", $("#adminToken").value);
      const out = $("#adminOut");
      out.hidden = false;
      out.textContent = "Checking Bolna key…";
      try { out.textContent = "✓ Bolna key works\n" + JSON.stringify(await api("/api/admin/bolna_check"), null, 2); }
      catch (e) { out.textContent = "✗ " + e.message; }
    });
    $("#syncBtn").addEventListener("click", () => adminCall(false));
    const m = location.hash.match(/s=([a-f0-9]+)/);
    if (m) { try { startWarRoom(await api(`/api/session/${m[1]}`)); } catch { /* expired */ } }
  }
  boot().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<p class="err" style="padding:16px">Failed to load: ${esc(e.message)}</p>`); });
})();
