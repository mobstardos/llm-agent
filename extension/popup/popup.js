/* popup.js — статус моста, адрес сервера, быстрый захват, панель. */
"use strict";
const apiX = (typeof browser !== "undefined") ? browser : chrome;

const $ = (s) => document.querySelector(s);
const statusEl = $("#status");
let base = "http://127.0.0.1:8000";

async function api(path, opts = {}, timeoutMs = 8000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(base + path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: ctl.signal,
    });
    const data = await res.json().catch(() => null);
    if (!res.ok) throw new Error((data && (data.detail || data.error))
      || ("HTTP " + res.status));
    return data;
  } finally { clearTimeout(t); }
}

async function ping() {
  statusEl.textContent = "проверяем связь…";
  try {
    const s = await api("/api/bridge/status", {}, 6000);
    $("#dot").className = "dot on";
    statusEl.textContent =
      "сервер: OK · расширение: " + (s.extension || "?") +
      "\nвкладок: " + s.tabs.count + " · захватов: " + s.captures +
      " · задач в очереди: " + s.jobs.pending;
  } catch (e) {
    $("#dot").className = "dot off";
    statusEl.textContent = "нет связи с сервером: " + e.message +
      "\nЗапустите llm-agent (python run.py) и проверьте адрес/порт.";
  }
}

$("#save").onclick = async () => {
  base = ($("#url").value.trim() || "http://127.0.0.1:8000")
    .replace(/\/+$/, "");
  await apiX.storage.local.set({ serverUrl: base });
  statusEl.textContent = "адрес сохранён: " + base;
  ping();
};

$("#capture").onclick = async () => {
  statusEl.textContent = "захватываем…";
  try {
    const [tab] = await apiX.tabs.query({ active: true,
                                          currentWindow: true });
    const [res] = await apiX.scripting.executeScript({
      target: { tabId: tab.id },
      func: function (maxChars) {
        const sels = ["article", "main", '[role="main"]', ".article",
          "#content", ".content", "body"];
        let text = "";
        for (const s of sels) {
          const nodes = document.querySelectorAll(s);
          const parts = [];
          for (const n of nodes) {
            const t = (n.innerText || "").trim();
            if (t.length > 200) parts.push(t);
          }
          if (parts.length) {
            text = parts.sort((a, b) => b.length - a.length)[0];
            break;
          }
        }
        if (!text && document.body) text = document.body.innerText || "";
        if (text.length > maxChars) text = text.slice(0, maxChars) + "…";
        return { title: document.title || "", url: location.href,
                 text, selection: "", chars: text.length };
      },
      args: [40000],
    });
    const d = (res && res.result) || {};
    const r = await api("/api/bridge/capture", { method: "POST",
      body: { title: d.title, url: d.url, text: d.text,
              selection: d.selection, tab_id: tab.id } }, 15000);
    statusEl.textContent = "захват сохранён #" + (r && r.id) +
      " (" + (d.chars || 0) + " симв.)";
  } catch (e) {
    statusEl.textContent = "захват не удался: " + e.message;
  }
};

$("#panel").onclick = async () => {
  try {
    if (apiX.sidePanel && apiX.sidePanel.open) {
      const win = await apiX.windows.getCurrent();
      await apiX.sidePanel.open({ windowId: win.id });
      window.close(); return;
    }
  } catch (e) { /* фолбэк */ }
  try {
    if (apiX.sidebarAction && apiX.sidebarAction.open) {
      await apiX.sidebarAction.open();
      window.close(); return;
    }
  } catch (e) { /* фолбэк */ }
  await apiX.tabs.create({ url: apiX.runtime.getURL("panel/panel.html") });
  window.close();
};

$("#webui").onclick = async () => {
  await apiX.tabs.create({ url: base + "/" });
  window.close();
};

async function sendMsg(type, provider) {
  return new Promise((resolve) => {
    try {
      apiX.runtime.sendMessage({ type, provider }, (res) => {
        void apiX.runtime.lastError;   // прочитали — не роняем
        resolve(res || { ok: false, error: "нет ответа фона" });
      });
    } catch (e) {
      resolve({ ok: false, error: e.message });
    }
  });
}

const AUTH_PROVIDERS = {
  deepseek: { label: "DeepSeek", state: "#auth-deepseek-state",
              btn: "#auth-deepseek" },
  qwen:     { label: "Qwen",     state: "#auth-qwen-state",
              btn: "#auth-qwen" },
};

async function refreshAuthState() {
  for (const [pid, ui] of Object.entries(AUTH_PROVIDERS)) {
    const stateEl = $(ui.state);
    const btnEl = $(ui.btn);
    if (!stateEl || !btnEl) continue;
    let flow = null;
    try {
      const st = await sendMsg("auth_status", pid);
      flow = (st && st.authFlow) || null;
    } catch (e) { /* фон может спать — не страшно */ }
    const pending = flow && flow.provider === pid;
    try {
      const s = await api("/api/bridge/cookies/status", {}, 6000);
      const p = (s.providers || {})[pid] || {};
      if (pending) {
        stateEl.textContent = "ожидаю вход… после логина нажми «✅ забрать»";
        btnEl.textContent = "✅ " + ui.label + ": забрать";
      } else if (p.configured) {
        const age = p.age_min == null ? "" :
          (p.age_min < 90 ? Math.round(p.age_min) + " мин назад"
                          : Math.round(p.age_min / 60) + " ч назад");
        stateEl.textContent = "✓ настроен" + (age ? " · обновлён " + age : "") +
          (p.env_present && p.env_present.length
            ? " · " + p.env_present.length + " ключей" : "");
        btnEl.textContent = "🔄 " + ui.label + " (обновить)";
      } else {
        stateEl.textContent = "не настроен — нажми и войди в аккаунт";
        btnEl.textContent = "🔑 " + ui.label;
      }
    } catch (e) {
      stateEl.textContent = pending
        ? "ожидаю вход… (сервер недоступен)"
        : "сервер недоступен — куки некуда сохранить";
      btnEl.textContent = "🔑 " + ui.label;
    }
  }
}

for (const [pid, ui] of Object.entries(AUTH_PROVIDERS)) {
  const btnEl = $(ui.btn);
  if (!btnEl) continue;
  btnEl.onclick = async () => {
    const stateEl = $(ui.state);
    const st = await sendMsg("auth_status", pid);
    const pending = st && st.authFlow && st.authFlow.provider === pid;
    if (pending) {
      stateEl.textContent = "забираю куки…";
      const r = await sendMsg("auth_collect", pid);
      stateEl.textContent = r && r.ok
        ? "✓ сохранено: " + (r.saved_env || []).join(", ")
        : "не удалось: " + ((r && r.error) || "?");
    } else {
      stateEl.textContent = "открываю страницу входа…";
      const r = await sendMsg("auth_open", pid);
      stateEl.textContent = r && r.ok
        ? "ожидаю вход… после логина нажми «✅ забрать»"
        : "не удалось: " + ((r && r.error) || "?");
    }
    refreshAuthState();
  };
}

(async function boot() {
  const st = await apiX.storage.local.get(["serverUrl"]);
  base = (st.serverUrl || "http://127.0.0.1:8000").replace(/\/+$/, "");
  $("#url").value = base;
  ping();
  refreshAuthState();
})();
