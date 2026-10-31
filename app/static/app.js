/* Swarm: a voice-first concierge. Talk → it finds providers → calls them in the local language → you confirm.
   Vanilla JS, no build step. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const inr = (n) => (n == null ? "—" : "₹" + Number(n).toLocaleString("en-IN"));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const store = {
    get(k, d) { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
  };

  const S = {
    config: null, langs: {}, commands: { yes: [], no: [] },
    mode: store.get("mode", "simulation"),
    voiceOn: store.get("voiceOn", "1") === "1",
    handsFree: store.get("handsFree", "1") === "1",
    glossOn: store.get("glossOn", "1") === "1",
    userLang: store.get("userLang", "auto"),   // "auto": detect the language of every message; or a code picked in ⋯
    spokenLang: "en",                          // the language Swarm answers in (detected, or the one picked)
    langLocked: false,                         // fixed for this conversation once detected (or picked)
    lines: {}, sttDown: false,
    conv: { id: null, task: {}, ready: false, trace: [] },
    callLang: "hi", callLangReason: "", callLangLocked: false,
    phase: "intake", interacted: false,
    geo: null, geoDenied: false,
    providers: [], provInfo: null, sel: new Set(), countdown: null,
    session: null, ws: null, poll: null, prev: {}, narrated: new Set(), recSpoken: null, openLegs: new Set(),
  };

  // ================================================================ API
  async function api(path, opts = {}) {
    const headers = { "Content-Type": "application/json" };
    const tok = $("#adminToken")?.value || store.get("adminToken", "");
    if (tok) headers["X-Admin-Token"] = tok;
    const r = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) }, body: opts.body ? JSON.stringify(opts.body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.detail || data.error || r.statusText);
    return data;
  }
  function toast(text, ms = 3200) {
    const t = $("#toast");
    t.textContent = text; t.hidden = false;
    clearTimeout(toast.t); toast.t = setTimeout(() => { t.hidden = true; }, ms);
  }

  // ================================================================ the user's language
  // The browser's language, if it's one of the 11: what Swarm greets in before the user says anything
  function browserLang() {
    for (const tag of navigator.languages || [navigator.language || "en"]) {
      const base = String(tag).toLowerCase().split("-")[0];
      const code = base === "or" ? "od" : base;
      if (S.langs[code]) return code;
    }
    return "en";
  }
  async function loadLines(code) {
    if (S.lines[code]) return;
    try { S.lines[code] = await api(`/api/lines/${encodeURIComponent(code)}`); } catch { /* English until it loads */ }
  }
  // Every reply says which language it's in; narration, the verdict and the deal follow it
  async function setSpokenLang(code) {
    if (!code || !S.langs[code]) return;
    const changed = code !== S.spokenLang;
    S.spokenLang = code;
    await loadLines(code);
    if (changed && S.userLang === "auto" && S.conv.id) toast(`${S.langs[code].native} · ${S.langs[code].name}`, 1800);
  }

  // ================================================================ voice in / out
  const Voice = {
    SR: window.SpeechRecognition || window.webkitSpeechRecognition,
    rec: null, stopRecorder: null, audio: null, utter: null, speakDone: null,
    canRecord() { return !!(navigator.mediaDevices?.getUserMedia && (window.AudioContext || window.webkitAudioContext)); },
    canListen() { return !!(this.SR || this.canRecord()); },
    tag(code) { return S.langs[code]?.browser || "en-IN"; },

    listen(code) {
      return this.SR ? this.listenBrowser(code) : this.listenRecorder(code);
    },
    listenBrowser(code) {
      return new Promise((resolve) => {
        const rec = new this.SR();
        this.rec = rec;
        rec.lang = this.tag(code); rec.interimResults = true; rec.continuous = false; rec.maxAlternatives = 1;
        let text = "", error = "";
        rec.onresult = (ev) => {
          text = [...ev.results].map((r) => r[0].transcript).join(" ").trim();
          showUser(text, true);
        };
        rec.onerror = (e) => { error = e.error || "error"; };
        rec.onend = () => { this.rec = null; resolve({ text, error: text ? "" : error }); };
        try { rec.start(); } catch (e) { this.rec = null; resolve({ text: "", error: "busy" }); }
      });
    },
    // Browsers without speech recognition: record 16 kHz WAV, stop on silence, transcribe with Gemini on the server
    async listenRecorder(code) {
      let stream;
      try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
      catch { return { text: "", error: "not-allowed" }; }
      const Ctx = window.AudioContext || window.webkitAudioContext;
      const ctx = new Ctx();
      const src = ctx.createMediaStreamSource(stream);
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      let spoke = false, silent = 0, total = 0, done = false;
      return new Promise((resolve) => {
        const finish = async (cancel = false) => {
          if (done) return; done = true;
          proc.disconnect(); src.disconnect(); stream.getTracks().forEach((t) => t.stop());
          const rate = ctx.sampleRate; ctx.close();
          this.stopRecorder = null;
          if (cancel || !spoke) { resolve({ text: "", error: spoke ? "" : "no-speech" }); return; }
          setOrb("thinking", "Transcribing…");
          try {
            const r = await api("/api/stt", { method: "POST", body: { audio: b64(wav16k(chunks, rate)), mime: "audio/wav", language: code } });
            resolve({ text: (r.text || "").trim(), error: "", language: r.language || "" });
          } catch (e) { resolve({ text: "", error: e.message, failed: true }); }
        };
        this.stopRecorder = () => finish(false);
        proc.onaudioprocess = (e) => {
          const data = e.inputBuffer.getChannelData(0);
          chunks.push(new Float32Array(data));
          let sum = 0; for (let i = 0; i < data.length; i++) sum += data[i] * data[i];
          const rms = Math.sqrt(sum / data.length), dt = data.length / ctx.sampleRate;
          total += dt;
          if (rms > 0.025) { spoke = true; silent = 0; } else if (spoke) silent += dt;
          $("#orb").style.setProperty("--level", Math.min(1, rms * 12).toFixed(2));
          if ((spoke && silent > 1.3) || total > 15 || (!spoke && total > 8)) finish();
        };
        src.connect(proc); proc.connect(ctx.destination);
      });
    },
    stopListening() {
      if (this.rec) { try { this.rec.stop(); } catch { /* already stopped */ } }
      if (this.stopRecorder) this.stopRecorder();
    },

    voiceFor(code) {
      const voices = window.speechSynthesis?.getVoices() || [];
      const tag = this.tag(code).toLowerCase(), base = tag.split("-")[0];
      return voices.find((v) => v.lang.replace("_", "-").toLowerCase() === tag)
        || voices.find((v) => v.lang.toLowerCase().startsWith(base === "od" ? "or" : base))
        || null;
    },
    // Speak a line in its language: the browser's voice if it has one, else a Gemini voice, else stay silent
    async say(text, code = "en") {
      this.stopSpeaking();
      const line = String(text || "").replace(/\*\*/g, "").replace(/\s*\(Say yes[^)]*\)/, "").trim();
      if (!S.voiceOn || !line || !S.interacted) return;
      const voice = this.voiceFor(code);
      setOrb("speaking");
      if (voice) {
        await new Promise((resolve) => {
          const u = new SpeechSynthesisUtterance(line);
          u.voice = voice; u.lang = voice.lang; u.rate = 1.02;
          const safety = setTimeout(resolve, 2500 + line.length * 110);
          u.onend = u.onerror = () => { clearTimeout(safety); resolve(); };
          this.utter = u; this.speakDone = resolve;
          window.speechSynthesis.speak(u);
        });
      } else if (S.config?.gemini_enabled) {
        try {
          const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: line }) });
          if (r.ok) {
            const url = URL.createObjectURL(await r.blob());
            await new Promise((resolve) => {
              const a = new Audio(url);
              this.audio = a; this.speakDone = resolve;
              a.onended = a.onerror = () => resolve();
              a.play().catch(resolve);
            });
            URL.revokeObjectURL(url);
          }
        } catch { /* no voice: the text is on screen */ }
      }
      this.audio = null; this.utter = null; this.speakDone = null;
      if ($("#orb").dataset.state === "speaking") setOrb("idle");
    },
    stopSpeaking() {
      try { window.speechSynthesis?.cancel(); } catch { /* none */ }
      if (this.audio) { this.audio.pause(); this.audio = null; }
      if (this.speakDone) { const d = this.speakDone; this.speakDone = null; d(); }
    },
  };

  function wav16k(chunks, rate) {
    const len = chunks.reduce((a, c) => a + c.length, 0);
    const all = new Float32Array(len); let off = 0;
    for (const c of chunks) { all.set(c, off); off += c.length; }
    const ratio = rate / 16000, n = Math.floor(len / ratio);
    const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
    const str = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
    str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt "); v.setUint32(16, 16, true);
    v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, 16000, true); v.setUint32(28, 32000, true);
    v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, n * 2, true);
    for (let i = 0; i < n; i++) {
      const s = Math.max(-1, Math.min(1, all[Math.floor(i * ratio)]));
      v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return buf;
  }
  function b64(buf) {
    const bytes = new Uint8Array(buf); let bin = "";
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  // ================================================================ the stage
  function setOrb(state, hint) {
    const orb = $("#orb");
    orb.dataset.state = state;
    orb.setAttribute("aria-label", state === "listening" ? "Stop listening" : state === "speaking" ? "Stop and talk" : "Tap to talk");
    const hints = { idle: Voice.canListen() ? "Tap and speak" : "Type below", listening: "Listening… tap when done", thinking: "Thinking…", speaking: "Tap to interrupt" };
    $("#orbHint").textContent = hint ?? hints[state] ?? "";
  }
  function dock(on) {
    $("#stage").classList.toggle("compact", on);
    document.body.classList.toggle("docked", on);
  }
  function showBot(text) { $("#botLine").innerHTML = esc(text).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); }
  function showUser(text, interim = false) {
    const el = $("#userLine");
    el.textContent = text ? (interim ? text : `“${text}”`) : "";
    el.classList.toggle("interim", interim);
  }
  function renderChips(list) {
    $("#chips").innerHTML = (list || []).map((t) => `<button class="chip" type="button">${esc(t)}</button>`).join("");
    $$("#chips .chip").forEach((b) => b.addEventListener("click", () => { S.interacted = true; onUtterance(b.textContent); }));
  }

  async function listen() {
    if (!Voice.canListen()) { toast("Voice input isn't available in this browser — please type."); $("#typeInput").focus(); return; }
    S.interacted = true;
    Voice.stopSpeaking();
    setOrb("listening");
    showUser("");
    // Auto: record and let the server work out which of the 11 languages it is (browser speech recognition only
    // understands the one language it is told). Once the conversation's language is fixed, it is transcribed in
    // that language only. A language picked in ⋯, or no Gemini, uses the browser's recognition.
    const detect = S.userLang === "auto" && S.config?.gemini_enabled && !S.sttDown && Voice.canRecord();
    const { text, error, language, failed } = detect ? await Voice.listenRecorder(S.langLocked ? S.spokenLang : "")
      : await Voice.listen(S.spokenLang);
    if (error === "not-allowed" || error === "service-not-allowed") {
      setOrb("idle"); toast("Microphone is blocked. Allow it in the browser, or type instead."); return;
    }
    if (detect && failed) {
      S.sttDown = true;
      toast(`Language detection is busy, so I'll listen in ${S.langs[S.spokenLang]?.name || "English"}. Pick your language in ⋯ to change it.`, 6000);
    }
    if (!text) { setOrb("idle", "Didn't catch that — tap and try again"); return; }
    await onUtterance(text, language);
  }

  async function onUtterance(text, heard = "") {
    text = String(text || "").trim();
    if (!text) return;
    if (text.startsWith("📍") || /^use my (current |live )?location$/i.test(text)) { showUser(text); await useLocation(); return; }
    if (S.phase !== "intake" && isRepeat(text)) { showUser(text); Narrator.push($("#botLine").textContent, true); return; }
    if (S.phase === "calling" || S.phase === "done") { showUser(text); await onCallCommand(text); return; }
    if (S.phase === "shortlist") { showUser(text); onShortlistCommand(text); return; }
    await sendChat(text, heard ? { heard_lang: heard } : {});
  }

  // ================================================================ 1. the conversation
  async function sendChat(text, extra = {}) {
    if (text) showUser(text);
    setOrb("thinking");
    let r;
    try {
      r = await api("/api/chat", { method: "POST", body: {
        conversation_id: S.conv.id, message: text || "", lat: S.geo?.lat, lng: S.geo?.lng,
        user_lang: S.userLang, heard_lang: S.conv.id ? undefined : browserLang(),
        returning: !S.conv.id && store.get("seen", "") === "1", ...extra } });
    } catch (e) {
      setOrb("idle"); showBot("Sorry, I couldn't reach the server: " + e.message); return;
    }
    S.conv = { id: r.conversation_id, task: r.task || {}, ready: r.ready, trace: r.trace || [], service: r.service_name };
    store.set("seen", "1");   // next visit: a short "hello again" instead of the full introduction
    S.langLocked = !!r.user_lang_locked;
    await setSpokenLang(r.user_lang);
    if (r.call_language) {
      S.callLang = r.call_language.code; S.callLangReason = r.call_language.reason || ""; S.callLangLocked = !!r.call_language.locked;
    }
    renderLangPill(); renderTask(); renderLog();
    showBot(r.reply);
    renderChips(r.suggestions);
    if (r.action === "go") {
      await Voice.say(r.reply, r.reply_language);
      await startSearch();
      return;
    }
    if (!S.geo && !S.geoDenied && /^\W*(near(by| me)?|around me|here|my (current |live )?location)\W*$/i.test(S.conv.task.location || "")) {
      await useLocation();
      return;
    }
    await Voice.say(r.reply, r.reply_language);
    if (S.handsFree && S.interacted && S.phase === "intake" && !S.typing && Voice.canListen()) { await sleep(150); listen(); }
    else setOrb("idle");
  }

  function renderTask() {
    const t = S.conv.task || {};
    $("#task").hidden = !t.service_key;
    if (!t.service_key) return;
    const appt = t.goal === "appointment";
    const where = t.pickup && t.drop ? `${t.pickup} → ${t.drop}` : t.location;
    const bits = [
      t.service_label, where, t.when, t.details,
      appt ? "appointment" : (t.walkaway_ceiling ? `${inr(t.target_price)}–${inr(t.walkaway_ceiling)}` : ""),
      S.langs[S.callLang] ? `calls in ${S.langs[S.callLang].native}` : "",
    ].filter(Boolean);
    $("#taskLine").innerHTML = bits.map((b) => `<span class="t">${esc(b)}</span>`).join("") + `<span class="edit">Edit</span>`;
    $("#tService").value = t.service_label || "";
    $("#tLocation").value = t.pickup && t.drop ? `${t.pickup} → ${t.drop}` : (t.location || "");
    $("#tWhen").value = t.when || "";
    $("#tDetails").value = t.details || "";
    $("#tTarget").value = t.target_price || "";
    $("#tCeiling").value = t.walkaway_ceiling || "";
    $$(".budget").forEach((el) => { el.hidden = appt; });
    $("#tGoal").textContent = appt ? "Appointment: I book a slot and get the fee. No haggling with clinics."
      : "Your target and max are never told to the calling agent, so it can't be talked above them.";
  }
  function readTask() {
    const t = S.conv.task;
    t.service_label = $("#tService").value.trim() || t.service_label;
    const loc = $("#tLocation").value.trim();
    if (loc.includes("→")) { [t.pickup, t.drop] = loc.split("→").map((x) => x.trim()); t.location = t.pickup; } else if (loc) t.location = loc;
    t.when = $("#tWhen").value.trim() || t.when;
    t.details = $("#tDetails").value.trim();
    if (t.goal !== "appointment") {
      const tgt = parseInt($("#tTarget").value, 10), max = parseInt($("#tCeiling").value, 10);
      if (tgt > 0) t.target_price = tgt;
      if (max > 0) t.walkaway_ceiling = Math.max(max, t.target_price || 0);
    }
  }

  // ---------------------------------------------------------------- location
  function getGeo() {
    if (S.geo) return Promise.resolve(S.geo);
    if (!navigator.geolocation || S.geoDenied) return Promise.resolve(null);
    return new Promise((resolve) => navigator.geolocation.getCurrentPosition((p) => {
      S.geo = { lat: p.coords.latitude, lng: p.coords.longitude }; resolve(S.geo);
    }, () => { S.geoDenied = true; resolve(null); }, { timeout: 10000, maximumAge: 120000, enableHighAccuracy: true }));
  }
  async function useLocation() {
    S.geoDenied = false;
    setOrb("thinking", "Finding where you are…");
    if (!(await getGeo())) { const line = narr("no_location"); setOrb("idle"); showBot(line); await Voice.say(line, narrLang()); return; }
    if (S.conv.task && /near|location|here/i.test(S.conv.task.location || "")) S.conv.task.location = "";
    await sendChat("");   // the server names the area from your position and picks its language
  }

  // ---------------------------------------------------------------- call language
  function renderLangPill() {
    const l = S.langs[S.callLang];
    $("#langPillText").textContent = l ? l.native : S.callLang;
    $("#langPill").title = `Calls in ${l?.name || S.callLang}${S.callLangReason ? " — " + S.callLangReason : ""}`;
  }
  function openLangSheet() {
    $("#langReason").textContent = S.callLangReason ? `Now: ${S.langs[S.callLang]?.name} — ${S.callLangReason}.` : "";
    $("#langGrid").innerHTML = Object.values(S.langs).map((l) =>
      `<button type="button" class="lang ${l.code === S.callLang ? "on" : ""}" data-code="${esc(l.code)}"><b>${esc(l.native)}</b><span>${esc(l.name)}</span></button>`).join("");
    $$("#langGrid .lang").forEach((b) => b.addEventListener("click", () => pickCallLanguage(b.dataset.code)));
    $("#langSheet").showModal();
  }
  async function pickCallLanguage(code) {
    $("#langSheet").close();
    S.callLang = code; S.callLangLocked = true; S.callLangReason = `you chose ${S.langs[code]?.name}`;
    renderLangPill(); renderTask();
    toast(`Calls will be in ${S.langs[code]?.native} · ${S.langs[code]?.name}`);
    if (S.conv.id && S.phase === "intake") {
      try {
        const r = await api("/api/chat", { method: "POST", body: { conversation_id: S.conv.id, message: "", call_language: code, user_lang: S.userLang } });
        S.conv.task = r.task || S.conv.task; S.conv.trace = r.trace || S.conv.trace; renderTask(); renderLog();
      } catch { /* applied when the calls start */ }
    }
  }

  // ================================================================ 2. who to call
  async function startSearch() {
    readTask();
    S.phase = "shortlist";
    dock(true);
    const t = S.conv.task;
    const say = narr("searching", { service: S.conv.service || t.service_label || "", place: (t.location || "").split(",")[0] });
    showBot(say); setOrb("thinking", "Searching the map…");
    let r;
    try {
      await getGeo();
      r = await api("/api/providers/search", { method: "POST", body: { task: t, lat: S.geo?.lat, lng: S.geo?.lng } });
    } catch (e) { setOrb("idle"); S.phase = "intake"; dock(false); showBot("Search failed: " + e.message); return; }
    S.providers = r.providers || []; S.provInfo = r;
    if (!S.providers.length) {
      S.phase = "intake"; dock(false); setOrb("idle");
      const line = narr("none", {}); showBot(line); await Voice.say(line, narrLang()); return;
    }
    const k = Math.min(S.config.auto_call_count || 3, S.providers.length);
    S.sel = new Set(S.providers.slice(0, k).map((p) => p.id));
    renderShortlist();
    $("#shortlist").hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
    const line = narr("found", { n: S.providers.length, k, language: langName(S.callLang) });
    showBot(line);
    setOrb("idle", "Say “wait” to choose yourself");
    startCountdown(5);
    await Voice.say(line, narrLang());
  }
  function renderShortlist() {
    const r = S.provInfo || {}, t = S.conv.task || {};
    $("#slTitle").textContent = `Best-rated ${t.service_label || "providers"} near ${(t.location || "you").split(",")[0]}`;
    $("#slSource").textContent = r.providers?.[0]?.source ? r.providers[0].source.replace(/ \(.*\)$/, "") : "";
    const test = r.test_mode && S.mode !== "simulation";
    $("#testNote").hidden = !test;
    $("#testNote").textContent = `Test mode: every call rings ${r.test_phone || "your test phone"}, not the business. Set CALL_REAL_NUMBERS=true for real calls.`;
    $("#slList").innerHTML = S.providers.map((p, i) => {
      const meta = [p.rating != null ? `<span class="stars">★ ${p.rating.toFixed(1)}</span> (${Number(p.reviews || 0).toLocaleString("en-IN")})` : "no ratings",
        p.distance_km != null ? `${p.distance_km} km` : "", p.open_now === true ? "open now" : p.open_now === false ? "closed now" : ""].filter(Boolean).join(" · ");
      const links = [p.phone ? `<a href="tel:${esc(p.phone)}">${esc(p.phone_local || p.phone)}</a>` : "",
        p.maps_uri ? `<a href="${esc(p.maps_uri)}" target="_blank" rel="noopener">map</a>` : "",
        p.website ? `<a href="${esc(p.website)}" target="_blank" rel="noopener">website</a>` : ""].filter(Boolean).join(" · ");
      return `<li><label class="prov ${S.sel.has(p.id) ? "sel" : ""}" data-id="${esc(p.id)}">
        <input type="checkbox" ${S.sel.has(p.id) ? "checked" : ""} aria-label="Call ${esc(p.name)}">
        <div><b>${esc(p.name)}</b><div class="meta">${meta}</div>${p.address ? `<div class="meta">${esc(p.address)}</div>` : ""}${links ? `<div class="meta">${links}</div>` : ""}</div>
        <span class="rank">#${i + 1}</span></label></li>`;
    }).join("");
    $$("#slList .prov").forEach((row) => $("input", row).addEventListener("change", (ev) => {
      stopCountdown();
      const id = row.dataset.id;
      if (ev.target.checked) {
        if (S.sel.size >= 5) { ev.target.checked = false; toast("Up to 5 at a time."); return; }
        S.sel.add(id);
      } else S.sel.delete(id);
      row.classList.toggle("sel", ev.target.checked);
      updateCallBtn();
    }));
    updateCallBtn();
  }
  function updateCallBtn() {
    const n = S.sel.size;
    $("#callBtn").disabled = !n;
    $("#callBtn").textContent = S.mode === "simulation" ? `Simulate ${n} call${n === 1 ? "" : "s"}` : `Call ${n} now`;
  }
  function startCountdown(seconds) {
    stopCountdown();
    let left = seconds;
    const tick = () => {
      $("#countdown").textContent = `Calling in ${left}… tap any provider to choose yourself`;
      if (left-- <= 0) { stopCountdown(); callSelected(); }
    };
    tick();
    S.countdown = setInterval(tick, 1000);
  }
  function stopCountdown() { clearInterval(S.countdown); S.countdown = null; $("#countdown").textContent = ""; }
  function onShortlistCommand(text) {
    if (isNo(text) || /\b(wait|ruko|hold|choose|pick)\b/i.test(text)) { stopCountdown(); showBot(narr("choose")); setOrb("idle"); return; }
    if (isYes(text)) { stopCountdown(); callSelected(); return; }
    setOrb("idle");
  }
  async function callSelected() {
    stopCountdown();
    const chosen = S.providers.filter((p) => S.sel.has(p.id));
    if (!chosen.length) return;
    readTask();
    $("#callBtn").disabled = true;
    try {
      const session = await api("/api/session", { method: "POST", body: { task: S.conv.task, providers: chosen, mode: S.mode, language: S.callLang } });
      startCalls(session);
    } catch (e) {
      toast("Couldn't start the calls: " + e.message, 6000);
      $("#callBtn").disabled = false;
    }
  }

  // ================================================================ 3. the calls
  function startCalls(session) {
    S.phase = "calling"; S.session = session; S.prev = {}; S.narrated = new Set(); S.recSpoken = null; S.openLegs = new Set();
    dock(true);
    window.scrollTo({ top: 0 });
    $("#shortlist").hidden = true; $("#task").hidden = true;
    $("#calls").hidden = false; $("#callList").innerHTML = "";
    renderChips([]);
    history.replaceState(null, "", "#s=" + session.session_id);
    const n = session.legs.length;
    showBot(narr("calling", { n, language: langName(session.language) }));
    setOrb("idle", "Say “stop” to end the calls");
    render(session);
    connect(session.session_id);
  }
  function connect(sid) {
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    clearInterval(S.poll);
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/session/${sid}`);
    S.ws = ws;
    ws.onmessage = (m) => { if (m.data === "pong") return; try { const msg = JSON.parse(m.data); if (msg.session) render(msg.session); } catch { /* ignore */ } };
    ws.onclose = () => {
      S.poll = setInterval(async () => { try { render(await api(`/api/session/${sid}`)); } catch { /* ignore */ } }, 2000);
      setTimeout(() => { if (S.session?.session_id === sid) connect(sid); }, 4000);
    };
    ws.onopen = () => clearInterval(S.poll);
    const ping = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); else clearInterval(ping); }, 20000);
  }

  function render(session) {
    if (!session || (S.session && session.session_id !== S.session.session_id)) return;
    S.session = session;
    const r = session.request, appt = r.goal === "appointment";
    const best = session.current_best_quote, bestLeg = best ? session.legs.find((l) => l.leg_id === best.leg_id) : null;
    $("#stBestL").textContent = appt ? "Best slot" : "Best so far";
    $("#stBest").textContent = best ? (appt ? (bestLeg?.slot || "—") : inr(best.price)) : "—";
    $("#stSavedL").textContent = appt ? "Slots offered" : "Saved";
    $("#stSaved").textContent = appt ? `${session.legs.filter((l) => l.slot).length}/${session.legs.length}`
      : (session.savings_vs_worst_first_quote ? inr(session.savings_vs_worst_first_quote) : "—");
    const list = $("#callList");
    session.legs.forEach((leg) => {
      let li = $(`#leg-${leg.leg_id}`);
      if (!li) { li = document.createElement("li"); li.id = `leg-${leg.leg_id}`; list.appendChild(li); }
      renderLeg(li, leg, session);
    });
    renderVerdict(session);
    renderLog();
    narrate(session);
    if (session.whatsapp && session.locked_leg_id && !S.dealShown) { S.dealShown = session.session_id; showDeal(session); }
  }

  const STATUS = { PENDING: "Queued", DIALING: "Ringing", CONNECTED: "On the call", QUOTED: "Quoted", COUNTERED: "Negotiating", HOLD: "Agreed", WON: "Confirmed", RELEASED: "Released", ENDED: "No deal", FAILED: "Failed" };
  function renderLeg(li, leg, session) {
    const r = session.request, appt = r.goal === "appointment", pv = leg.provider || {};
    const lines = (leg.transcript || []).filter((t) => t.role !== "system");
    const last = lines[lines.length - 1];
    const meta = [pv.rating != null ? `★ ${Number(pv.rating).toFixed(1)}` : "", pv.distance_km != null ? `${pv.distance_km} km` : "",
      leg.mode === "live" ? "live call" : "simulated", pv.test_number ? "test phone" : ""].filter(Boolean).join(" · ");
    const flags = [];
    if (leg.first_hidden) flags.push(`<span class="flag hidden">${inr(leg.first_hidden)} hidden extras caught</span>`);
    const saved = leg.first_all_in && leg.best_all_in && leg.first_all_in > leg.best_all_in ? leg.first_all_in - leg.best_all_in : 0;
    if (saved) flags.push(`<span class="flag save">${inr(saved)} negotiated off</span>`);
    for (const x of leg.excluded || []) flags.push(`<span class="flag">${esc(x.item)} ${x.amount ? inr(x.amount) : "at actuals"}, paid separately</span>`);
    const captured = [...(leg.extras || []).filter((x) => x.kind !== "excluded").map((x) => `${x.item}: ${x.status === "included" ? "included" : (x.amount ? inr(x.amount) + " " : "") + "extra"}`), ...(leg.details || [])];
    const canLock = !session.locked_leg_id && leg.status !== "FAILED" && (appt ? !!leg.slot : leg.best_all_in && (!r.has_budget || leg.best_all_in <= r.walkaway_ceiling));
    const price = appt ? (leg.slot || (leg.best_all_in ? inr(leg.best_all_in) : "")) : (leg.best_all_in ? inr(leg.best_all_in) : "");
    const bubble = (t) => `<div class="bubble ${esc(t.role)}">${esc(t.text)}${t.gloss ? `<p class="gloss">${esc(t.gloss)}</p>` : ""}</div>`;
    const wasOpen = S.openLegs.has(leg.leg_id);
    li.className = `call st-${leg.status}` + (leg.status === "WON" ? " won" : "") + (leg.status === "RELEASED" ? " released" : "");
    li.innerHTML = `
      <div class="call-top">
        <span class="avatar">${esc((leg.vendor_name || "?").trim()[0])}</span>
        <div class="who"><b>${esc(leg.vendor_name)}</b><span class="meta">${esc(meta)}</span></div>
        <div class="price"><b>${esc(price || "")}</b><span class="status">${esc(STATUS[leg.status] || leg.status)}</span></div>
      </div>
      ${last ? `<p class="last"><span class="who-said">${last.role === "agent" ? "Swarm" : "Them"}</span>${esc(last.text)}</p>${last.gloss ? `<p class="gloss">${esc(last.gloss)}</p>` : ""}` : ""}
      ${flags.length ? `<div class="flags">${flags.join("")}</div>` : ""}
      ${leg.error ? `<p class="err">${esc(leg.error)}</p>` : ""}
      <details class="more" ${wasOpen ? "open" : ""}><summary>Full call</summary>
        <div class="transcript">${(leg.transcript || []).map(bubble).join("") || '<div class="bubble system">Waiting to dial…</div>'}</div>
        ${captured.length ? `<ul class="captured">${captured.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` : ""}
        ${leg.recording_url ? `<audio controls preload="none" src="${esc(leg.recording_url)}"></audio>` : ""}
        ${canLock ? `<button class="btn" type="button" data-lock="${esc(leg.leg_id)}">Choose ${esc(leg.vendor_name.split(" ")[0])}${price ? " · " + esc(price) : ""}</button>` : ""}
      </details>`;
    $("details", li).addEventListener("toggle", (e) => { if (e.target.open) S.openLegs.add(leg.leg_id); else S.openLegs.delete(leg.leg_id); });
    $("[data-lock]", li)?.addEventListener("click", () => lockWinner(leg.leg_id));
    if (wasOpen) { const tr = $(".transcript", li); tr.scrollTop = tr.scrollHeight; }
  }

  function renderVerdict(session) {
    const rec = session.recommendation, v = $("#verdict");
    if (!rec || !rec.all_done || !rec.leg_id) { v.hidden = true; return; }
    v.hidden = false;
    $("#vMain").textContent = rec.summary;
    $("#vReasons").innerHTML = (rec.reasons || []).map((x) => `<li>${esc(x)}</li>`).join("");
    $(".verdict-k", v).textContent = narr(rec.confirmed ? "confirmed_k" : "recommended");
    $("#vConfirm").textContent = narr("confirm_btn"); $("#vOther").textContent = narr("other_btn");
    $("#vConfirm").hidden = !!rec.confirmed; $("#vOther").hidden = !!rec.confirmed;
  }

  // ---------------------------------------------------------------- spoken narration of the calls
  // Narration, the verdict and the deal, in the user's language (app/userlex.py; English until a table loads)
  const narrLang = () => (S.lines[S.spokenLang] ? S.spokenLang : "en");
  const table = () => S.lines[narrLang()] || S.lines.en || { lines: {}, language_names: {} };
  const langName = (code) => table().language_names?.[code] || S.langs[code]?.name || "";
  const narr = (key, vars = {}) => (table().lines[key] || S.lines.en?.lines[key] || "").replace(/\{(\w+)\}/g, (_, k) => vars[k] ?? "");

  const Narrator = {
    queue: [], busy: false,
    push(text, important = false) {
      showBot(text);
      if (!important) { this.queue = this.queue.filter((q) => q.important).slice(-1); }
      this.queue.push({ text, important });
      this.run();
    },
    async run() {
      if (this.busy) return;
      this.busy = true;
      while (this.queue.length) {
        const { text } = this.queue.shift();
        if ($("#orb").dataset.state === "listening") break;
        await Voice.say(text, narrLang());
      }
      this.busy = false;
    },
  };

  function narrate(session) {
    const appt = session.request.goal === "appointment";
    for (const leg of session.legs) {
      const prev = S.prev[leg.leg_id] || {};
      const name = leg.vendor_name.split(",")[0];
      const once = (key, text, important) => { const k = `${leg.leg_id}:${key}`; if (!S.narrated.has(k)) { S.narrated.add(k); Narrator.push(text, important); } };
      if (!appt && leg.first_all_in && !prev.first_all_in) {
        once("quote", leg.first_hidden ? narr("hidden", { name, said: inr(leg.first_stated), price: inr(leg.first_all_in) }) : narr("quoted", { name, price: inr(leg.first_all_in) }));
      }
      if (leg.status !== prev.status) {
        if (leg.status === "HOLD") once("hold", appt ? narr("slot", { name, slot: leg.slot_user || leg.slot }) : narr("hold", { name, price: inr(leg.best_all_in) }), true);
        if (leg.status === "FAILED") once("failed", narr("failed", { name }));
        if (leg.status === "ENDED" && !leg.slot) once("ended", leg.last_action === "WALK_AWAY" ? narr("walk", { name }) : narr("nodeal", { name }));
      }
      S.prev[leg.leg_id] = { status: leg.status, first_all_in: leg.first_all_in };
    }
    const rec = session.recommendation;
    if (rec?.all_done && S.recSpoken !== session.session_id && !session.locked_leg_id) {
      S.recSpoken = session.session_id;
      S.phase = "done";
      const what = rec.what || (appt ? [rec.slot, rec.price ? `fee ${inr(rec.price)}` : ""].filter(Boolean).join(", ") : `${inr(rec.price)} all-in`);
      const line = rec.leg_id ? narr("recommend", { name: rec.vendor_name, what }) : narr("nobody", {});
      S.log.push({ agent: "advisor", text: rec.leg_id ? `recommends ${rec.summary} (${(rec.reasons || []).join("; ")})` : rec.summary });
      renderLog();
      Narrator.queue = [];
      Narrator.push(line, true);
      (async () => {
        while (Narrator.busy || Narrator.queue.length) await sleep(150);
        if (S.handsFree && S.interacted && rec.leg_id && !S.session.locked_leg_id && Voice.canListen()) listen();
        else setOrb("idle", rec.leg_id ? "Say “yes” to confirm" : "");
      })();
    }
  }

  // ---------------------------------------------------------------- voice commands during calls
  function matches(text, words) {
    const t = ` ${String(text).toLowerCase().replace(/[.,!?;:"'()।]/g, " ").replace(/\s+/g, " ").trim()} `;
    return words.some((w) => t.includes(` ${w} `));
  }
  const isNo = (t) => matches(String(t).replace(/no problem/gi, ""), S.commands.no);
  const isYes = (t) => !isNo(t) && String(t).split(/\s+/).length <= 6 && matches(t, S.commands.yes);
  // "what? / फिर से": the whole message, as on the server (app/languages.py is_repeat)
  const isRepeat = (t) => (S.commands.repeat || []).includes(String(t).toLowerCase().replace(/[.,!?;:"'()\[\]–—।॥]/g, " ").replace(/\s+/g, " ").trim().replace(/ (please|ji|plz)$/, ""));

  async function onCallCommand(text) {
    const s = S.session; if (!s) return;
    const rec = s.recommendation;
    if (/\b(stop|end|cancel)( the)? calls?\b|^stop$|^रुको$|^बंद करो$/i.test(text)) {
      try { await api(`/api/session/${s.session_id}/stop`, { method: "POST" }); } catch { /* already over */ }
      Narrator.push(narr("stopped", {}), true); return;
    }
    if (/\b(new|start over|another request|naya)\b/i.test(text)) { newRequest(); return; }
    const named = s.legs.find((l) => l.vendor_name && text.toLowerCase().includes(l.vendor_name.split(" ")[0].toLowerCase()) && l.vendor_name.split(" ")[0].length > 2);
    if (named && !s.locked_leg_id) { await lockWinner(named.leg_id); return; }
    if (isNo(text)) { Narrator.push(narr("pick", {}), true); return; }
    if (isYes(text) && rec?.leg_id && !s.locked_leg_id) { await lockWinner(rec.leg_id); return; }
    if (rec?.leg_id && !s.locked_leg_id) Narrator.push(narr("unknown", { name: rec.vendor_name }), true);
    else setOrb("idle");
  }

  // ================================================================ 4. confirm
  async function lockWinner(legId) {
    const s = S.session; if (!s) return;
    try {
      const res = await api(`/api/session/${s.session_id}/lock`, { method: "POST", body: { leg_id: legId } });
      S.dealShown = s.session_id;
      render(res.session);
      showDeal(res.session);
      Narrator.queue = [];
      Narrator.push(narr("confirmed", { name: res.vendor_name }), true);
    } catch (e) { toast(e.message, 5000); }
  }
  function showDeal(session) {
    const leg = session.legs.find((l) => l.leg_id === session.locked_leg_id);
    const p = session.payment || {}, wa = session.whatsapp || {}, appt = session.request.goal === "appointment", paid = p.status === "paid";
    $("#dealTitle").textContent = narr(appt ? "appt_title" : paid ? "paid_title" : "deal_title");
    const callback = leg?.mode === "live" ? `<p class="fine">${esc(narr("calling_back", { name: leg.vendor_name }))}</p>` : "";
    const excluded = (leg?.excluded || []).length ? `<p class="fine">${esc(narr("paid_separately", { items: leg.excluded.map((x, i) => (leg.excluded_user?.[i] || x.item) + (x.amount ? " " + inr(x.amount) : ` (${narr("at_actuals")})`)).join(", ") }))}</p>` : "";
    const saved = session.savings_vs_worst_first_quote ? ` · ${narr("saved", { amount: inr(session.savings_vs_worst_first_quote) })}` : "";
    $("#dealBody").innerHTML = `<div class="deal"><span class="fine">${esc(session.request.user_summary || session.request.summary)}</span><b>${esc(leg?.vendor_name)}</b>
      <span class="big">${appt ? esc(leg?.slot || "") : inr(leg?.best_all_in)}</span>
      ${appt ? `<span class="fine">${esc(leg?.best_all_in ? narr("fee", { fee: inr(leg.best_all_in) }) : narr("fee_tbc"))}</span>`
        : `<span class="fine">${esc(narr("all_inclusive"))}${esc(saved)}${p.amount ? ` · ${esc(narr("advance", { amount: inr(p.amount) }))}` : ""}</span>`}
      ${excluded}<span class="fine">${esc(narr("released"))}</span>${callback}</div>
      ${!appt && p.provider === "mock" && !paid ? `<div class="mockpay"><div class="qr" aria-hidden="true"></div><div><b>Test checkout</b><br>No real money moves. Advance ${inr(p.amount)}</div></div>` : ""}`;
    const actions = [];
    if (p.provider === "razorpay" && p.url) actions.push(`<a class="btn primary" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(narr("pay", { amount: inr(p.amount) }))}</a>`);
    if (!appt && p.provider === "mock" && !paid) actions.push(`<button type="button" class="btn primary" id="mockPayBtn">Pay ${inr(p.amount)} (test)</button>`);
    if (wa.link) actions.push(`<a class="btn" href="${esc(wa.link)}" target="_blank" rel="noopener">${esc(narr(wa.sent ? "sent_wa" : "send_wa"))}</a>`);
    $("#dealActions").innerHTML = actions.join("");
    $("#mockPayBtn")?.addEventListener("click", async () => {
      try { const s = await api(`/api/session/${session.session_id}/mock_pay`, { method: "POST" }); render(s); showDeal(s); } catch (e) { toast(e.message); }
    });
    if (!$("#dealModal").open) $("#dealModal").showModal();
  }

  // ================================================================ agent log
  S.log = [];
  function renderLog() {
    const items = [...(S.conv.trace || []), ...(S.session?.trace || []), ...S.log];
    $("#logBox").hidden = !items.length;
    $("#log").innerHTML = items.slice(-80).map((x) => `<li><span class="agent">${esc(x.agent)}</span><span>${esc(x.text)}</span></li>`).join("");
  }

  // ================================================================ settings
  function applyToggles() {
    document.body.classList.toggle("show-gloss", S.glossOn);
    $("#voiceOn").checked = S.voiceOn; $("#handsFree").checked = S.handsFree; $("#glossOn").checked = S.glossOn;
    $$("#modeSeg input").forEach((i) => { i.checked = i.value === S.mode; });
    const c = S.config || {}, sync = c.bolna_sync || {};
    $("#modeNote").textContent = S.mode === "simulation" ? "No phone calls, no cost."
      : c.bolna_api_key_set ? (sync.state && sync.state !== "ok" ? `Bolna setup: ${sync.state} — ${sync.detail || ""}` : "Agents for each language are created on first use.")
        : "Needs BOLNA_API_KEY on the server (see Bolna setup below).";
  }
  async function openSettings() {
    try { S.config = await api("/api/config"); } catch { /* keep old */ }
    applyToggles();
    const c = S.config, g = c.gemini || {};
    const rows = [
      ["Public URL", c.public_base_url || c.detected_base_url + " (auto)"],
      ["Bolna API key", c.bolna_api_key_set ? "set" : "missing"],
      ["Bolna agents", Object.keys(c.bolna_agent_ids || {}).length ? Object.keys(c.bolna_agent_ids).join(", ") : "none yet (created on first call)"],
      ["Telephony", c.bolna_telephony_provider],
      ["Call LLM", (c.bolna_llm_active || c.bolna_llm_model) + (c.bolna_llm_note ? ` · ${c.bolna_llm_note}` : "")],
      ["Concierge model", c.gemini_enabled ? (g.models?.chat || "rate-limited, using rules") : "off (rules)"],
      ["Voice fallback (Gemini)", c.gemini_enabled ? `listen: ${g.models?.stt || "—"}, speak: ${g.models?.tts || "—"}` : "off"],
      ["Test phone", c.live_test_phone_set ? c.test_phone : "not set"],
      ["Real numbers", c.call_real_numbers ? "ON — calls businesses" : "off (test phone only)"],
      ["Bolna auto-setup", `${c.bolna_sync?.state || "?"}: ${c.bolna_sync?.detail || ""}`],
    ];
    try { rows.unshift(["Deployed version", (await api("/api/health")).commit]); } catch { /* offline */ }
    $("#cfgList").innerHTML = rows.map(([k, v]) => `<div><span class="fine">${esc(k)}</span><span>${esc(v)}</span></div>`).join("");
    $("#adminToken").value = store.get("adminToken", "");
    $("#settings").showModal();
  }
  async function adminCall(kind) {
    store.set("adminToken", $("#adminToken").value);
    const out = $("#adminOut"); out.hidden = false;
    const langs = $("#syncLangs").value.split(",").map((s) => s.trim()).filter(Boolean);
    try {
      if (kind === "check") { out.textContent = "Checking…"; out.textContent = JSON.stringify(await api("/api/admin/bolna_check"), null, 2); }
      if (kind === "preview") { out.textContent = "Building…"; out.textContent = JSON.stringify(await api(`/api/admin/agent_payload?lang=${encodeURIComponent(langs[0] || "hi")}`), null, 2); }
      if (kind === "sync") {
        out.textContent = "Calling Bolna…";
        const res = await api("/api/admin/sync_agent", { method: "POST", body: { languages: langs } });
        out.textContent = (res.env_hint ? `Done. To keep these ids across restarts, add:\n${res.env_hint}\n\n` : "") + JSON.stringify(res, null, 2);
        S.config = await api("/api/config"); applyToggles();
      }
      if (kind === "selftest") { out.textContent = "Replaying every call scenario in all 11 languages…"; out.textContent = await (await fetch("/api/selftest?format=text")).text(); }
    } catch (e) { out.textContent = "✗ " + e.message; }
  }

  // ================================================================ reset + boot
  function newRequest() {
    if (S.ws) { S.ws.onclose = null; S.ws.close(); }
    clearInterval(S.poll); stopCountdown(); Voice.stopSpeaking(); Narrator.queue = [];
    Object.assign(S, { session: null, phase: "intake", providers: [], sel: new Set(), dealShown: null, log: [], conv: { id: null, task: {}, ready: false, trace: [] },
      langLocked: S.userLang !== "auto" });   // a new request detects the language afresh
    $("#calls").hidden = true; $("#shortlist").hidden = true; $("#task").hidden = true; $("#callList").innerHTML = "";
    dock(false);
    history.replaceState(null, "", location.pathname);
    showUser("");
    sendChat("");
  }

  async function boot() {
    // A cached old page with this new script (or the reverse) can't work: fetch a fresh copy once
    const required = ["#orb", "#botLine", "#typeForm", "#langPill", "#callList", "#settings", "#langSheet", "#dealModal"];
    if (required.some((sel) => !$(sel))) {
      if (!/[?&]fresh=/.test(location.search)) { location.replace(`${location.pathname}?fresh=${Date.now()}${location.hash}`); return; }
      throw new Error("the page is out of date — please hard-refresh (pull down to reload, or Ctrl+Shift+R)");
    }
    if (/[?&]fresh=/.test(location.search)) history.replaceState(null, "", location.pathname + location.hash);
    S.config = await api("/api/config");
    for (const l of S.config.languages || []) S.langs[l.code] = l;
    S.commands = S.config.voice_commands || S.commands;
    S.lines.en = S.config.user_lines?.en;
    $("#userLang").innerHTML = `<option value="auto">Any language (detect it)</option>` +
      Object.values(S.langs).map((l) => `<option value="${esc(l.code)}">${esc(l.native)} · ${esc(l.name)}</option>`).join("");
    if (S.userLang !== "auto" && !S.langs[S.userLang]) S.userLang = "auto";
    $("#userLang").value = S.userLang;
    await setSpokenLang(S.userLang === "auto" ? browserLang() : S.userLang);
    applyToggles(); renderLangPill(); setOrb("idle");
    try { window.speechSynthesis?.getVoices(); window.speechSynthesis?.addEventListener?.("voiceschanged", () => {}); } catch { /* none */ }

    $("#orb").addEventListener("click", () => {
      const st = $("#orb").dataset.state;
      if (st === "listening") Voice.stopListening();
      else if (st === "speaking") { Voice.stopSpeaking(); Narrator.queue = []; listen(); }
      else if (st !== "thinking") listen();
    });
    $("#typeForm").addEventListener("submit", (e) => {
      e.preventDefault();
      const text = $("#typeInput").value.trim();
      if (!text) return;
      $("#typeInput").value = ""; S.interacted = true; S.typing = true;
      onUtterance(text).finally(() => { S.typing = false; });
    });
    $("#taskLine").addEventListener("click", () => { const ed = $("#taskEdit"); ed.hidden = !ed.hidden; $("#taskLine").setAttribute("aria-expanded", String(!ed.hidden)); });
    $("#saveTask").addEventListener("click", () => { readTask(); renderTask(); $("#taskEdit").hidden = true; toast("Updated"); });
    $("#langPill").addEventListener("click", openLangSheet);
    $("#menuBtn").addEventListener("click", openSettings);
    $("#callBtn").addEventListener("click", callSelected);
    $("#newBtn").addEventListener("click", newRequest);
    $("#vConfirm").addEventListener("click", () => { const rec = S.session?.recommendation; if (rec?.leg_id) lockWinner(rec.leg_id); });
    $("#vOther").addEventListener("click", () => { Narrator.push(narr("pick", {}), true); $$(".call details").forEach((d) => { d.open = true; }); });
    $$("#modeSeg input").forEach((i) => i.addEventListener("change", () => { S.mode = i.value; store.set("mode", S.mode); applyToggles(); updateCallBtn(); }));
    $("#voiceOn").addEventListener("change", (e) => { S.voiceOn = e.target.checked; store.set("voiceOn", S.voiceOn ? "1" : "0"); if (!S.voiceOn) Voice.stopSpeaking(); });
    $("#handsFree").addEventListener("change", (e) => { S.handsFree = e.target.checked; store.set("handsFree", S.handsFree ? "1" : "0"); });
    $("#glossOn").addEventListener("change", (e) => { S.glossOn = e.target.checked; store.set("glossOn", S.glossOn ? "1" : "0"); applyToggles(); });
    $("#userLang").addEventListener("change", (e) => {
      S.userLang = e.target.value; store.set("userLang", S.userLang); S.sttDown = false;
      if (S.userLang !== "auto") setSpokenLang(S.userLang);
    });
    $("#checkBtn").addEventListener("click", () => adminCall("check"));
    $("#previewBtn").addEventListener("click", () => adminCall("preview"));
    $("#syncBtn").addEventListener("click", () => adminCall("sync"));
    $("#selftestBtn").addEventListener("click", () => adminCall("selftest"));
    document.addEventListener("keydown", (e) => {
      if (e.code === "Space" && document.activeElement === document.body) { e.preventDefault(); $("#orb").click(); }
    });

    const m = location.hash.match(/s=([a-f0-9]+)/);
    if (m) {
      try {
        const session = await api(`/api/session/${m[1]}`);
        S.session = session; startCalls(session); return;
      } catch { /* expired: start fresh */ }
    }
    await sendChat("");
  }
  boot().catch((e) => {
    const el = $("#botLine");
    if (el) el.textContent = "Couldn't load: " + e.message;
    else document.body.insertAdjacentHTML("afterbegin", `<p style="padding:16px">Couldn't load: ${esc(e.message)}. Please refresh.</p>`);
  });
})();
