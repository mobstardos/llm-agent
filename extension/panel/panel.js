/* panel.js — чат с агентами через WebSocket /ws (единый протокол с
   web-UI). Панель открывается как side panel / sidebar / вкладка —
   там соединение стабильно, в отличие от service worker.

   Клиент → сервер: {query, model, session_id}, {type:"hello", session_id},
                    {type:"approval_response", id, approved, remember:"once"}
   Сервер → клиент: start, status, route, plan, step_start, token,
                    step_done, replan, plan_done, plan_approval,
                    session_restored, event, message, done, error,
                    approval_request
   Шаги плана: {id, agent, task, status}. */
"use strict";

const apiX = (typeof browser !== "undefined") ? browser : chrome;

const $ = (s) => document.querySelector(s);
const log = $("#log");
let ws = null;
let wsTimer = null;
let sessionId = "";
let currentAssistant = null;   // поток токенов
let currentPlan = null;        // {el, steps: Map}
let pendingApproval = null;    // {id, resolve}? — кнопки шлют напрямую

/* ── Утилиты ────────────────────────────────────────────── */
function esc(s) {
  const d = document.createElement("div");
  d.textContent = s == null ? "" : String(s);
  return d.innerHTML;
}
function scrollEnd() { log.scrollTop = log.scrollHeight; }
function addSys(text, cls) {
  const d = document.createElement("div");
  d.className = "sys " + (cls || "");
  d.textContent = text;
  log.appendChild(d); scrollEnd();
  return d;
}
function addUser(text) {
  const d = document.createElement("div");
  d.className = "msg user"; d.textContent = text;
  log.appendChild(d); scrollEnd();
}
function addAssistant(agent) {
  const d = document.createElement("div");
  d.className = "msg assistant";
  d.innerHTML = '<div class="who">' + esc(agent || "ассистент") + "</div>" +
                '<div class="body"></div>';
  log.appendChild(d); scrollEnd();
  return d.querySelector(".body");
}
function setDot(state) {
  const dot = $("#dot");
  dot.className = "dot " + (state === "on" ? "on"
    : state === "busy" ? "busy" : "off");
}

/* ── Рендер плана ───────────────────────────────────────── */
function planEl(titleText) {
  const d = document.createElement("div");
  d.className = "plan";
  d.innerHTML = '<div class="plan-title">' + esc(titleText) + "</div>";
  log.appendChild(d); scrollEnd();
  return d;
}
function planSteps(container, steps) {
  // убрать старые строки, нарисовать заново (порядок важен)
  container.querySelectorAll(".plan-step").forEach(n => n.remove());
  const map = new Map();
  (steps || []).forEach(st => {
    const row = document.createElement("div");
    row.className = "plan-step " + (st.status || "pending");
    row.innerHTML = '<span class="st">' +
      (st.status === "done" ? "✔" :
       st.status === "running" ? "▶" :
       st.status === "failed" ? "✖" : "·") +
      "</span><span>" + esc((st.agent || "") + ": " + (st.task || "")) +
      "</span>";
    container.appendChild(row);
    map.set(st.id, row);
  });
  scrollEnd();
  return map;
}
function markStep(stepId, status) {
  if (!currentPlan) return;
  const row = currentPlan.steps.get(stepId);
  if (!row) return;
  row.className = "plan-step " + (status || "pending");
  row.querySelector(".st").textContent =
    status === "done" ? "✔" : status === "failed" ? "✖" :
    status === "running" ? "▶" : "·";
  scrollEnd();
}

/* ── Апрувы (инструменты и план) ────────────────────────── */
function showApproval(kind, data) {
  pendingApproval = data;
  $("#approval").classList.remove("hidden");
  $("#approval-title").textContent = kind === "plan"
    ? "План требует подтверждения"
    : "Инструмент требует разрешения";
  $("#approval-body").textContent = JSON.stringify(
    kind === "plan" ? data.plan : { tool: data.tool,
                                    arguments: data.arguments,
                                    reason: data.reason }, null, 2);
}
function answerApproval(approved) {
  if (!pendingApproval) return;
  sendJson({ type: "approval_response", id: pendingApproval.id,
             approved, remember: "once" });
  pendingApproval = null;
  $("#approval").classList.add("hidden");
}
$("#ap-yes").onclick = () => answerApproval(true);
$("#ap-no").onclick = () => answerApproval(false);

/* ── Пробуждение (горячая клавиша wake-agent) ───────────── */
/* Подставить выделенное в ввод и сфокусировать его (без автосообщения). */
function wake(prefill) {
  const ta = $("#input");
  const txt = String(prefill || "").trim();
  if (txt) {
    ta.value = ta.value.trim()
      ? ta.value.replace(/\s+$/, "") + "\n" + txt
      : txt;
    ta.dispatchEvent(new Event("input"));   // пересчитать высоту
  }
  ta.focus();
  try { ta.selectionStart = ta.selectionEnd = ta.value.length; }
  catch (e) { /* не критично */ }
}
function forgetWake() {
  try { apiX.storage.local.remove("pendingWake").catch(() => {}); }
  catch (e) { /* sync-вариант старых API */ }
}
if (apiX.runtime && apiX.runtime.onMessage) {
  apiX.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "bridge_wake") {
      forgetWake();          // живой приход — хвостовую запись стираем
      wake(msg.prefill);
    }
    return undefined;
  });
}

/* ── WebSocket ──────────────────────────────────────────── */
function wsUrl() {
  return getBase().replace(/^http/, "ws") + "/ws";
}
function getBase() {
  return ($("#cfg-url") && $("#cfg-url").value.trim()
    .replace(/\/+$/, "")) || base;
}
let base = "http://127.0.0.1:8000";

function connect() {
  clearTimeout(wsTimer);
  try { ws && ws.close(); } catch (e) { /* уже закрыт */ }
  setDot("off");
  let url;
  try { url = wsUrl(); } catch (e) { return; }
  try {
    ws = new WebSocket(url);
  } catch (e) {
    addSys("Не удалось открыть WebSocket: " + e.message, "err");
    wsTimer = setTimeout(connect, 5000);
    return;
  }
  ws.onopen = () => {
    setDot("on");
    sendJson({ type: "hello", session_id: sessionId });
  };
  ws.onmessage = (e) => {
    try { handleMessage(JSON.parse(e.data)); } catch (err) { /* мусор */ }
  };
  ws.onclose = () => {
    setDot("off");
    wsTimer = setTimeout(connect, 3000);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) { /* ignore */ } };
}

function sendJson(obj) {
  if (!ws || ws.readyState !== 1) {
    addSys("нет соединения — сообщение не отправлено", "err");
    return false;
  }
  ws.send(JSON.stringify(obj));
  return true;
}

/* ── Протокол ───────────────────────────────────────────── */
function handleMessage(msg) {
  switch (msg.type) {
    case "session_restored": {
      if (msg.session_id) {
        sessionId = msg.session_id;
        apiX.storage.local.set({ panelSession: sessionId });
        $("#session-tag").textContent = "сессия " + sessionId.slice(0, 6);
      }
      if (msg.history) addSys("история: " + msg.history + " сообщ.");
      return;
    }
    case "start":
      currentAssistant = null; currentPlan = null;
      setDot("busy");
      return;
    case "status":
      addSys("· " + (msg.text || ""), "status");
      return;
    case "route":
      addSys("агенты: " + ((msg.agents || []).join(", ") || "—") +
        (msg.reason ? " · " + msg.reason : ""));
      return;
    case "plan":
      currentPlan = { el: planEl("План (" + ((msg.steps || []).length) +
        " шагов)"), steps: new Map() };
      currentPlan.steps = planSteps(currentPlan.el, msg.steps);
      return;
    case "step_start":
      if (msg.step !== undefined && currentPlan) {
        // fallthrough: по id из step_done точнее; тут — по порядку
      }
      addSys("▶ " + (msg.agent || "") + (msg.step ? " (шаг " +
        msg.step + ")" : ""), "status");
      return;
    case "step_done":
      if (msg.id) markStep(msg.id, msg.status || "done");
      return;
    case "replan":
      addSys("⟳ перепланировка" + (msg.reason ? ": " + msg.reason : ""));
      currentPlan = null;
      return;
    case "plan_done":
      currentPlan = null;
      addSys("план завершён");
      return;
    case "token":
      if (!currentAssistant) currentAssistant = addAssistant("");
      currentAssistant.textContent += (msg.token || "");
      scrollEnd();
      return;
    case "message": {
      const body = addAssistant(msg.agent || "");
      body.textContent = msg.content || "";
      scrollEnd();
      currentAssistant = null;
      return;
    }
    case "event":
      addSys("⚯ " + (msg.kind || "") +
        (msg.payload && msg.payload.title ?
          " · " + msg.payload.title : ""));
      return;
    case "approval_request":
      showApproval("tool", msg);
      return;
    case "plan_approval":
      showApproval("plan", msg);
      return;
    case "done":
      currentAssistant = null; currentPlan = null;
      setDot("on");
      addSys("— готово —");
      return;
    case "error":
      addSys("ошибка: " + (msg.text || "?"), "err");
      setDot("on");
      return;
    default:
      return;
  }
}

/* ── Отправка ───────────────────────────────────────────── */
function send() {
  const ta = $("#input");
  const q = ta.value.trim();
  if (!q) return;
  if (!sendJson({ query: q, model: null, session_id: sessionId })) return;
  addUser(q);
  ta.value = "";
  ta.style.height = "auto";
}
$("#send").onclick = send;
$("#input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});
$("#input").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 120) + "px";
});

/* ── Настройки / сессия ─────────────────────────────────── */
$("#btn-settings").onclick = () => {
  $("#cfg").classList.toggle("hidden");
  $("#cfg-url").value = base;
};
$("#cfg-cancel").onclick = () => $("#cfg").classList.add("hidden");
$("#cfg-save").onclick = async () => {
  base = ($("#cfg-url").value.trim() || "http://127.0.0.1:8000")
    .replace(/\/+$/, "");
  await apiX.storage.local.set({ panelServer: base });
  $("#cfg").classList.add("hidden");
  addSys("адрес сохранён: " + base);
  connect();
};
$("#btn-new").onclick = async () => {
  sessionId = "ext-" + Date.now().toString(36) +
    Math.random().toString(36).slice(2, 8);
  await apiX.storage.local.set({ panelSession: sessionId });
  $("#session-tag").textContent = "сессия " + sessionId.slice(0, 6);
  log.innerHTML = "";
  addSys("новая сессия " + sessionId);
  connect();
};

/* ── Старт ──────────────────────────────────────────────── */
(async function boot() {
  const st = await apiX.storage.local.get(
    ["panelSession", "panelServer", "pendingAsk", "pendingWake"]);
  sessionId = st.panelSession || ("ext-" +
    Date.now().toString(36) + Math.random().toString(36).slice(2, 8));
  await apiX.storage.local.set({ panelSession: sessionId });
  base = (st.panelServer || "http://127.0.0.1:8000").replace(/\/+$/, "");
  $("#session-tag").textContent = "сессия " + sessionId.slice(0, 6);
  addSys("панель открыта · сервер: " + base);
  connect();

  // Отложенный вопрос из omnibox (`ag …`), меню «спросить/суммаризировать»
  const ask = st.pendingAsk;
  if (ask && ask.query && Date.now() - (ask.ts || 0) < 120000) {
    await apiX.storage.local.remove("pendingAsk");
    $("#input").value = ask.query;
    send();
  }

  // Горячая клавиша: панель открылась ПОСЛЕ нажатия → pendingWake
  const wk = st.pendingWake;
  if (wk && Date.now() - (wk.ts || 0) < 120000) {
    forgetWake();
    wake(wk.prefill);
  }
})();
