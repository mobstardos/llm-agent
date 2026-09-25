// ═══════════════════════════════════════════════════════════
// Helpers
// ═══════════════════════════════════════════════════════════
const $ = id => document.getElementById(id);

function escapeHtml(s) {
  return String(s || "").replace(/[&<>"]/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;",
  }[c]));
}

function fmtAge(ts) {
  if (!ts) return "—";
  const s = Math.floor(Date.now() / 1000 - ts);
  if (s < 60) return `${s} с назад`;
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return `${Math.floor(s / 86400)} дн назад`;
}

function fmtDuration(s) {
  if (!s || s < 0) return "—";
  s = Math.floor(s);
  if (s < 60) return `${s} с`;
  if (s < 3600) return `${Math.floor(s / 60)} мин`;
  if (s < 86400) return `${(s / 3600).toFixed(1)} ч`;
  return `${(s / 86400).toFixed(1)} дн`;
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    ...opts,
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

// ═══════════════════════════════════════════════════════════
// Elements
// ═══════════════════════════════════════════════════════════
const chat = $("chat");
const form = $("form");
const input = $("input");
const statusEl = $("status");
// Task 24-d: комбо-бокс моделей вместо <select>
const modelBox = $("model-box");
const modelBtn = $("model-btn");
const modelCurrent = $("model-current");
const modelPanel = $("model-panel");
const modelSearch = $("model-search");
const modelList = $("model-list");
const modelRefresh = $("model-refresh");
const modelCountEl = $("model-count");
const cacheStatsEl = $("cache-stats");
const submitBtn = form.querySelector("button");

const sysDot = $("sys-dot");
const sysText = $("sys-text");

const projectBtn = $("project-btn");
const projectLabel = $("project-label");

const settingsBtn = $("settings-btn");
const settingsModal = $("settings-modal");
const settingsClose = $("settings-close");

// State
let ws;
let streamingEl = null;
let currentApprovalId = null;
let pendingImportFile = null;
let pendingImportMode = "merge";
let snapshotData = null;

// Task 24-c/24-d: карта модель → провайдер + текущий выбор (qid
// вида «provider/model» — коллизии имён между провайдерами решены)
let modelProvider = {};
let lastSentModel = null;
let currentModel = "";         // квалифицированный id выбранной модели
let modelData = { providers: [] };
let expandedGroups = {};        // pid → true («показать ещё» раскрыт)
let modelsOpen = false;         // панель комбо-бокса открыта

// Сессия диалога (Этап 3): переживает переподключение WS
let sessionId = localStorage.getItem("llm_session_id") || "";

// Task 27: индекс следующего сообщения в истории сессии (для правки/отката)
// и активное редактирование {index} либо null
let chatIndex = 0;
let editing = null;

// Дорожка шагов Supervisor (Этап 2)
let planBox = null;        // текущий блок плана
let stepEls = {};          // step_id -> {root, stream, status, meta}

function resetPlanState() {
  planBox = null;
  stepEls = {};
}

function planStepEl(s) {
  const el = document.createElement("div");
  el.className = "plan-step";
  el.innerHTML =
    `<div class="plan-step-icon">${escapeHtml(s.icon || "🤖")}</div>` +
    `<div class="plan-step-body">` +
      `<div class="plan-step-head"><b>${escapeHtml(s.title || s.agent)}</b>` +
      `<span class="plan-step-id">#${escapeHtml(String(s.id))}</span>` +
      `<span class="plan-step-agent">${escapeHtml(s.agent)}</span>` +
      `<span class="plan-step-status">ожидание</span></div>` +
      `<div class="plan-step-task">${escapeHtml(s.task || "")}</div>` +
      `<div class="plan-step-stream"></div>` +
      `<div class="plan-step-meta"></div>` +
    `</div>`;
  stepEls[String(s.id)] = {
    root: el,
    stream: el.querySelector(".plan-step-stream"),
    status: el.querySelector(".plan-step-status"),
    meta: el.querySelector(".plan-step-meta"),
  };
  return el;
}

function renderPlan(steps, intent) {
  resetPlanState();
  const div = document.createElement("div");
  div.className = "plan";
  div.innerHTML =
    `<div class="plan-title"><span>ПЛАН · ${steps.length} шаг(ов)</span>` +
    `<span>${escapeHtml(intent || "Plan → Execute → Observe")}</span></div>` +
    `<div class="plan-steps"></div>`;
  const box = div.querySelector(".plan-steps");
  steps.forEach(s => box.appendChild(planStepEl(s)));
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  planBox = div;
  return div;
}

function appendPlanSteps(steps) {
  if (!planBox) { renderPlan(steps, "re-plan"); return; }
  const box = planBox.querySelector(".plan-steps");
  steps.forEach(s => box.appendChild(planStepEl(s)));
}

// Этап 3: восстановление дорожки шагов после переподключения WS
// (план приходит из реестра: GET /api/plans?session_id=…)
function renderRestoredPlan(plan) {
  if (!plan || !plan.steps || !plan.steps.length) return;
  const views = plan.steps.map(s => ({
    id: s.id, agent: s.agent, task: s.task,
    title: s.agent, icon: "🤖",
  }));
  renderPlan(views, `восстановлен · ${plan.intent || ""}`.trim());
  plan.steps.forEach(s => {
    if (s.status === "done") {
      setStepState(String(s.id), "done", "готово ✓");
      const st = stepEls[String(s.id)];
      if (st) st.meta.textContent = s.summary || "";
    } else if (s.status === "error") {
      setStepState(String(s.id), "error", "ошибка");
      const st = stepEls[String(s.id)];
      if (st) st.meta.textContent = s.error || "шаг не выполнен";
    }
  });
  addMsg("system", "🔄 Сессия восстановлена — план снова виден " +
    `(история: ${plan.steps.length} шаг(ов))`);
}

function setStepState(id, state, text) {
  const st = stepEls[id];
  if (!st) return;
  st.root.classList.remove("running", "done", "error");
  if (state !== "pending") st.root.classList.add(state);
  if (text && st.status) st.status.textContent = text;
}

function appendStepToken(id, text) {
  const st = stepEls[id];
  if (!st) return false;
  st.stream.style.display = "block";
  st.stream.textContent += text;
  chat.scrollTop = chat.scrollHeight;
  return true;
}

// Апрув плана целиком (inline-карточка, как в прототипе)
function showPlanApproval(msg) {
  finalizeStreaming();
  const steps = (msg.plan && msg.plan.steps) || [];
  const div = document.createElement("div");
  div.className = "plan-approval";
  const items = steps.map(s =>
    `<li><b>${escapeHtml(s.agent)}</b> — ${escapeHtml(s.task || "")}</li>`
  ).join("");
  div.innerHTML =
    `<div class="pa-title">⚠️ План требует подтверждения</div>` +
    `<ol class="pa-steps">${items}</ol>` +
    `<div class="pa-btns">` +
      `<button class="pa-ok">Выполнить план</button>` +
      `<button class="pa-no">Отклонить</button>` +
    `</div>`;
  const send = ok => {
    if (ws && ws.readyState === 1)
      ws.send(JSON.stringify({
        type: "approval_response", id: msg.id, approved: ok,
      }));
    div.classList.add("resolved");
  };
  div.querySelector(".pa-ok").onclick = () => send(true);
  div.querySelector(".pa-no").onclick = () => send(false);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
}

// ═══════════════════════════════════════════════════════════
// Chat rendering
// ═══════════════════════════════════════════════════════════
function addMsg(role, content, extra = "", index = null) {
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = extra + escapeHtml(content);
  // Task 27: у сообщений пользователя — кнопки «править»/«заново»:
  // правка перезаписывает сообщение и откатывает историю после него,
  // «заново» — тот же механизм без изменения текста
  if (role === "user" && index !== null && Number.isInteger(index)) {
    const acts = document.createElement("div");
    acts.className = "msg-actions";
    const edit = document.createElement("button");
    edit.className = "msg-edit-btn";
    edit.title = "Править и переспросить (история после — откатится)";
    edit.textContent = "✏️";
    edit.onclick = () => startEdit(index, content);
    const regen = document.createElement("button");
    regen.className = "msg-edit-btn";
    regen.title = "Переспросить с этого места без правки текста";
    regen.textContent = "🔁";
    regen.onclick = () => sendEdit(index, content);
    acts.appendChild(edit);
    acts.appendChild(regen);
    div.appendChild(acts);
  }
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  return div;
}

// Task 27: правка сообщения — текст в поле ввода, баннер режима
function startEdit(index, text) {
  editing = { index };
  input.value = text || "";
  input.focus();
  const banner = $("edit-banner");
  if (banner) {
    banner.classList.remove("hidden");
    banner.querySelector(".edit-note").textContent =
      `Правка сообщения #${index + 1} — ответы после него откатятся`;
  }
}

function cancelEdit() {
  editing = null;
  input.value = "";
  const banner = $("edit-banner");
  if (banner) banner.classList.add("hidden");
}

function sendEdit(index, text) {
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({
    type: "edit_message", index, text,
    session_id: sessionId || "__new__",
    model: currentModel || null,
  }));
  cancelEdit();
  setBusy(true);
}

// Task 27: перерисовать чат по messages_view (после правки/отката)
function rerenderFromView(messages) {
  chat.innerHTML = "";
  for (const m of messages || []) {
    addMsg(m.role === "user" ? "user" : "assistant", m.content || "",
      "", m.index);
  }
  chatIndex = (messages && messages.length)
    ? messages[messages.length - 1].index + 1 : 0;
}

function startStreaming(agentName) {
  if (streamingEl) return streamingEl;
  const div = document.createElement("div");
  div.className = "msg assistant streaming";
  if (agentName) {
    const tag = document.createElement("div");
    tag.className = "agent-tag";
    tag.textContent = `agent: ${agentName}`;
    div.appendChild(tag);
  }
  const text = document.createElement("span");
  text.className = "stream-text";
  div.appendChild(text);
  chat.appendChild(div);
  chat.scrollTop = chat.scrollHeight;
  streamingEl = div;
  return div;
}

// Task 24-d: сворачиваемый блок «Размышления» (reasoning-модели).
// Приходит ДО content-токенов; при первом контенте — сворачиваем.
function ensureReasoningBlock(container) {
  let det = container.querySelector(":scope > details.reasoning");
  if (!det) {
    det = document.createElement("details");
    det.className = "reasoning";
    det.open = true;
    det.innerHTML = `<summary>🧠 Размышления</summary>` +
      `<div class="reasoning-text"></div>`;
    const text = container.querySelector(".stream-text");
    container.insertBefore(det, text || null);
  }
  return det.querySelector(".reasoning-text");
}

function appendReasoning(text) {
  const el = streamingEl || startStreaming(null);
  ensureReasoningBlock(el).textContent += text;
  chat.scrollTop = chat.scrollHeight;
}

function appendStepReasoning(stepId, text) {
  const st = stepEls[stepId];
  if (!st) return false;
  st.stream.style.display = "block";
  ensureReasoningBlock(st.root).textContent += text;
  chat.scrollTop = chat.scrollHeight;
  return true;
}

function appendToken(text) {
  if (!streamingEl) return;
  // первый контент после reasoning — сворачиваем блок размышлений
  const det = streamingEl.querySelector(":scope > details.reasoning");
  if (det && det.open && !streamingEl._rcollapsed) {
    det.open = false;
    streamingEl._rcollapsed = true;
  }
  const span = streamingEl.querySelector(".stream-text") || streamingEl;
  span.textContent += text;
  chat.scrollTop = chat.scrollHeight;
}

function finalizeStreaming() {
  if (streamingEl) {
    streamingEl.classList.remove("streaming");
    streamingEl = null;
  }
}

function setBusy(busy) {
  submitBtn.disabled = busy;
  input.disabled = busy;
  if (busy) {
    statusEl.textContent = "Работаю...";
    statusEl.className = "status busy";
  }
}

// ═══════════════════════════════════════════════════════════
// System status indicator
// ═══════════════════════════════════════════════════════════
async function refreshSystemStatus() {
  try {
    const [health, enrichment] = await Promise.all([
      api("/api/db/health").catch(() => ({ enabled: false, healthy: false })),
      api("/api/enrichment/status").catch(() => ({ enabled: false })),
    ]);

    let status = "OK";
    let cls = "ok";

    if (health.enabled && !health.healthy) {
      status = "DB down";
      cls = "bad";
    } else if (enrichment.enabled && !enrichment.worker?.running) {
      status = "Enrich off";
      cls = "warn";
    } else if (!health.enabled) {
      status = "No PG";
      cls = "warn";
    }

    sysDot.className = `sys-dot ${cls}`;
    sysText.textContent = status;
  } catch {
    sysDot.className = "sys-dot bad";
    sysText.textContent = "err";
  }
}

// ═══════════════════════════════════════════════════════════
// WebSocket
// ═══════════════════════════════════════════════════════════
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = () => {
    statusEl.textContent = "Подключено";
    statusEl.className = "status online";
    // Этап 3: представляемся серверу — он вернёт сессию и последний план
    if (sessionId) {
      try { ws.send(JSON.stringify({ type: "hello", session_id: sessionId })); } catch (_) {}
    }
  };

  ws.onclose = () => {
    statusEl.textContent = "Переподключение...";
    statusEl.className = "status offline";
    setTimeout(connect, 1500);
  };

  ws.onerror = () => {
    statusEl.className = "status offline";
  };

  ws.onmessage = e => handleMessage(JSON.parse(e.data));
}

function handleMessage(msg) {
  switch (msg.type) {
    case "start":
      finalizeStreaming();
      resetPlanState();
      setBusy(true);
      return;

    case "status":
      statusEl.textContent = msg.text;
      statusEl.className = "status busy";
      return;

    case "route": {
      const a = (msg.agents || []).join(", ") || "—";
      addMsg("system", `🧭 ${a}${msg.reason ? " — " + msg.reason : ""}`);
      return;
    }

    case "plan":
      renderPlan(msg.steps || [], msg.intent);
      return;

    case "step_start":
      if (msg.step_id && stepEls[msg.step_id]) {
        // шаг плана: подсветка в дорожке, стрим пойдёт в блок шага
        setStepState(msg.step_id, "running", "выполняется…");
      } else {
        // legacy-путь (handle()): глобальный стрим агента
        finalizeStreaming();
        startStreaming(msg.agent);
      }
      return;

    case "token":
      if (msg.step_id && stepEls[msg.step_id]) {
        appendStepToken(msg.step_id, msg.token);
      } else {
        appendToken(msg.token);
      }
      return;

    case "reasoning":
      // Task 24-d: ход мыслей reasoning-моделей (R1/QwQ/reasoner)
      if (msg.step_id && stepEls[msg.step_id]) {
        appendStepReasoning(msg.step_id, msg.text);
      } else {
        appendReasoning(msg.text);
      }
      return;

    case "step_done": {
      const st = stepEls[msg.step_id];
      if (!st) return;
      setStepState(
        msg.step_id, msg.ok ? "done" : "error",
        msg.ok ? "готово ✓" : "ошибка");
      st.meta.textContent = msg.ok
        ? `🔧 ${msg.tool_calls || 0} tool-calls · ` +
          `${((msg.duration_ms || 0) / 1000).toFixed(1)}s`
        : `⚠️ ${msg.error || "шаг не выполнен"}`;
      if (!st.stream.textContent) st.stream.style.display = "none";
      return;
    }

    case "replan":
      addMsg("system",
        `🔁 Re-plan (${msg.attempt}): ${msg.reason || "меняю план"}`);
      appendPlanSteps(msg.steps || []);
      return;

    case "plan_done":
      addMsg("system", msg.success
        ? `✅ План выполнен (${msg.steps_done || 0}/${msg.steps_total || 0})` +
          (msg.replans ? ` · re-plan: ${msg.replans}` : "")
        : `⚠️ План завершён с ошибкой ` +
          `(${msg.steps_done || 0}/${msg.steps_total || 0})`);
      resetPlanState();
      return;

    case "plan_approval":
      showPlanApproval(msg);
      return;

    case "session_restored":
      // Этап 3: сервер подтвердил сессию (после hello/session_id)
      if (msg.session_id) {
        const switched = sessionId && sessionId !== msg.session_id;
        sessionId = msg.session_id;
        localStorage.setItem("llm_session_id", sessionId);
        // Task 24-c: при переключении чата — рендерим подхваченную историю
        if (Array.isArray(msg.messages) && msg.messages.length
            && (!chat.children.length || switched)) {
          // Task 27: у сообщений есть index — включаем кнопки правки
          rerenderFromView(msg.messages);
        } else {
          // чат уже отрисован (reconnect) — синхронизируем только счётчик
          chatIndex = (Array.isArray(msg.messages) && msg.messages.length)
            ? msg.messages[msg.messages.length - 1].index + 1
            : (msg.history || 0);
        }
        if (switched) loadSessions();
      }
      renderRestoredPlan(msg.plan);
      return;

    case "event":
      // Этап 4: события шины (фичи) — раздаём фичевым вкладкам
      // через DOM-событие (вкладка «Мост» слушает bridge.*), app.js
      // остаётся незнающим о конкретных фичах.
      try {
        window.dispatchEvent(new CustomEvent("llm-event", { detail: msg }));
      } catch (e) { /* старый браузер — не критично */ }
      return;

    case "session_edited": {
      // Task 27: сервер подтвердил правку и откат — перерисовываем чат;
      // далее идёт штатный конвейер ответа (start → token → message)
      rerenderFromView(msg.messages);
      addMsg("system",
        `✏️ Сообщение исправлено — история после него откачена (${msg.index + 1})`);
      return;
    }

    case "message": {
      finalizeStreaming();
      const tag = msg.agent
        ? `<div class="agent-tag">agent: ${msg.agent}</div>`
        : "";
      let steps = "";
      if (msg.steps && msg.steps.length) {
        const items = msg.steps.slice(0, 20).map(s =>
          `<pre>${escapeHtml(
            `[${s.tool}] ${JSON.stringify(s.args)}\n→ ${s.result_preview}`
          )}</pre>`
        ).join("");
        steps = `<div class="steps"><details><summary>Шагов: ${msg.tool_calls || 0}</summary>${items}</details></div>`;
      }
      // Task 24-d: блок размышлений в финальном сообщении (если есть)
      let reasoning = "";
      if (msg.reasoning) {
        reasoning = `<details class="reasoning">` +
          `<summary>🧠 Размышления</summary>` +
          `<div class="reasoning-text">${escapeHtml(msg.reasoning)}</div></details>`;
      }
      // Task 24-c: бейдж локальной модели (без облака)
      const isLocal = lastSentModel
        && modelProvider[lastSentModel] === "ollama_local";
      const localBadge = isLocal
        ? `<div class="local-badge">💻 локальная модель · без облака</div>`
        : "";
      // Task 24-d: бейдж авто-фолбэка (модель сменилась сервером)
      const fbBadge = msg.fallback_from
        ? `<div class="fallback-badge">♻️ выбранная модель недоступна — ответ через ${escapeHtml(msg.model_used || "fallback")}</div>`
        : "";
      const wrap = document.createElement("div");
      wrap.className = "msg assistant";
      wrap.innerHTML = fbBadge + localBadge + tag + reasoning +
        escapeHtml(msg.content) + steps;
      chat.appendChild(wrap);
      chat.scrollTop = chat.scrollHeight;
      chatIndex++;   // Task 27: assistant занял следующую позицию истории
      return;
    }

    case "done":
      finalizeStreaming();
      resetPlanState();
      setBusy(false);
      statusEl.textContent = "Готово";
      statusEl.className = "status online";
      refreshCacheStats();
      return;

    case "error":
      finalizeStreaming();
      addMsg("system", "❌ " + msg.text);
      setBusy(false);
      statusEl.textContent = "Ошибка";
      statusEl.className = "status offline";
      return;

    case "approval_request":
      showApproval(msg);
      return;
  }
}

// ═══════════════════════════════════════════════════════════
// Approval modal
// ═══════════════════════════════════════════════════════════
function showApproval(msg) {
  currentApprovalId = msg.id;
  $("approval-tool").textContent = msg.tool || "";
  $("approval-reason").textContent = msg.reason || "";
  try {
    $("approval-args").textContent = JSON.stringify(msg.arguments, null, 2);
  } catch {
    $("approval-args").textContent = String(msg.arguments);
  }
  const sel = $("approval-remember");
  sel.querySelectorAll(".path-only").forEach(o => {
    o.style.display = msg.has_path ? "" : "none";
  });
  sel.value = "once";
  $("approval-modal").classList.remove("hidden");
}

function sendApproval(approved) {
  if (!currentApprovalId || !ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify({
    type: "approval_response",
    id: currentApprovalId,
    approved: approved,
    remember: $("approval-remember").value || "once",
  }));
  $("approval-modal").classList.add("hidden");
  currentApprovalId = null;
}

$("approval-approve").onclick = () => sendApproval(true);
$("approval-reject").onclick = () => sendApproval(false);

// ═══════════════════════════════════════════════════════════
// Models (Task 24-d: комбо-бокс с поиском, все провайдеры)
// ═══════════════════════════════════════════════════════════
const VISIBLE_PER_GROUP = 25;   // «показать ещё» свыше этого числа

function qidOf(pid, modelId) { return `${pid}/${modelId}`; }

function shortModelName(qid) {
  // «openrouter/deepseek/deepseek-r1:free» → «deepseek/deepseek-r1:free»
  const i = qid.indexOf("/");
  return i > -1 ? qid.slice(i + 1) : qid;
}

function setModelCurrent(qid) {
  currentModel = qid || "";
  const prov = modelProvider[currentModel];
  modelCurrent.textContent = currentModel
    ? shortModelName(currentModel) + (prov === "ollama_local" || prov === "lmstudio" ? " 💻" : "")
    : "—";
  if (currentModel) modelBtn.title = `${currentModel} (сохраняется автоматически)`;
}

function renderModelList() {
  const filter = (modelSearch.value || "").trim().toLowerCase();
  modelList.innerHTML = "";
  modelProvider = {};
  let shown = 0;
  const provs = [...(modelData.providers || [])]
    .sort((a, b) => (b.available - a.available));   // доступные сверху

  for (const p of provs) {
    const all = p.models || [];
    const matches = filter
      ? all.filter(m => m.id.toLowerCase().includes(filter))
      : all;
    if (!matches.length && filter) continue;

    const group = document.createElement("div");
    group.className = "model-group" + (p.available ? "" : " off");

    const head = document.createElement("div");
    head.className = "model-group-head";
    head.innerHTML =
      `<span>${escapeHtml(p.name)}</span>` +
      `<span class="model-group-badge${p.available ? "" : " err"}">` +
      (p.available ? (p.local ? "💻 без интернета" : "готов") :
       escapeHtml(p.error || "недоступен")) +
      `</span>`;
    head.title = p.docs_url ? `Ключ: ${p.docs_url}` : "";
    group.appendChild(head);

    if (p.available) {
      const expanded = expandedGroups[p.id] || filter.length > 0;
      const visible = expanded ? matches : matches.slice(0, VISIBLE_PER_GROUP);
      for (const m of visible) {
        const qid = qidOf(p.id, m.id);
        modelProvider[qid] = p.id;
        const item = document.createElement("button");
        item.type = "button";
        item.className = "model-item" + (qid === currentModel ? " sel" : "");
        const badges = [];
        if (p.local) badges.push(`<span class="mbadge">💻</span>`);
        if (!m.supports_tools) badges.push(`<span class="mbadge soft">без tools</span>`);
        item.innerHTML =
          `<span class="model-item-name">${escapeHtml(m.id)}</span>` +
          `<span class="model-item-badges">${badges.join("")}</span>`;
        item.title = qid;
        item.onclick = () => {
          setModelCurrent(qid);
          api("/api/model/select", { method: "POST", body: { model: qid } })
            .catch(() => {});
          closeModelPanel();
          renderModelList();
        };
        group.appendChild(item);
      }
      if (!expanded && matches.length > visible.length) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "model-more";
        more.textContent = `Ещё ${matches.length - visible.length}…`;
        more.onclick = () => { expandedGroups[p.id] = true; renderModelList(); };
        group.appendChild(more);
      }
      shown += visible.length;
    }
    modelList.appendChild(group);
  }

  const total = provs.reduce((s, p) => s + (p.models_total || (p.models || []).length), 0);
  modelCountEl.textContent = filter
    ? `${shown} совпадений из ${total}`
    : `всего ${total} · у ${provs.filter(p => p.available).length} провайдеров`;
  if (!modelList.children.length) {
    modelList.innerHTML = `<div class="model-empty">Ничего не найдено</div>`;
  }
}

function openModelPanel() {
  modelPanel.classList.remove("hidden");
  modelsOpen = true;
  renderModelList();
  modelSearch.value = "";
  try { modelSearch.focus(); } catch (_) {}
}

function closeModelPanel() {
  modelPanel.classList.add("hidden");
  modelsOpen = false;
}

modelBtn.onclick = () => (modelsOpen ? closeModelPanel() : openModelPanel());
modelSearch.addEventListener("input", () => renderModelList());
modelSearch.addEventListener("keydown", e => {
  if (e.key === "Enter") {
    // Enter — выбрать первое видимое доступное
    const first = modelList.querySelector(".model-item:not(.sel)");
    if (first) first.click();
  } else if (e.key === "Escape") {
    closeModelPanel();
  }
});
document.addEventListener("click", e => {
  if (modelsOpen && modelBox && !modelBox.contains(e.target)) closeModelPanel();
});
modelRefresh.onclick = () => loadModels(true);

async function loadModels(force = false) {
  try {
    if (force) {
      modelBtn.disabled = true;
      modelCurrent.textContent = "…";
    }
    const d = await api("/api/models");
    modelData = d;
    expandedGroups = {};

    // карта qid → провайдер до поиска кандидатов (панель может быть
    // ещё ни разу не отрисована)
    modelProvider = {};
    for (const p of (d.providers || [])) {
      for (const m of (p.models || [])) modelProvider[qidOf(p.id, m.id)] = p.id;
    }

    // выбор: сохранённый (runtime.yaml) → дефолт сервера → первый доступный.
    // Имя может быть квалифицированным «provider/model» или старым коротким.
    const wantCandidates = [d.saved, d.default].filter(Boolean);
    let want = "";
    for (const cand of wantCandidates) {
      if (modelProvider[cand] !== undefined) { want = cand; break; }
      const suffix = "/" + cand;
      const hit = Object.keys(modelProvider).find(q => q.endsWith(suffix) || q === cand);
      if (hit) { want = hit; break; }
    }
    if (!want) {
      const avail = (d.providers || []).find(p => p.available && (p.models || []).length);
      if (avail) {
        const first = avail.models[0];
        if (first) want = qidOf(avail.id, first.id);
      }
    }
    if (want) {
      modelProvider[want] = modelProvider[want] ||
        want.slice(0, want.indexOf("/"));
      setModelCurrent(want);
    }
    if (modelsOpen) renderModelList();
  } catch {} finally {
    modelBtn.disabled = false;
  }
}

// ═══════════════════════════════════════════════════════════
// Sessions (Task 24-c: переключение чатов с авто-заголовками)
// ═══════════════════════════════════════════════════════════
const sessionSelect = $("session");

async function loadSessions() {
  try {
    const d = await api("/api/sessions");
    sessionSelect.innerHTML = "";
    const optNew = document.createElement("option");
    optNew.value = "__new__";
    optNew.textContent = "＋ Новый чат";
    sessionSelect.appendChild(optNew);
    let haveCur = false;
    for (const s of d.sessions || []) {
      if (s.id === sessionId) haveCur = true;
      const o = document.createElement("option");
      o.value = s.id;
      o.textContent = `${s.title || "Без названия"} · ${fmtAge(s.updated_at)}`;
      sessionSelect.appendChild(o);
    }
    if (sessionId && !haveCur) {
      const o = document.createElement("option");
      o.value = sessionId;
      o.textContent = "Текущий чат";
      sessionSelect.appendChild(o);
    }
    sessionSelect.value = sessionId || "__new__";
  } catch {}
}

sessionSelect.addEventListener("change", () => {
  const v = sessionSelect.value;
  if (v === (sessionId || "__new__")) return;
  sessionId = v === "__new__" ? "" : v;
  if (sessionId) localStorage.setItem("llm_session_id", sessionId);
  else localStorage.removeItem("llm_session_id");
  chat.innerHTML = "";
  resetPlanState();
  if (ws && ws.readyState === 1) {
    try {
      ws.send(JSON.stringify({ type: "hello", session_id: sessionId || "__new__" }));
    } catch (_) {}
  }
  loadSessions();
});

// ═══════════════════════════════════════════════════════════
// Digest (Task 24-c: дайджест журнала локальной моделью)
// ═══════════════════════════════════════════════════════════
async function loadDigest() {
  const el = $("settings-digest");
  if (!el) return;
  try {
    const d = await api("/api/digest/latest");
    if (!d.enabled || !d.digest || !d.digest.text) {
      el.textContent = "Дайджест пока не готов (воркер ждёт Ollama или выключен).";
      return;
    }
    const when = new Date((d.digest.ts || 0) * 1000).toLocaleString();
    el.textContent = `${d.digest.text}\n\n— ${when} · источник: ${d.digest.source} · событий: ${d.digest.events}`;
  } catch {
    el.textContent = "—";
  }
}

$("digest-refresh-btn").onclick = async () => {
  const el = $("settings-digest");
  el.textContent = "Готовлю дайджест локальной моделью… (может занять до минуты)";
  try {
    await api("/api/digest/refresh", { method: "POST", body: {} });
  } catch (e) {
    el.textContent = "Ошибка: " + (e.message || e);
  }
  loadDigest();
};

// ═══════════════════════════════════════════════════════════
// Project
// ═══════════════════════════════════════════════════════════
async function loadProject() {
  try {
    const d = await api("/api/project");
    const short = (d.project_root || "").split(/[\\/]/).slice(-2).join("/");
    projectLabel.textContent = short || "—";
    projectBtn.title = d.project_root || "не задан";
    return d;
  } catch {
    projectLabel.textContent = "?";
    return null;
  }
}

projectBtn.onclick = async () => {
  const d = await loadProject();
  $("project-current-path").textContent = d?.project_root || "—";
  $("project-path-input").value = d?.project_root || "";
  $("project-feedback").style.display = "none";
  $("project-servers").textContent = d?.enabled_servers?.length
    ? `Активные модули: ${d.enabled_servers.join(", ")}`
    : "—";
  $("project-modal").classList.remove("hidden");
};

$("project-close").onclick = () =>
  $("project-modal").classList.add("hidden");

$("project-save-btn").onclick = async () => {
  const path = $("project-path-input").value.trim();
  if (!path) return;
  try {
    await api("/api/project/set", { method: "POST", body: { path } });
    await loadProject();
    $("project-modal").classList.add("hidden");
  } catch (e) {
    alert(e.message);
  }
};

$("project-browse-btn").onclick = async () => {
  const btn = $("project-browse-btn");
  btn.disabled = true;
  try {
    const d = await api("/api/project/select-dialog", { method: "POST" });
    if (d.ok && d.path) {
      $("project-path-input").value = d.path;
    } else {
      $("project-feedback").style.display = "";
      $("project-feedback").textContent = "Диалог отменён";
    }
  } catch (e) {
    $("project-feedback").style.display = "";
    $("project-feedback").textContent = "Ошибка: " + e.message;
  } finally {
    btn.disabled = false;
  }
};

// ═══════════════════════════════════════════════════════════
// Cache
// ═══════════════════════════════════════════════════════════
async function refreshCacheStats() {
  try {
    const d = await api("/api/cache/stats");
    if (!d.enabled) {
      cacheStatsEl.textContent = "cache off";
      return;
    }
    const rate = Math.round((d.hit_rate || 0) * 100);
    cacheStatsEl.textContent = `cache ${rate}%`;
    cacheStatsEl.title =
      `Hits: ${d.hits}\nMisses: ${d.misses}\nRecords: ${d.total}`;
  } catch {
    cacheStatsEl.textContent = "cache ?";
  }
}

// ═══════════════════════════════════════════════════════════
// Settings tabs
// ═══════════════════════════════════════════════════════════
// Переключение вкладок настроек — через делегирование на документе.
// Важно: вкладки фич (мост, кластер, память, заметки, эксплуатация,
// история-PG, журнал) добавляются в DOM ПОСЛЕ загрузки app.js, поэтому
// прямая привязка btn.onclick их не накрывала — панели фич не активировались
// и клик по вкладке «ничего не делал». Делегирование чинит это для всех
// текущих и будущих вкладок.
document.addEventListener("click", (e) => {
  const btn = e.target && e.target.closest
    ? e.target.closest(".settings-tabs .tab-btn") : null;
  if (!btn || !btn.dataset.tab) return;
  document.querySelectorAll(".tab-btn").forEach(b =>
    b.classList.remove("active"));
  document.querySelectorAll(".tab-panel").forEach(p =>
    p.classList.remove("active"));
  btn.classList.add("active");
  const panel = document.querySelector(`[data-panel="${btn.dataset.tab}"]`);
  panel?.classList.add("active");
  onTabOpen(btn.dataset.tab);
});

async function onTabOpen(tab) {
  try {
    if (tab === "system") await loadSystemInfo();
    else if (tab === "agents") await loadAgents();
    else if (tab === "mcp") await loadMCP();
    else if (tab === "capabilities") await loadCapabilities();
    else if (tab === "policies") { await loadPolicies(); await loadMetrics(); }
    else if (tab === "backup") await loadBackups();
    else if (tab === "synonyms") await loadSynonyms();
    else if (tab === "age") await loadAgeStats();
    else if (tab === "cdc") await loadCDC();
    else if (tab === "history") await loadHistory();
    else if (tab === "profiles") await loadProfiles();
    else if (tab === "audit") await loadAudit();
    else if (tab === "rollback") await loadRollback();
    else if (tab === "diagnostics") await loadDiagnostics();
  } catch (e) {
    console.error(e);
  }
}

settingsBtn.onclick = async () => {
  snapshotData = await api("/api/registry/snapshot").catch(() => null);
  settingsModal.classList.remove("hidden");
  const active = document.querySelector(".tab-btn.active")?.dataset.tab
    || "system";
  await onTabOpen(active);
};

settingsClose.onclick = () => settingsModal.classList.add("hidden");

// ═══════════════════════════════════════════════════════════
// System tab
// ═══════════════════════════════════════════════════════════
async function loadSystemInfo() {
  const el = $("sys-health");
  try {
    const [health, enrichment] = await Promise.all([
      api("/api/db/health").catch(() => ({ enabled: false })),
      api("/api/enrichment/status").catch(() => ({ enabled: false })),
    ]);

    const lines = [];
    lines.push(`PostgreSQL: ${health.enabled ? (health.healthy ? "✓ online" : "✗ offline") : "disabled"}`);

    if (enrichment.enabled) {
      const w = enrichment.worker || {};
      lines.push(`Enrichment worker: ${w.running ? "✓ running" : "✗ stopped"}`);
      lines.push(`  Model: ${enrichment.ollama?.model || "—"}`);
      lines.push(`  Healthy: ${enrichment.ollama?.healthy ? "yes" : "no"}`);
      lines.push(`  Processed: ${w.processed_total || 0}`);
      lines.push(`  Pending: ${enrichment.db_stats?.pending || 0}`);
      lines.push(`  Enriched: ${enrichment.db_stats?.enriched || 0}`);
    } else {
      lines.push(`Enrichment: disabled`);
    }

    el.textContent = lines.join("\n");
  } catch (e) {
    el.textContent = "Ошибка: " + e.message;
  }

  // Cache stats
  try {
    const c = await api("/api/cache/stats");
    $("settings-cache-stats").textContent = c.enabled
      ? `Записей: ${c.total}\nHits: ${c.hits}\nMisses: ${c.misses}\nHit rate: ${Math.round((c.hit_rate || 0) * 100)}%`
      : "Кэш отключён";
  } catch {}

  // Enrichment details
  try {
    const e = await api("/api/enrichment/status");
    $("settings-enrichment").textContent = e.enabled
      ? `Worker: ${e.worker?.running ? "работает" : "остановлен"}\n` +
        `Модель: ${e.ollama?.model}\n` +
        `Pending: ${e.db_stats?.pending || 0}\n` +
        `Enriched: ${e.db_stats?.enriched || 0}\n` +
        `Avg importance: ${(e.db_stats?.avg_importance || 0).toFixed(3)}\n` +
        `Важных: ${e.db_stats?.important || 0}\n` +
        `Шум: ${e.db_stats?.noise || 0}`
      : "Обогащение отключено";
  } catch {}
}

$("cache-clear-btn").onclick = async () => {
  await api("/api/cache/clear", { method: "POST" });
  refreshCacheStats();
  loadSystemInfo();
};

$("cache-reset-metrics-btn").onclick = async () => {
  await api("/api/cache/reset-metrics", { method: "POST" });
  refreshCacheStats();
  loadSystemInfo();
};

$("enrichment-trigger-btn").onclick = async () => {
  try {
    await api("/api/enrichment/trigger", { method: "POST" });
    alert("Воркер разбужен");
  } catch (e) {
    alert(e.message);
  }
};

$("enrichment-batch-btn").onclick = async () => {
  try {
    const r = await api("/api/enrichment/process-batch", { method: "POST" });
    alert(`Обработано: ${r.processed}`);
    loadSystemInfo();
  } catch (e) {
    alert(e.message);
  }
};

// ═══════════════════════════════════════════════════════════
// Agents
// ═══════════════════════════════════════════════════════════
async function loadAgents() {
  const d = await api("/api/registry/agents");
  const list = $("agents-list");
  const groups = { active: [], degraded: [], unavailable: [], disabled: [] };
  d.agents.forEach(a => {
    if (groups[a.status]) groups[a.status].push(a);
    else groups.unavailable.push(a);
  });

  $("agents-summary").textContent =
    `Активных: ${groups.active.length} · degraded: ${groups.degraded.length} · ` +
    `недоступных: ${groups.unavailable.length} · отключено: ${groups.disabled.length}`;

  list.innerHTML = "";
  for (const status of ["active", "degraded", "unavailable", "disabled"]) {
    for (const a of groups[status]) {
      const card = document.createElement("div");
      card.className = `agent-card status-${a.status}`;

      const reasons = [...(a.reasons || []), ...(a.degraded_reasons || [])];
      const reasonsHtml = reasons.length
        ? `<div style="color:#fca5a5;font-size:11px;margin-top:6px">${
            reasons.slice(0, 3).map(escapeHtml).join("<br>")
          }</div>`
        : "";

      const badge = {
        active: "badge-green", degraded: "badge-yellow",
        unavailable: "badge-red", disabled: "badge-gray",
        failed: "badge-red",
      }[a.status] || "badge-gray";

      const enabled = a.status !== "disabled";

      card.innerHTML = `
        <div class="agent-header">
          <div class="agent-title">
            <span>${escapeHtml(a.icon || "🤖")}</span>
            <strong>${escapeHtml(a.title)}</strong>
            <code class="agent-id">${escapeHtml(a.id)}</code>
            <span class="badge ${badge}">${escapeHtml(a.status)}</span>
          </div>
          <label class="switch">
            <input type="checkbox" data-agent="${escapeHtml(a.id)}"
                   ${enabled ? "checked" : ""} />
            <span class="slider"></span>
          </label>
        </div>
        <div class="agent-desc">${escapeHtml(a.description || "")}</div>
        ${reasonsHtml}
        <div class="agent-actions" style="margin-top:8px;display:flex;gap:6px">
          <button class="secondary" data-check="${escapeHtml(a.id)}">🔍 Проверить</button>
        </div>
      `;
      list.appendChild(card);
    }
  }

  list.querySelectorAll("[data-agent]").forEach(el => {
    el.onchange = async () => {
      try {
        await api(`/api/registry/agents/${el.dataset.agent}/enable`, {
          method: "POST", body: { enabled: el.checked },
        });
        await loadAgents();
      } catch (e) {
        alert(e.message);
        el.checked = !el.checked;
      }
    };
  });

  list.querySelectorAll("[data-check]").forEach(el => {
    el.onclick = async () => {
      el.disabled = true;
      el.textContent = "⏳";
      try {
        await api(`/api/registry/agents/${el.dataset.check}/check`,
                  { method: "POST" });
        await loadAgents();
      } catch (e) {
        alert(e.message);
      } finally {
        el.disabled = false;
        el.textContent = "🔍 Проверить";
      }
    };
  });
}

$("agents-refresh").onclick = loadAgents;

// ═══════════════════════════════════════════════════════════
// MCP
// ═══════════════════════════════════════════════════════════
async function loadMCP() {
  const d = await api("/api/registry/mcp");
  const list = $("mcp-list");
  const enabled = d.mcp_servers.filter(m => m.enabled).length;
  const alive = d.mcp_servers.filter(m => m.alive).length;
  $("mcp-summary").textContent =
    `Всего: ${d.mcp_servers.length} · включено: ${enabled} · alive: ${alive}`;

  list.innerHTML = "";
  for (const m of d.mcp_servers) {
    const card = document.createElement("div");
    card.className = `agent-card${m.enabled ? "" : " status-disabled"}`;
    card.innerHTML = `
      <div class="agent-header">
        <div class="agent-title">
          <code class="agent-id">${escapeHtml(m.id)}</code>
          ${m.alive
            ? '<span class="badge badge-green">alive</span>'
            : '<span class="badge badge-gray">offline</span>'}
        </div>
        <label class="switch">
          <input type="checkbox" data-mcp="${escapeHtml(m.id)}"
                 ${m.enabled ? "checked" : ""} />
          <span class="slider"></span>
        </label>
      </div>
      <div class="agent-desc">${escapeHtml(m.description || "")}</div>
    `;
    list.appendChild(card);
  }

  list.querySelectorAll("[data-mcp]").forEach(el => {
    el.onchange = async () => {
      try {
        await api(`/api/registry/mcp/${el.dataset.mcp}/enable`, {
          method: "POST", body: { enabled: el.checked },
        });
        await loadMCP();
      } catch (e) {
        alert(e.message);
        el.checked = !el.checked;
      }
    };
  });
}

$("mcp-refresh").onclick = loadMCP;

// ═══════════════════════════════════════════════════════════
// Capabilities
// ═══════════════════════════════════════════════════════════
async function loadCapabilities() {
  const d = await api("/api/registry/capabilities");
  const list = $("capabilities-list");
  list.innerHTML = d.capabilities.length
    ? d.capabilities.map(c => `
      <div class="agent-card">
        <div class="agent-title">
          <code class="agent-id">${escapeHtml(c.id)}</code>
          <span class="badge badge-blue">${escapeHtml(c.strategy)}</span>
        </div>
        <div class="agent-desc">${escapeHtml(c.description || "")}</div>
        <pre class="stats">${escapeHtml(JSON.stringify(c.resolved, null, 2))}</pre>
      </div>
    `).join("")
    : '<div class="summary">Нет capabilities</div>';
}

$("capabilities-refresh").onclick = loadCapabilities;

// ═══════════════════════════════════════════════════════════
// Policies
// ═══════════════════════════════════════════════════════════
async function loadPolicies() {
  const d = await api("/api/policies");
  const list = $("policies-list");
  const stats = d.stats || {};

  $("settings-policies-stats").textContent =
    `Всего: ${stats.total || 0}\n` +
    `Allow: ${stats.by_decision?.allow || 0}, ` +
    `Deny: ${stats.by_decision?.deny || 0}\n` +
    `TTL: ${stats.ttl_days || 30} дн`;

  list.innerHTML = d.policies.length
    ? d.policies.map(p => `
      <div class="policy-item">
        <div class="policy-info">
          <div class="policy-tool">${escapeHtml(p.tool)}</div>
          <div class="policy-meta">
            ${p.scope === "tool" ? "любой" : `path: ${escapeHtml(p.path_pattern)}`}
            · ${p.use_count || 0} исп. · ${fmtAge(p.last_used_at)}
          </div>
        </div>
        <span class="policy-decision ${p.decision}">${p.decision}</span>
        <button class="policy-delete" data-pid="${p.id}">✕</button>
      </div>
    `).join("")
    : '<div class="empty">Нет политик</div>';

  list.querySelectorAll("[data-pid]").forEach(el => {
    el.onclick = async () => {
      await api(`/api/policies/${el.dataset.pid}`, { method: "DELETE" });
      loadPolicies();
    };
  });
}

async function loadMetrics() {
  try {
    const d = await api("/api/policies/metrics?top=10");
    const lines = ["🏆 Топ:"];
    (d.top_active || []).forEach((p, i) => {
      lines.push(`  ${i + 1}. ${p.tool} — ${p.use_count} исп.`);
    });
    lines.push("", `📉 Удалено: ${d.deleted_overall?.count || 0}`);
    $("settings-metrics").textContent = lines.join("\n");
  } catch {}
}

$("policies-clear-btn").onclick = async () => {
  if (!confirm("Удалить все политики?")) return;
  await api("/api/policies/clear", { method: "POST" });
  loadPolicies();
};

$("policies-cleanup-btn").onclick = async () => {
  if (!confirm("Очистить старые политики?")) return;
  const r = await api("/api/policies/cleanup", { method: "POST" });
  alert(`Удалено: ${r.removed}`);
  loadPolicies();
};

$("policies-backup-btn").onclick = async () => {
  const r = await api("/api/policies/backup", { method: "POST" });
  alert(`Бэкап: ${r.backup_path}`);
};

$("policies-export-btn").onclick = () => {
  window.location.href = "/api/policies/export";
};

$("policies-import-btn").onclick = () => $("policies-import-file").click();

$("policies-import-file").onchange = async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  pendingImportFile = f;
  pendingImportMode = $("policies-import-mode").value || "merge";

  const fd = new FormData();
  fd.append("file", f);

  try {
    const r = await fetch(
      `/api/policies/import/preview?mode=${pendingImportMode}`,
      { method: "POST", body: fd }
    );
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Error");

    $("import-preview-summary").textContent =
      `Файл: ${f.name}\nРежим: ${pendingImportMode}\n` +
      `Добавится: ${d.added}\nКонфликтов: ${d.details.conflicts.length}\n` +
      `Невалидных: ${d.details.invalid.length}`;

    $("import-new-count").textContent = d.details.new.length;
    $("import-conflicts-count").textContent = d.details.conflicts.length;
    $("import-invalid-count").textContent = d.details.invalid.length;
    $("import-preview-modal").classList.remove("hidden");
  } catch (err) {
    alert("Ошибка: " + err.message);
    pendingImportFile = null;
  } finally {
    $("policies-import-file").value = "";
  }
};

$("import-preview-cancel").onclick = () => {
  $("import-preview-modal").classList.add("hidden");
  pendingImportFile = null;
};

$("import-preview-apply").onclick = async () => {
  if (!pendingImportFile) return;
  const fd = new FormData();
  fd.append("file", pendingImportFile);
  try {
    const r = await fetch(
      `/api/policies/import?mode=${pendingImportMode}`,
      { method: "POST", body: fd }
    );
    const d = await r.json();
    alert(`Добавлено: ${d.added}, пропущено: ${d.skipped}`);
    $("import-preview-modal").classList.add("hidden");
    pendingImportFile = null;
    loadPolicies();
  } catch (err) {
    alert("Ошибка: " + err.message);
  }
};

// ═══════════════════════════════════════════════════════════
// Backup
// ═══════════════════════════════════════════════════════════
async function loadBackups() {
  try {
    const d = await api("/api/backup/list");
    if (!d.enabled) {
      $("backup-info").textContent = "Бэкапы не настроены";
      return;
    }
    $("backup-info").textContent =
      `Всего: ${d.stats.count}\n` +
      `Хранится последних: ${d.stats.keep_last}\n` +
      `Общий размер: ${d.stats.total_mb} MB\n` +
      `Каталог: ${d.stats.backup_dir}`;

    const list = $("backup-list");
    list.innerHTML = d.backups.length
      ? d.backups.map(b => `
        <div class="agent-card">
          <div class="agent-header">
            <div class="agent-title">
              <code class="agent-id">${escapeHtml(b.name)}</code>
              <span class="summary">${b.size_mb} MB · ${b.age_hours} ч назад</span>
            </div>
            <div class="agent-actions">
              <button class="secondary danger-text" data-bdel="${escapeHtml(b.name)}">✕</button>
            </div>
          </div>
        </div>
      `).join("")
      : '<div class="empty">Нет бэкапов</div>';

    list.querySelectorAll("[data-bdel]").forEach(el => {
      el.onclick = async () => {
        if (!confirm("Удалить бэкап?")) return;
        await api(`/api/backup/${el.dataset.bdel}`, { method: "DELETE" });
        loadBackups();
      };
    });
  } catch (e) {
    $("backup-info").textContent = "Ошибка: " + e.message;
  }
}

$("backup-refresh").onclick = loadBackups;

$("backup-create-btn").onclick = async () => {
  const btn = $("backup-create-btn");
  btn.disabled = true;
  btn.textContent = "⏳...";
  try {
    const r = await api("/api/backup/create", { method: "POST" });
    alert(`Создан: ${r.name} (${r.size_mb} MB)`);
    loadBackups();
  } catch (e) {
    alert(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "💾 Создать бэкап";
  }
};

// ═══════════════════════════════════════════════════════════
// Synonyms
// ═══════════════════════════════════════════════════════════
async function loadSynonyms() {
  try {
    const d = await api("/api/synonyms");
    if (!d.enabled) {
      $("synonyms-summary").textContent = "PG не настроен";
      return;
    }
    const s = d.stats || {};
    $("synonyms-summary").textContent =
      `Всего: ${s.total_terms} · Категорий: ${s.categories}`;

    const list = $("synonyms-list");
    list.innerHTML = d.synonyms.length
      ? d.synonyms.slice(0, 50).map(s => `
        <div class="agent-card">
          <div class="agent-header">
            <div class="agent-title">
              <code class="agent-id">${escapeHtml(s.term)}</code>
              ${s.category
                ? `<span class="badge badge-blue">${escapeHtml(s.category)}</span>`
                : ""}
            </div>
            <button class="policy-delete" data-sdel="${escapeHtml(s.term)}">✕</button>
          </div>
          <div class="agent-desc">${escapeHtml((s.syns || []).join(", "))}</div>
        </div>
      `).join("")
      : '<div class="empty">Нет синонимов</div>';

    list.querySelectorAll("[data-sdel]").forEach(el => {
      el.onclick = async () => {
        await api(`/api/synonyms/${encodeURIComponent(el.dataset.sdel)}`,
                  { method: "DELETE" });
        loadSynonyms();
      };
    });
  } catch (e) {
    $("synonyms-summary").textContent = "Ошибка: " + e.message;
  }
}

$("synonyms-refresh").onclick = loadSynonyms;

$("syn-add-btn").onclick = async () => {
  const term = $("syn-term").value.trim();
  const syns = $("syn-syns").value.split(",").map(s => s.trim())
    .filter(Boolean);
  const category = $("syn-category").value.trim();

  if (!term || !syns.length) {
    alert("Заполните термин и синонимы");
    return;
  }

  try {
    await api("/api/synonyms", {
      method: "POST",
      body: { term, syns, category: category || null },
    });
    $("syn-term").value = "";
    $("syn-syns").value = "";
    $("syn-category").value = "";
    loadSynonyms();
  } catch (e) {
    alert(e.message);
  }
};

// ═══════════════════════════════════════════════════════════
// AGE
// ═══════════════════════════════════════════════════════════
async function loadAgeStats() {
  try {
    const d = await api("/api/age/stats");
    if (!d.enabled) {
      $("age-stats").textContent =
        "Apache AGE не включён. Установите AGE_ENABLED=true";
      return;
    }
    $("age-stats").textContent =
      `Graph: ${d.graph}\nNodes: ${d.nodes}\nEdges: ${d.edges}` +
      (d.error ? `\nError: ${d.error}` : "");
  } catch (e) {
    $("age-stats").textContent = "Ошибка: " + e.message;
  }
}

$("age-refresh").onclick = loadAgeStats;

$("age-sync-btn").onclick = async () => {
  try {
    const r = await api("/api/age/sync", { method: "POST" });
    alert(`Синхронизировано: nodes=${r.nodes}, edges=${r.edges}`);
    loadAgeStats();
  } catch (e) {
    alert(e.message);
  }
};

$("age-run-btn").onclick = async () => {
  const query = $("age-query").value.trim();
  const columns = $("age-columns").value.split(",").map(s => s.trim())
    .filter(Boolean);

  if (!query) return;

  try {
    const r = await api("/api/age/cypher", {
      method: "POST",
      body: { query, columns },
    });
    $("age-result").textContent = JSON.stringify(r, null, 2);
    $("age-result").classList.remove("hidden");
  } catch (e) {
    alert(e.message);
  }
};

// ═══════════════════════════════════════════════════════════
// CDC
// ═══════════════════════════════════════════════════════════
async function loadCDC() {
  try {
    const d = await api("/api/cdc/status");
    if (!d.enabled) {
      $("cdc-stats").textContent = "CDC не настроен (KAFKA_ENABLED=false)";
      return;
    }
    const p = d.publisher || {};
    $("cdc-stats").textContent =
      `Running: ${d.running}\n` +
      `Channels: ${(d.channels || []).join(", ")}\n` +
      `\nReceived: ${d.received_total}\n` +
      `Published: ${d.published_total}\n` +
      `Parse errors: ${d.parse_errors}\n` +
      `\nKafka: ${p.available ? "online" : "offline"}\n` +
      `Buffer: ${p.buffer_size}/${p.buffer_capacity}\n` +
      `Sent: ${p.sent_total}\n` +
      `Dropped: ${p.dropped_total}`;
  } catch (e) {
    $("cdc-stats").textContent = "Ошибка: " + e.message;
  }
}

$("cdc-refresh").onclick = loadCDC;

// ═══════════════════════════════════════════════════════════
// History
// ═══════════════════════════════════════════════════════════
async function loadHistory() {
  const d = await api("/api/registry/history?limit=20");
  const list = $("history-list");
  list.innerHTML = d.snapshots.length
    ? d.snapshots.map((s, i) => {
        const date = new Date(s.built_at * 1000).toLocaleString();
        const next = d.snapshots[i + 1];
        const diffBtn = next
          ? `<button class="secondary" data-diff="${next.id}|${s.id}">📊 Diff</button>`
          : "";
        return `
          <div class="agent-card">
            <div class="agent-header">
              <div class="agent-title">
                <span class="badge badge-blue">slot ${escapeHtml(s.slot)}</span>
                <code class="agent-id">${escapeHtml(s.id)}</code>
                <span class="summary">${escapeHtml(s.reason)}</span>
              </div>
              ${diffBtn}
            </div>
            <div class="agent-desc">${date} · ${s.agents_count} агентов · ${s.mcp_count} MCP</div>
          </div>
        `;
      }).join("")
    : '<div class="summary">История пуста</div>';

  list.querySelectorAll("[data-diff]").forEach(el => {
    el.onclick = async () => {
      const [from, to] = el.dataset.diff.split("|");
      const d = await api(`/api/registry/history/diff?from_id=${to}&to_id=${from}`);
      const pre = $("history-diff");
      pre.classList.remove("hidden");
      pre.textContent = JSON.stringify(d, null, 2);
    };
  });
}

$("history-refresh").onclick = loadHistory;

// ═══════════════════════════════════════════════════════════
// Profiles
// ═══════════════════════════════════════════════════════════
async function loadProfiles() {
  const d = await api("/api/registry/profiles");
  const list = $("profiles-list");
  list.innerHTML = d.profiles.length
    ? d.profiles.map(p => `
        <div class="agent-card">
          <div class="agent-header">
            <div class="agent-title">
              <span class="badge ${p.is_builtin ? "badge-gray" : "badge-green"}">${p.is_builtin ? "builtin" : "custom"}</span>
              <code class="agent-id">${escapeHtml(p.id)}</code>
              <span class="summary">${escapeHtml(p.title || "")}</span>
            </div>
            <div class="agent-actions">
              <button class="secondary" data-apply="${escapeHtml(p.id)}">✅ Применить</button>
              ${p.is_builtin ? "" : `<button class="secondary" data-del="${escapeHtml(p.id)}">🗑</button>`}
            </div>
          </div>
          <div class="agent-desc">${escapeHtml(p.description || "")}</div>
          <div class="mcp-tools">${Object.keys(p.overrides || {}).length} overrides</div>
        </div>
      `).join("")
    : '<div class="summary">Профили не найдены</div>';

  list.querySelectorAll("[data-apply]").forEach(el => {
    el.onclick = async () => {
      if (!confirm(`Применить профиль "${el.dataset.apply}"?`)) return;
      await api(`/api/registry/profiles/${el.dataset.apply}/apply`, { method: "POST" });
      snapshotData = await api("/api/registry/snapshot").catch(() => null);
      await loadProfiles();
    };
  });
  list.querySelectorAll("[data-del]").forEach(el => {
    el.onclick = async () => {
      if (!confirm(`Удалить профиль "${el.dataset.del}"?`)) return;
      await api(`/api/registry/profiles/${el.dataset.del}`, { method: "DELETE" });
      await loadProfiles();
    };
  });
}

$("profiles-refresh").onclick = loadProfiles;

$("profile-save-current").onclick = async () => {
  const id = ($("profile-new-id").value || "").trim();
  if (!id) { alert("Введите id профиля"); return; }
  await api(`/api/registry/profiles/${encodeURIComponent(id)}/save`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ title: id }),
  });
  $("profile-new-id").value = "";
  await loadProfiles();
};

$("profile-export-current").onclick = () => {
  window.open("/api/registry/overrides/export", "_blank");
};

$("profile-import-btn").onclick = () => $("profile-import-file").click();

$("profile-import-file").onchange = async (e) => {
  const file = e.target.files[0];
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    await api("/api/registry/profiles/import", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(data),
    });
    await loadProfiles();
  } catch (err) {
    alert("Ошибка импорта: " + err.message);
  } finally {
    e.target.value = "";
  }
};

// ═══════════════════════════════════════════════════════════
// Audit
// ═══════════════════════════════════════════════════════════
async function loadAudit() {
  const d = await api("/api/registry/audit?limit=100");
  const list = $("audit-list");
  list.innerHTML = d.events.length
    ? d.events.map(e => `
        <div class="agent-card">
          <div class="agent-header">
            <div class="agent-title">
              <span class="badge badge-blue">${escapeHtml(e.action || "")}</span>
              <code class="agent-id">${escapeHtml(e.actor || "system")}</code>
              <span class="summary">${escapeHtml(e.target || "")}</span>
            </div>
          </div>
          <div class="agent-desc">${escapeHtml(e.created_at || "")}</div>
        </div>
      `).join("")
    : '<div class="summary">Аудит пуст</div>';
}

$("audit-refresh").onclick = loadAudit;

// ═══════════════════════════════════════════════════════════
// Rollback (runtime.yaml versions)
// ═══════════════════════════════════════════════════════════
const ROLLBACK_KINDS = ["agents", "mcp_servers", "capabilities"];

async function loadRollback() {
  const list = $("rollback-list");
  const sections = await Promise.all(ROLLBACK_KINDS.map(async kind => {
    const d = await api(`/api/registry/rollback/${kind}`).catch(() => ({ versions: [] }));
    const rows = (d.versions || []).map(v => `
      <div class="agent-card">
        <div class="agent-header">
          <div class="agent-title">
            <code class="agent-id">${escapeHtml(v.dir || v.name || "")}</code>
            <span class="summary">${escapeHtml(v.tag || "")}</span>
          </div>
          <button class="secondary" data-kind="${kind}" data-ver="${escapeHtml(v.dir || v.name || "")}">⏪ Восстановить</button>
        </div>
        <div class="agent-desc">${escapeHtml(v.built_at || v.date || "")}</div>
      </div>
    `).join("");
    return `<h4>${kind}</h4>${rows || '<div class="summary">Нет версий</div>'}`;
  }));
  list.innerHTML = sections.join("");

  list.querySelectorAll("[data-ver]").forEach(el => {
    el.onclick = async () => {
      if (!confirm(`Восстановить ${el.dataset.kind} из ${el.dataset.ver}?`)) return;
      await api(`/api/registry/rollback/${el.dataset.kind}/restore`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ version_dir: el.dataset.ver }),
      });
      snapshotData = await api("/api/registry/snapshot").catch(() => null);
      await loadRollback();
    };
  });
}

$("rollback-refresh").onclick = loadRollback;

// ═══════════════════════════════════════════════════════════
// Diagnostics
// ═══════════════════════════════════════════════════════════
async function loadDiagnostics() {
  snapshotData = await api("/api/registry/snapshot");
  $("diag-snapshot").textContent = JSON.stringify(snapshotData, null, 2);
}

$("show-prompt").onclick = async () => {
  const d = await api("/api/registry/prompt");
  const pre = $("diag-prompt");
  pre.textContent = d.prompt;
  pre.classList.toggle("hidden");
};

// ═══════════════════════════════════════════════════════════
// Form submit
// ═══════════════════════════════════════════════════════════
form.addEventListener("submit", e => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;
  const model = currentModel || null;
  lastSentModel = model;
  // Task 27: активное редактирование — не добавляем сообщение, а шлём
  // edit_message (сервер перепишет историю и сам перезапустит ответ)
  if (editing) {
    sendEdit(editing.index, text);
    return;
  }
  addMsg("user", text, "", chatIndex++);
  ws.send(JSON.stringify({ query: text, model, session_id: sessionId }));
  input.value = "";
  setBusy(true);
});

input.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    form.dispatchEvent(new Event("submit"));
  }
});

// ═══════════════════════════════════════════════════════════
// Features (Этап 4): вкладки из реестра, а не из захардкоженного списка
// ═══════════════════════════════════════════════════════════
async function loadFeatures() {
  try {
    const d = await api("/api/features");
    (d.features || []).forEach(f => {
      if (!f.js_url || document.querySelector(`script[data-feature="${f.id}"]`)) return;
      const s = document.createElement("script");
      s.src = f.js_url;
      s.dataset.feature = f.id;
      s.defer = true;
      document.head.appendChild(s);
    });
  } catch (_) { /* фичи необязательны */ }
}

// ═══════════════════════════════════════════════════════════
// Task 27: импорт чатов с сайта (JSON-экспорт провайдера)
// ═══════════════════════════════════════════════════════════
const importBtn = $("import-chats");
const importFile = $("import-file");
const editCancelBtn = $("edit-cancel");

if (importBtn && importFile) {
  importBtn.addEventListener("click", () => importFile.click());
  importFile.addEventListener("change", async () => {
    const file = importFile.files && importFile.files[0];
    importFile.value = "";
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      const d = await api("/api/chats/import?provider=deepseek",
        { method: "POST", body: data });
      const n = d.count || 0;
      addMsg("system",
        `⬆️ Импортировано чатов: ${n}. Список «Последние чаты» обновлён.`);
      await loadSessions();
      const first = (d.imported || [])[0];
      if (first && first.session_id) {
        // сразу открываем первый импортированный чат
        sessionId = first.session_id;
        localStorage.setItem("llm_session_id", sessionId);
        if (ws && ws.readyState === 1) {
          ws.send(JSON.stringify({ type: "hello", session_id: sessionId }));
        }
        await loadSessions();
      }
    } catch (err) {
      addMsg("system", `❌ Импорт не удался: ${err.message || err}`);
    }
  });
}
if (editCancelBtn) {
  editCancelBtn.addEventListener("click", cancelEdit);
}

// ═══════════════════════════════════════════════════════════
// Init
// ═══════════════════════════════════════════════════════════
loadModels();
loadSessions();
loadProject();
refreshCacheStats();
refreshSystemStatus();
loadFeatures();
loadDigest();

setInterval(refreshCacheStats, 10000);
setInterval(refreshSystemStatus, 30000);
setInterval(loadSessions, 60000);

connect();
