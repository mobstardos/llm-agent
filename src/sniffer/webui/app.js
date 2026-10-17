/* Universal Sniffer — логика панели мониторинга */
"use strict";

const $ = (id) => document.getElementById(id);
const MAX_ROWS = 800;            // строк в живой таблице
const PAUSE_BUFFER_MAX = 3000;   // буфер на паузе

const state = {
  paused: false,
  pauseBuf: [],
  sound: true,
  packetsShown: 0,
  packetsTotal: 0,
  alertsCount: 0,
  seenProtos: new Set(),
  seenTypes: new Set(),
  seenClients: new Set(),
  sessions: new Map(),
  series: [],
  filters: { text: "", proto: "", type: "", client: "", dir: "", valid: "", min: null, max: null, hideconn: true },
};

/* ============================ утилиты ============================ */

function fmtSize(n) {
  if (n == null) return "—";
  if (n >= 1048576) return (n / 1048576).toFixed(1) + " МБ";
  if (n >= 1024) return (n / 1024).toFixed(1) + " КБ";
  return n + " Б";
}
function fmtNum(n) { return (n || 0).toLocaleString("ru-RU"); }
function fmtUptime(s) {
  s = Math.floor(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + "ч " + m + "м";
  if (m > 0) return m + "м " + (s % 60) + "с";
  return s + "с";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function timeShort(iso) {
  if (!iso) return "";
  const m = /\d{2}:\d{2}:\d{2}/.exec(iso);
  return m ? m[0] : iso;
}

/* ============================ SSE ============================ */

let es = null, reconnectDelay = 1000;

function connectSSE() {
  es = new EventSource("/api/stream");
  es.onopen = () => { $("conn-dot").className = "dot dot-on"; reconnectDelay = 1000; setStatus("поток подключён"); };
  es.onerror = () => {
    $("conn-dot").className = "dot dot-off";
    setStatus("переподключение…");
    es.close();
    setTimeout(connectSSE, reconnectDelay);
    reconnectDelay = Math.min(reconnectDelay * 2, 15000);
  };
  es.addEventListener("packet", (e) => onPacket(JSON.parse(e.data)));
  es.addEventListener("stats", (e) => onStats(JSON.parse(e.data)));
  es.addEventListener("session", (e) => onSession(JSON.parse(e.data)));
  es.addEventListener("alert", (e) => onAlert(JSON.parse(e.data)));
  es.addEventListener("detected", (e) => {
    const d = JSON.parse(e.data);
    setStatus(`автодетект ${d.port_name}: ${d.protocol} (${d.confidence}%)`);
  });
  es.addEventListener("status", (e) => {
    const d = JSON.parse(e.data);
    if (d.stats) onStats(d.stats);
  });
}

/* ============================ поток пакетов ============================ */

function matchesFilters(p) {
  const f = state.filters;
  if (f.hideconn && (p.protocol === "CONNECT" || p.protocol === "DISCONNECT")) return false;
  if (f.proto && p.protocol !== f.proto) return false;
  if (f.dir && p.direction !== f.dir) return false;
  if (f.valid !== "" && String(p.valid ? 1 : 0) !== f.valid) return false;
  if (f.min != null && (p.size || 0) < f.min) return false;
  if (f.max != null && (p.size || 0) > f.max) return false;
  if (f.client && p.client !== f.client) return false;
  if (f.type) {
    const t = p.info && (p.info.cmd_type || p.info.method_type) || "";
    if (t !== f.type) return false;
  }
  if (f.text) {
    const hay = [p.summary, p.client, p.port_name, p.protocol,
      p.info && p.info.method, p.info && p.info.cmd_type,
      p.info && p.info.method_type, p.info && p.info.tables && p.info.tables.join(","),
      p.info && p.info.http_path]
      .filter(Boolean).join(" ").toLowerCase();
    if (!hay.includes(f.text.toLowerCase())) return false;
  }
  return true;
}

function onPacket(p) {
  state.packetsTotal++;
  $("badge-packets").textContent = fmtNum(state.packetsTotal);
  rememberPacketFact(p);
  if (state.paused) {
    state.pauseBuf.push(p);
    if (state.pauseBuf.length > PAUSE_BUFFER_MAX) state.pauseBuf.shift();
    return;
  }
  if (matchesFilters(p)) addPacketRow(p, true);
}

function rememberPacketFact(p) {
  const proto = (p.protocol || "").toUpperCase();
  if (proto && proto !== "CONNECT" && proto !== "DISCONNECT" && !state.seenProtos.has(proto)) {
    state.seenProtos.add(proto);
    addOption($("f-proto"), proto);
  }
  const t = p.info && (p.info.cmd_type || p.info.method_type);
  if (t && !state.seenTypes.has(t)) {
    state.seenTypes.add(t);
    addOption($("f-type"), t);
  }
  if (p.client && !state.seenClients.has(p.client)) {
    state.seenClients.add(p.client);
    addOption($("f-client"), p.client);
  }
}

function addOption(sel, value) {
  const opt = document.createElement("option");
  opt.value = value; opt.textContent = value;
  sel.appendChild(opt);
}

function protoChip(proto) {
  const cls = { THRIFT: "p-thrift", REMOTE_SERVER: "p-remote_server", HTTP: "p-http", JSON: "p-json", MODBUS: "p-modbus" }[proto] || "p-raw";
  return `<span class="proto-chip ${cls}">${esc(proto)}</span>`;
}

function summaryOf(p) {
  if (p.summary) return p.summary;
  const i = p.info || {};
  return i.method || i.cmd_type || i.http_path || "";
}

function addPacketRow(p, atTop) {
  const tbody = $("packets-body");
  const tr = document.createElement("tr");
  tr.dataset.id = p.id;
  if (!p.valid) tr.classList.add("invalid");
  const dir = p.direction === "TX"
    ? '<span class="dir-tx">&rarr;</span>'
    : '<span class="dir-rx">&larr;</span>';
  const type = (p.info && (p.info.cmd_type || p.info.method_type)) || "";
  tr.innerHTML = `
    <td class="mono">${p.id}</td>
    <td class="mono">${esc(timeShort(p.ts_iso))}</td>
    <td>${dir}</td>
    <td class="mono">${esc(p.client || "")}</td>
    <td>${esc(p.port_name || "")}</td>
    <td>${protoChip(p.protocol)}</td>
    <td class="mono">${esc(type || summaryOf(p)).slice(0, 60)}</td>
    <td class="mono">${fmtSize(p.size)}</td>
    <td class="summary-cell">${esc(summaryOf(p))}</td>`;
  tr.addEventListener("click", () => openDrawer(p.id));
  if (atTop && tbody.firstChild) {
    tbody.insertBefore(tr, tbody.firstChild);
  } else {
    tbody.appendChild(tr);
  }
  while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.lastChild);
  state.packetsShown++;
  $("stream-empty").style.display = "none";
}

function refilterStream() {
  const tbody = $("packets-body");
  tbody.innerHTML = "";
  state.packetsShown = 0;
  fetch("/api/packets?limit=600").then(r => r.json()).then(d => {
    const rows = d.packets.filter(matchesFilters).reverse();
    rows.forEach(p => addPacketRow(p, false));
  }).catch(() => {});
}

/* ============================ статистика ============================ */

function onStats(s) {
  $("kpi-pps").textContent = fmtNum(s.pps);
  $("kpi-packets").textContent = fmtNum(s.totals.packets);
  $("kpi-bytes").textContent = fmtSize(s.totals.bytes);
  $("kpi-sessions").textContent = fmtNum(s.sessions_active);
  $("kpi-uptime").textContent = fmtUptime(s.uptime_sec);
  $("badge-sessions").textContent = fmtNum(s.sessions_active);
  renderProtoBars(s.per_proto);
  renderBars($("top-types"), s.top_types.map(t => [t.type, t.count]));
  renderBars($("top-clients"), s.top_clients.map(c => [c.client, c.packets]));
  renderBars($("ports-stats"), Object.entries(s.per_port).map(([k, v]) => [k, v.packets]));
}

function renderBars(el, pairs) {
  if (!pairs.length) { el.innerHTML = '<div class="empty" style="padding:14px">нет данных</div>'; return; }
  const max = Math.max(...pairs.map(p => p[1]), 1);
  el.innerHTML = pairs.map(([label, val]) => `
    <div class="bar-row" title="${esc(label)}: ${fmtNum(val)}">
      <div class="bar-label">${esc(label)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, val / max * 100)}%"></div></div>
      <div class="bar-val">${fmtNum(val)}</div>
    </div>`).join("");
}

function renderProtoBars(per) {
  const pairs = Object.entries(per || {}).map(([k, v]) => [k, v.packets]);
  renderBars($("proto-bars"), pairs);
}

/* график pps */
function drawChart() {
  const cv = $("chart-pps");
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth, h = 180;
  if (cv.width !== w * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const s = state.series;
  if (s.length < 2) { ctx.fillStyle = "#8b97a8"; ctx.font = "12px sans-serif"; ctx.fillText("накопление данных…", 12, 24); return; }
  const max = Math.max(...s.map(p => p[1]), 5);
  ctx.strokeStyle = "#2a3341";
  ctx.beginPath();
  for (let i = 1; i <= 3; i++) { const y = h - (h - 16) * i / 4 - 8; ctx.moveTo(0, y); ctx.lineTo(w, y); }
  ctx.stroke();
  ctx.strokeStyle = "#4f8cff"; ctx.lineWidth = 1.6;
  ctx.beginPath();
  s.forEach((p, i) => {
    const x = i / (s.length - 1) * (w - 4) + 2;
    const y = h - 8 - (p[1] / max) * (h - 24);
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  });
  ctx.stroke();
  ctx.fillStyle = "rgba(79,140,255,.14)";
  ctx.lineTo(w - 2, h); ctx.lineTo(2, h); ctx.closePath(); ctx.fill();
  ctx.fillStyle = "#8b97a8"; ctx.font = "11px monospace";
  ctx.fillText("макс " + max + " пак/с", 8, 14);
}

/* серии приходят в stats? нет — тянем серию отдельно раз в секунду */
setInterval(() => {
  if (!$("tab-stats").classList.contains("active")) return;
  fetch("/api/series").then(r => r.json()).then(d => {
    state.series = d.series.map(p => [p[0], p[1], p[2]]);
    drawChart();
  }).catch(() => {});
}, 1000);

/* ============================ сессии ============================ */

function onSession(ev) {
  if (ev.action === "open") {
    state.sessions.set(ev.session.conn_id, ev.session);
  } else if (ev.action === "close") {
    state.sessions.delete(ev.session.conn_id);
  }
  renderSessions();
}

function renderSessions() {
  const rows = [...state.sessions.values()].sort((a, b) => b.conn_id - a.conn_id);
  $("badge-sessions").textContent = fmtNum(rows.length);
  const tb = $("sessions-body");
  tb.innerHTML = rows.map(s => `
    <tr>
      <td class="mono">${s.conn_id}</td>
      <td class="mono">${esc(s.client)}:${s.client_port}</td>
      <td>${esc(s.port_name)}</td>
      <td class="mono">${esc(s.target_host)}:${s.target_port}</td>
      <td class="mono">${esc(timeShort(s.started_iso))}</td>
      <td class="mono">${fmtNum(s.packets_tx)}</td>
      <td class="mono">${fmtNum(s.packets_rx)}</td>
      <td class="mono">${fmtSize(s.bytes_tx)} / ${fmtSize(s.bytes_rx)}</td>
      <td>${s.state === "active" ? '<span style="color:var(--green)">активна</span>' : esc(s.state)}</td>
      <td class="mono">${esc(s.protocol_tx || s.protocol_rx || "—")}</td>
    </tr>`).join("");
  $("sessions-empty").style.display = rows.length ? "none" : "";
}

/* ============================ тревоги ============================ */

function onAlert(a) {
  state.alertsCount++;
  $("badge-alerts").textContent = fmtNum(state.alertsCount);
  const tb = $("alerts-body");
  const tr = document.createElement("tr");
  tr.className = "al-" + (a.severity || "info");
  tr.innerHTML = `
    <td class="mono">${esc(timeShort(a.ts_iso))}</td>
    <td class="sev-${esc(a.severity)}">${esc((a.severity || "info").toUpperCase())}</td>
    <td>${esc(a.rule)}</td>
    <td class="summary-cell">${esc(a.detail)}</td>
    <td class="mono">${esc(a.client || "")}</td>
    <td>${esc(a.port_name || "")}</td>`;
  tb.insertBefore(tr, tb.firstChild);
  $("alerts-empty").style.display = "none";
  if (a.sound && state.sound) beep(a.severity);
  setStatus("ТРЕВОГА: " + a.rule);
}

let audioCtx = null;
function beep(severity) {
  try {
    audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.connect(g); g.connect(audioCtx.destination);
    o.frequency.value = severity === "crit" ? 880 : 620;
    g.gain.setValueAtTime(0.12, audioCtx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.001, audioCtx.currentTime + 0.45);
    o.start(); o.stop(audioCtx.currentTime + 0.5);
  } catch (e) { /* звук не критичен */ }
}

/* ============================ детали пакета ============================ */

function openDrawer(id) {
  fetch("/api/packet/" + id).then(r => r.json()).then(d => {
    if (!d.packet) { setStatus("пакет не найден в буфере"); return; }
    const p = d.packet;
    $("drawer-title").textContent = `Пакет #${p.id} — ${p.protocol}`;
    const rows = [];
    const kv = (k, v) => { if (v !== undefined && v !== null && v !== "") rows.push([k, v]); };
    kv("время", p.ts_iso); kv("направление", p.direction === "TX" ? "TX — клиент → сервер" : "RX — сервер → клиент");
    kv("клиент", p.client && (p.client + ":" + p.client_port)); kv("канал", p.port_name);
    kv("протокол", p.protocol); kv("валидный", p.valid ? "да" : "нет"); kv("размер", fmtSize(p.size));
    for (const [k, v] of Object.entries(p.info || {})) kv(k, Array.isArray(v) ? v.join(", ") : String(v));
    if (p.notes) kv("примечание", p.notes);
    let html = '<h4>Основное</h4><div class="kv">' +
      rows.map(([k, v]) => `<div class="k">${esc(k)}</div><div class="v">${esc(v)}</div>`).join("") + "</div>";
    if (p.info && p.info.fields) {
      html += "<h4>Поля Thrift</h4><div class='kv'>" +
        p.info.fields.map(f => `<div class="k">${esc(f.name || ("field " + f.id))} (${esc(f.type)})</div><div class="v">${esc(typeof f.value === "object" ? JSON.stringify(f.value) : f.value)}</div>`).join("") + "</div>";
    }
    html += `<h4>Hex-дамп</h4><pre class="codeview">${esc(p.hexdump || "(нет данных)")}</pre>`;
    $("drawer-body").innerHTML = html;
    $("drawer").classList.add("open");
    $("drawer-overlay").classList.add("show");
  }).catch(() => setStatus("ошибка загрузки деталей"));
}

function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer-overlay").classList.remove("show");
}

/* ============================ настройки ============================ */

function loadConfig() {
  fetch("/api/config").then(r => r.json()).then(d => {
    $("config-view").textContent = JSON.stringify(d.config, null, 2);
    $("parsers-list").innerHTML = (d.parsers || []).map(p => `<span class="chip">${esc(p)}</span>`).join("");
  }).catch(() => { $("config-view").textContent = "ошибка загрузки"; });
}

function showResult(msg, isErr) {
  const el = $("action-result");
  el.style.color = isErr ? "var(--red)" : "var(--green)";
  el.textContent = msg;
  setTimeout(() => { el.textContent = ""; }, 6000);
}

/* ============================ статус-бар ============================ */

function setStatus(msg) {
  $("status-msg").textContent = msg;
  $("status-evt").textContent = "событий: " + fmtNum(state.packetsTotal) + " | тревог: " + state.alertsCount;
}
setInterval(() => setStatus("готов"), 8000);

/* ============================ инициализация ============================ */

function bindUI() {
  // вкладки
  document.querySelectorAll(".tab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      document.querySelectorAll(".tabpane").forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      $("tab-" + btn.dataset.tab).classList.add("active");
      if (btn.dataset.tab === "config") loadConfig();
      if (btn.dataset.tab === "sessions") renderSessions();
    });
  });

  // пауза
  $("btn-pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("btn-pause").innerHTML = state.paused ? "&#9654; продолжить" : "&#10074;&#10074; пауза";
    if (!state.paused) {
      const buf = state.pauseBuf.splice(0);
      buf.filter(matchesFilters).reverse().forEach(p => addPacketRow(p, false));
      setStatus("поток возобновлён (" + buf.length + " из буфера)");
    } else {
      setStatus("пауза — события копятся в буфере");
    }
  });

  // звук
  $("btn-sound").addEventListener("click", () => {
    state.sound = !state.sound;
    $("sound-label").textContent = state.sound ? "звук" : "тихо";
    $("btn-sound").style.opacity = state.sound ? 1 : .55;
  });

  // фильтры
  const apply = () => {
    const f = state.filters;
    f.text = $("f-text").value.trim();
    f.proto = $("f-proto").value;
    f.type = $("f-type").value;
    f.client = $("f-client").value;
    f.dir = $("f-dir").value;
    f.valid = $("f-valid").value;
    f.min = $("f-min").value === "" ? null : +$("f-min").value;
    f.max = $("f-max").value === "" ? null : +$("f-max").value;
    f.hideconn = $("f-hideconn").checked;
    refilterStream();
  };
  ["f-text", "f-proto", "f-type", "f-client", "f-dir", "f-valid", "f-min", "f-max", "f-hideconn"]
    .forEach(id => {
      const el = $(id);
      el.addEventListener(id === "f-text" ? "input" : "change", apply);
    });
  $("f-clear").addEventListener("click", () => {
    ["f-text", "f-proto", "f-type", "f-client", "f-dir", "f-valid", "f-min", "f-max"].forEach(id => $(id).value = "");
    $("f-hideconn").checked = true;
    apply();
  });

  // drawer
  $("drawer-close").addEventListener("click", closeDrawer);
  $("drawer-overlay").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrawer(); });

  // действия
  $("btn-reload").addEventListener("click", () => {
    fetch("/api/reload", { method: "POST" })
      .then(r => r.json())
      .then(d => showResult(d.message || "перезагружено", !d.ok))
      .catch(() => showResult("ошибка перезагрузки", true));
  });
  $("btn-csv").addEventListener("click", () => {
    fetch("/api/export/csv").then(r => r.json()).then(d => {
      showResult(d.files && d.files.length ? "Готово: " + d.files.join("; ") : "CSV-экспорт отключён", !d.ok);
    });
  });
  $("btn-download-jsonl").addEventListener("click", () => window.open("/download/traffic.jsonl", "_blank"));
  $("btn-download-pcap").addEventListener("click", () => window.open("/download/traffic.pcap", "_blank"));
  $("btn-download-sqlite").addEventListener("click", () => window.open("/download/traffic.db", "_blank"));
}

async function init() {
  bindUI();
  connectSSE();
  try {
    const st = await (await fetch("/api/status")).json();
    $("ports-line").textContent = st.ports.map(p => `${p.name}: ${p.listen} → ${p.target}`).join("   |   ");
  } catch (e) {
    $("ports-line").textContent = "статус недоступен";
  }
  refilterStream();
  loadConfig();
}

init();
