/* zoomlt overlay: renders the WebSocket caption feed. No build step, no dependencies. */
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const captionsEl = $("captions");
  const partialEl = $("partial");
  const dot = $("dot");
  const metaEl = $("meta");
  const lagEl = $("lag");
  const meterEl = $("meter");
  const meterFill = $("meter-fill");
  const offlineEl = $("offline");

  const params = new URLSearchParams(location.search);
  const state = {
    paused: false,
    showSource: params.get("source") === "1" || localStorage.getItem("zoomlt.source") === "1",
    compact: params.get("compact") === "1",
    font: parseInt(params.get("font") || localStorage.getItem("zoomlt.font") || "28", 10),
    maxLines: parseInt(params.get("lines") || "60", 10),
    config: {},
    finals: [],          // {id, english, source_text, source_lang}
    pausedQueue: [],
    partialId: null,
  };

  // ---- helpers -----------------------------------------------------------------------------

  function applyFont() {
    document.documentElement.style.setProperty("--font", state.font + "px");
    try { localStorage.setItem("zoomlt.font", String(state.font)); } catch (e) {}
  }
  function setSource(on) {
    state.showSource = on;
    document.body.classList.toggle("show-source", on);
    $("btn-source").classList.toggle("on", on);
    try { localStorage.setItem("zoomlt.source", on ? "1" : "0"); } catch (e) {}
  }
  function setCompact(on) {
    state.compact = on;
    document.body.classList.toggle("compact", on);
    $("btn-compact").classList.toggle("on", on);
  }
  function setPaused(on) {
    state.paused = on;
    document.body.classList.toggle("paused", on);
    $("btn-pause").classList.toggle("on", on);
    $("btn-pause").textContent = on ? "resume" : "pause";
    if (!on) {
      const q = state.pausedQueue.splice(0);
      q.forEach(handleMessage);
    }
  }
  function displayText(c) {
    // translate mode: english; transcribe mode: source only; dual: english with source underneath
    return c.english || c.source_text || "";
  }
  function scrollToBottom() {
    captionsEl.scrollTop = captionsEl.scrollHeight;
  }
  function trim() {
    while (captionsEl.children.length > state.maxLines) captionsEl.removeChild(captionsEl.firstChild);
  }
  function lagClass(s) {
    if (s == null) return "";
    if (s > 6) return "bad";
    if (s > 3) return "slow";
    return "";
  }

  // ---- rendering ---------------------------------------------------------------------------

  function renderFinal(c) {
    let el = captionsEl.querySelector('[data-id="' + c.id + '"]');
    if (!el) {
      el = document.createElement("div");
      el.className = "cap new";
      el.dataset.id = c.id;
      el.innerHTML = '<span class="lang"></span><span class="text"><span class="main"></span><span class="src"></span></span>';
      captionsEl.appendChild(el);
    }
    el.querySelector(".lang").textContent = c.source_lang || "??";
    el.querySelector(".main").textContent = displayText(c);
    const src = el.querySelector(".src");
    src.textContent = (c.english && c.source_text) ? c.source_text : "";
    document.body.classList.add("has-captions");
    trim();
    scrollToBottom();
  }

  function renderPartial(c) {
    state.partialId = c.id;
    const t = displayText(c);
    partialEl.textContent = t ? "… " + t : "";
    document.body.classList.add("has-captions");
    scrollToBottom();
  }

  function clearPartial(id) {
    if (id == null || state.partialId === id) {
      partialEl.textContent = "";
      state.partialId = null;
    }
  }

  function renderStatus(s) {
    const lag = s.last_lag_s;
    lagEl.textContent = (lag != null && s.finals > 0) ? "lag " + lag.toFixed(1) + "s" : "";
    lagEl.className = lagClass(lag);
    const rtf = s.realtime_factor;
    const bits = [];
    if (state.config.model) bits.push(state.config.model);
    if (state.config.mode) bits.push(state.config.mode);
    if (rtf) bits.push("rtf " + rtf.toFixed(2) + (rtf > 1 ? " (falling behind: use a smaller model)" : ""));
    if (s.backlog > 1) bits.push("backlog " + s.backlog);
    if (s.shed) bits.push(s.shed + " skipped");
    if (s.audio_overflows) bits.push(s.audio_overflows + " audio drops");
    metaEl.textContent = bits.join(" · ");
    // Input meter: it is the difference between "nobody is talking" and "the wrong device".
    if (typeof s.input_level === "number") {
      const pct = Math.min(100, Math.round(Math.sqrt(s.input_level) * 140));
      meterFill.style.width = pct + "%";
      meterEl.classList.toggle("silent", s.input_level < 0.002);
      meterEl.title = s.input_level < 0.002
        ? "no sound reaching the app: check the capture device (zoomlt devices)"
        : "input level";
    }
  }

  // ---- message handling ----------------------------------------------------------------------

  function handleMessage(msg) {
    if (state.paused && msg.type === "caption") { state.pausedQueue.push(msg); return; }
    if (msg.type === "hello") {
      state.config = msg.config || {};
      metaEl.textContent = [state.config.model, state.config.mode].filter(Boolean).join(" · ");
      // "original" only means something when both texts exist (dual/transcribe modes).
      $("btn-source").hidden = state.config.mode === "translate";
      captionsEl.innerHTML = "";
      partialEl.textContent = "";
      state.partialId = null;
      state.finals = [];
      (msg.history || []).forEach((c) => { state.finals.push(c); renderFinal(c); });
      captionsEl.querySelectorAll(".cap").forEach((el) => el.classList.remove("new"));
      if (msg.status) renderStatus(msg.status);
      return;
    }
    if (msg.type === "status") { renderStatus(msg); return; }
    if (msg.type !== "caption") return;
    if (msg.kind === "partial") { renderPartial(msg); return; }
    if (msg.kind === "retract") { clearPartial(msg.id); return; }
    if (msg.kind === "final") {
      clearPartial(msg.id);
      state.finals.push(msg);
      renderFinal(msg);
      lagEl.textContent = "lag " + (msg.lag_s || 0).toFixed(1) + "s";
      lagEl.className = lagClass(msg.lag_s);
    }
  }

  // ---- websocket with reconnect --------------------------------------------------------------

  let ws = null, retry = 500;
  function connect() {
    const proto = location.protocol === "https:" ? "wss://" : "ws://";
    const token = params.get("t");
    ws = new WebSocket(proto + location.host + "/ws" + (token ? "?t=" + encodeURIComponent(token) : ""));
    ws.onopen = () => { dot.className = "dot ok"; offlineEl.hidden = true; retry = 500; };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleMessage(msg);
    };
    ws.onclose = () => {
      dot.className = "dot";
      offlineEl.hidden = false;        // visible even in compact mode, where the toolbar is hidden
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 5000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  // ---- controls -----------------------------------------------------------------------------

  $("btn-source").onclick = () => setSource(!state.showSource);
  $("btn-smaller").onclick = () => { state.font = Math.max(12, state.font - 2); applyFont(); };
  $("btn-bigger").onclick = () => { state.font = Math.min(72, state.font + 2); applyFont(); };
  $("btn-compact").onclick = () => setCompact(!state.compact);
  $("btn-pause").onclick = () => setPaused(!state.paused);
  $("btn-clear").onclick = () => { captionsEl.innerHTML = ""; partialEl.textContent = ""; state.finals = []; };
  $("btn-copy").onclick = () => {
    const lines = state.finals.map((c) => {
      const main = displayText(c);
      return c.english && c.source_text ? main + "\n    " + c.source_text : main;
    });
    const text = lines.join("\n");
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).catch(() => {});
    } else {
      const ta = document.createElement("textarea");
      ta.value = text; document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) {}
      ta.remove();
    }
  };
  document.addEventListener("keydown", (e) => {
    if (e.target && (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA")) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;   // leave Ctrl+C, Cmd+-, browser zoom alone
    if (e.code === "Space") { e.preventDefault(); setPaused(!state.paused); }
    else if (e.key === "c") setCompact(!state.compact);
    else if (e.key === "o") setSource(!state.showSource);
    else if (e.key === "+" || e.key === "=") $("btn-bigger").click();
    else if (e.key === "-") $("btn-smaller").click();
  });

  applyFont();
  setSource(state.showSource);
  setCompact(state.compact);
  connect();
})();
