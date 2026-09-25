/* bridge-core.js — общая фоновая логика расширения «LLM Agent Bridge».
   Работает и в Chromium (MV3 service worker, подгружается через
   importScripts), и в Firefox (MV3 event page, подгружается через
   background.scripts в манифесте). Кроссбраузерность — через apiX.

   Обязанности:
   * конфиг (адрес сервера) в storage.local;
   * регистрация снапшота вкладок (events: created/updated/activated/
     removed + периодически);
   * pull-очередь задач: GET /api/bridge/pull?wait=15 long-poll —
     MV3-воркер спит и WS держать нечем, поэтому поллинг с alarms;
   * исполнитель задач read_tab: chrome.scripting.executeScript —
     читает текст страницы БЕЗ content scripts (только по явной задаче);
   * контекстное меню: захват выделенного/страницы, «спросить агента»,
     «суммаризировать страницу»;
   * авторизация веб-чатов (Task 24-a): открыть страницу входа
     DeepSeek/Qwen, после логина собрать куки (cookies.getAll) и
     localStorage (scripting) и отправить на POST /api/bridge/cookies —
     сервер сохранит их в .env (DEEPSEEK_* / QWEN_WEB_*);
   * omnibox: ag <запрос> → запрос открывается в панели чата;
   * горячая клавиша wake-agent (Alt+Shift+B, меняется в настройках
     браузера): «разбудить» агента — открыть панель, сфокусировать ввод
     и подхватить выделенное на активной вкладке;
   * статус-бейдж: ON (сервер на связи) / OFF / … (работает задача). */
"use strict";

const apiX = (typeof browser !== "undefined") ? browser : chrome;

const BRIDGE_DEFAULTS = { serverUrl: "http://127.0.0.1:8000", enabled: true };
let bridgeCfg = Object.assign({}, BRIDGE_DEFAULTS);

/* ── Конфиг ─────────────────────────────────────────────── */
async function loadCfg() {
  try {
    const st = await apiX.storage.local.get(Object.keys(BRIDGE_DEFAULTS));
    bridgeCfg = Object.assign({}, BRIDGE_DEFAULTS, st || {});
  } catch (e) { /* дефолты */ }
  return bridgeCfg;
}

async function saveCfg(patch) {
  bridgeCfg = Object.assign({}, bridgeCfg, patch || {});
  await apiX.storage.local.set(patch || {});
}

function serverBase() {
  return (bridgeCfg.serverUrl || BRIDGE_DEFAULTS.serverUrl)
    .replace(/\/+$/, "");
}

/* ── HTTP с таймаутом ───────────────────────────────────── */
async function apiFetch(path, opts = {}, timeoutMs = 20000) {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const res = await fetch(serverBase() + path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: ctl.signal,
    });
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.detail || data.error)) ||
        ("HTTP " + res.status);
      throw new Error(String(msg).slice(0, 200));
    }
    return data;
  } finally {
    clearTimeout(t);
  }
}

/* ── Бейдж ──────────────────────────────────────────────── */
function setBadge(text, color) {
  try {
    apiX.action.setBadgeText({ text: text || "" });
    if (color) apiX.action.setBadgeBackgroundColor({ color });
  } catch (e) { /* action может отсутствовать на старых */ }
}
function badgeOk()  { setBadge("ON", "#2e7d32"); }
function badgeOff() { setBadge("OFF", "#9e9e9e"); }
function badgeBusy(){ setBadge("…", "#f9a825"); }

function notify(title, message) {
  try {
    apiX.notifications.create({
      type: "basic", title: title || "LLM Agent",
      message: (message || "").slice(0, 300), iconUrl: "icons/icon128.png",
    });
  } catch (e) { /* не критично */ }
}

/* ── Вкладки: снапшот для агентов ───────────────────────── */
async function registerTabs() {
  try {
    const tabs = await apiX.tabs.query({});
    const payload = {
      tabs: tabs
        .filter(t => /^https?:/i.test(t.url || ""))
        .map(t => ({
          id: t.id, title: t.title || "", url: t.url || "",
          active: !!t.active, window: t.windowId || 0,
        })),
      meta: { ua: (apiX.runtime && apiX.runtime.getManifest) ?
        apiX.runtime.getManifest().version : "" },
    };
    await apiFetch("/api/bridge/tabs", { method: "POST", body: payload },
      8000);
    return payload.tabs.length;
  } catch (e) {
    return -1;
  }
}

let regTimer = null;
function scheduleRegisterTabs(ms = 1500) {
  if (regTimer) return;
  regTimer = setTimeout(() => {
    regTimer = null;
    registerTabs();
  }, ms);
}

/* ── Извлечение текста страницы (функция сериализуется в страницу!) ── */
function pageExtractArticle(maxChars) {
  const LIMIT = Math.max(1000, Number(maxChars) || 40000);
  const sel = (window.getSelection) ? String(window.getSelection() || "")
    : "";
  const pick = [];
  const sels = ["article", "main", '[role="main"]', ".article",
    "#content", ".content", "body"];
  for (const s of sels) {
    const nodes = document.querySelectorAll(s);
    for (const n of nodes) {
      const txt = (n.innerText || "").trim();
      if (txt.length > 200) pick.push(txt);
    }
    if (pick.length && pick.join("").length > LIMIT / 2) break;
  }
  let text = pick.sort((a, b) => b.length - a.length)[0] ||
    (document.body && document.body.innerText) || "";
  if (text.length > LIMIT) text = text.slice(0, LIMIT) + "…";
  return {
    title: document.title || "",
    url: location.href,
    text,
    selection: sel.slice(0, 4000),
    chars: text.length,
  };
}

function pageExtractSelection() {
  return String((window.getSelection && window.getSelection()) || "")
    .trim();
}

/* ── Исполнитель задач ──────────────────────────────────── */
async function findTab(payload) {
  const tabs = await apiX.tabs.query({});
  const ok = tabs.filter(t => /^https?:/i.test(t.url || ""));
  if (payload && payload.tab_id !== null && payload.tab_id !== undefined
      && payload.tab_id !== "") {
    const id = Number(payload.tab_id);
    const byId = ok.find(t => t.id === id);
    if (byId) return byId;
  }
  if (payload && payload.url_contains) {
    const needle = String(payload.url_contains).toLowerCase();
    return ok.find(t => (t.url || "").toLowerCase().includes(needle)) ||
      null;
  }
  return ok.find(t => t.active && t.lastAccessed !== undefined) ||
    ok.find(t => t.active) || ok[0] || null;
}

async function handleJob(job) {
  badgeBusy();
  try { await apiFetch("/api/bridge/jobs/" + job.id + "/ack",
    { method: "POST", body: {} }, 8000); } catch (e) { /* не критично */ }

  let ok = false, result = null, error = null;
  try {
    if (job.kind === "read_tab") {
      const tab = await findTab(job.payload || {});
      if (!tab) {
        error = "подходящая вкладка не найдена (нужен http/https)";
      } else {
        const maxChars = (job.payload && job.payload.max_chars) || 40000;
        const [res] = await apiX.scripting.executeScript({
          target: { tabId: tab.id },
          func: pageExtractArticle,
          args: [maxChars],
        });
        result = (res && res.result) || null;
        if (!result || !(result.text || result.selection)) {
          error = "страница не дала текста (about: / pdf / web store?)";
        } else {
          result.tab_id = tab.id;
          ok = true;
        }
      }
    } else {
      error = "неизвестный тип задачи: " + job.kind;
    }
  } catch (e) {
    error = String(e && e.message || e).slice(0, 300);
  }
  try {
    await apiFetch("/api/bridge/jobs/" + job.id + "/done", {
      method: "POST",
      body: { ok, result, error },
    }, 10000);
  } catch (e) {
    error = "не удалось отправить результат: " + e.message;
  }
  badgeOk();
}

/* ── Pull-цикл (вместо WS в service worker) ─────────────── */
let pulling = false;
async function pullOnce() {
  const r = await apiFetch("/api/bridge/pull?wait=15", {}, 25000);
  const jobs = (r && r.jobs) || [];
  for (const j of jobs) {
    await handleJob(j);      // последовательно: страницы тяжёлые
  }
  return jobs.length;
}

async function pullLoop() {
  if (pulling) return;
  pulling = true;
  try {
    while (pulling) {
      if (!bridgeCfg.enabled) { badgeOff(); await sleep(15000); continue; }
      try {
        await pullOnce();
        badgeOk();
      } catch (e) {
        badgeOff();
        await sleep(20000);
      }
      await sleep(800);
    }
  } finally {
    pulling = false;
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

/* Перезапуск цикла после сна воркера: alarms каждые 0.5 мин. */
async function ensureLoop() {
  await loadCfg();
  pullLoop();
}

/* ── Авторизация веб-чатов (Task 24-a) ─────────────────── */
/* Пользователь жмёт кнопку в popup → открываем страницу входа, помечаем
   поток «pending». Дальше два пути: авто-детект (таб окончил загрузку /
   alarm каждые 0.5 мин) — как только в куках появились «маркеры входа»,
   собираем и отправляем; либо вручную — кнопка «забрать сейчас». */
const WEB_AUTH = {
  deepseek: {
    label: "DeepSeek",
    origin: "chat.deepseek.com",
    loginUrl: "https://chat.deepseek.com/",
    cookieDomains: ["deepseek.com"],
    // маркеры «уже вошёл» — строже, чем просто куки домена
    loginMarkers: ["ds_session_id", "userToken"],
    storageKeys: ["userToken"],
  },
  qwen: {
    label: "Qwen",
    origin: "chat.qwen.ai",
    loginUrl: "https://chat.qwen.ai/",
    cookieDomains: ["qwen.ai", "aliyun.com", "alibaba.com"],
    loginMarkers: ["token", "tongyi_sso_ticket", "login_aliyunid_ticket"],
    storageKeys: ["token", "userToken"],
  },
};
const AUTH_FLOW_MAX_MS = 15 * 60 * 1000;   // 15 минут на вход

/* Читает localStorage на странице провайдера (функция сериализуется!) */
function pageReadStorage(keys) {
  const out = {};
  for (const k of keys || []) {
    try {
      const v = localStorage.getItem(k);
      if (v) out[k] = v;
    } catch (e) { /* приватный режим — пропускаем */ }
  }
  return out;
}

async function authCookieMap(providerId) {
  const p = WEB_AUTH[providerId];
  if (!p || !apiX.cookies || !apiX.cookies.getAll) return {};
  const map = {};
  for (const domain of p.cookieDomains) {
    let list = [];
    try {
      list = await apiX.cookies.getAll({ domain });
    } catch (e) { continue; }
    for (const c of list || []) {
      if (c && c.name && c.value && !(c.name in map)) {
        map[c.name] = {
          name: c.name,
          value: c.value,
          domain: c.domain || "",
          path: c.path || "",
          expirationDate: c.expirationDate || null,
        };
      }
    }
  }
  return map;
}

async function authLoginMarkerPresent(providerId) {
  const p = WEB_AUTH[providerId];
  if (!p) return false;
  const map = await authCookieMap(providerId);
  return p.loginMarkers.some((n) => n in map);
}

async function authFindProviderTab(providerId) {
  const p = WEB_AUTH[providerId];
  if (!p) return null;
  try {
    const tabs = await apiX.tabs.query({});
    return tabs.find((t) =>
      String(t.url || "").toLowerCase().includes(p.origin)) || null;
  } catch (e) { return null; }
}

/* Собрать куки + localStorage и отправить на сервер. Возвращает
   {ok, saved_env?} или {ok:false, error}. */
async function authCollect(providerId, { notifyOk = true } = {}) {
  const p = WEB_AUTH[providerId];
  if (!p) return { ok: false, error: "неизвестный провайдер " + providerId };
  const cookies = Object.values(await authCookieMap(providerId));
  if (!cookies.length) {
    return { ok: false, error: "куки домена " + p.origin +
      " не найдены — откройте страницу входа и войдите" };
  }
  let storage = {};
  const tab = await authFindProviderTab(providerId);
  if (tab && apiX.scripting && apiX.scripting.executeScript) {
    try {
      const [res] = await apiX.scripting.executeScript({
        target: { tabId: tab.id },
        func: pageReadStorage,
        args: [p.storageKeys],
      });
      storage = (res && res.result) || {};
    } catch (e) { /* вкладка могла закрыться — обойдёмся куками */ }
  }
  const r = await apiFetch("/api/bridge/cookies", {
    method: "POST",
    body: {
      provider: providerId,
      url: (tab && tab.url) || p.loginUrl,
      cookies,
      storage,
    },
  }, 15000);
  if (notifyOk) {
    notify("🍪 Куки " + p.label + " сохранены",
      "в .env: " + ((r && r.saved_env) || []).join(", "));
  }
  await apiX.storage.local.remove("authFlow");
  return { ok: true, saved_env: (r && r.saved_env) || [] };
}

async function authOpen(providerId) {
  const p = WEB_AUTH[providerId];
  if (!p) return { ok: false, error: "неизвестный провайдер" };
  await loadCfg();
  await apiX.tabs.create({ url: p.loginUrl });
  await apiX.storage.local.set({
    authFlow: { provider: providerId, ts: Date.now() },
  });
  await apiX.alarms.create("bridge-auth", { periodInMinutes: 0.5 });
  notify("Вход в " + p.label,
    "Войдите в аккаунт — куки заберутся автоматически");
  return { ok: true };
}

/* Проверка pending-потока: маркер появился → собираем; 15 мин → сброс. */
async function authCheckPending() {
  const st = await apiX.storage.local.get("authFlow");
  const flow = st && st.authFlow;
  if (!flow || !flow.provider) return;
  if (Date.now() - Number(flow.ts || 0) > AUTH_FLOW_MAX_MS) {
    await apiX.storage.local.remove("authFlow");
    notify("Авторизация", "Ожидание входа истекло — нажмите кнопку ещё раз");
    return;
  }
  try {
    if (await authLoginMarkerPresent(flow.provider)) {
      await authCollect(flow.provider);
    }
  } catch (e) {
    // сеть/сервер не готовы — попробуем на следующем тике alarm
  }
}

/* Сообщения popup → фоновая логика (состояние authFlow консистентнее
   держать в одном месте — в service worker/event page). */
if (apiX.runtime && apiX.runtime.onMessage) {
  apiX.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    (async () => {
      const t = (msg && msg.type) || "";
      try {
        if (t === "auth_open") {
          sendResponse(await authOpen(msg.provider));
        } else if (t === "auth_collect") {
          sendResponse(await authCollect(msg.provider, { notifyOk: false }));
        } else if (t === "auth_status") {
          const st = await apiX.storage.local.get("authFlow");
          sendResponse({ ok: true, authFlow: (st && st.authFlow) || null });
        } else {
          sendResponse({ ok: false, error: "неизвестное сообщение: " + t });
        }
      } catch (e) {
        sendResponse({ ok: false, error: String((e && e.message) || e) });
      }
    })();
    return true;   // ответ будет асинхронным
  });
}

/* ── Контекстное меню ───────────────────────────────────── */
const MENU_CAP_SEL = "bridge-capture-selection";
const MENU_CAP_PAGE = "bridge-capture-page";
const MENU_ASK_SEL = "bridge-ask-selection";
const MENU_ASK_PAGE = "bridge-ask-page";
const MENU_SUM_PAGE = "bridge-summarize-page";

function setupMenus() {
  if (!apiX.contextMenus) return;
  apiX.contextMenus.removeAll(() => {
    apiX.contextMenus.create({
      id: MENU_CAP_SEL, title: "Захватить выделенное в LLM Agent",
      contexts: ["selection"],
    });
    apiX.contextMenus.create({
      id: MENU_ASK_SEL, title: "Спросить агента про выделенное",
      contexts: ["selection"],
    });
    apiX.contextMenus.create({
      id: MENU_CAP_PAGE, title: "Захватить страницу в LLM Agent",
      contexts: ["page"],
    });
    apiX.contextMenus.create({
      id: MENU_ASK_PAGE, title: "Спросить агента про страницу",
      contexts: ["page"],
    });
    apiX.contextMenus.create({
      id: MENU_SUM_PAGE, title: "Суммаризировать страницу в LLM Agent",
      contexts: ["page"],
    });
  });
}

async function doCapture(tab, selectionOnly) {
  let data = null;
  try {
    if (selectionOnly) {
      const [res] = await apiX.scripting.executeScript({
        target: { tabId: tab.id }, func: pageExtractSelection,
      });
      const sel = (res && res.result) || "";
      if (!sel) { notify("Захват", "Нет выделенного текста"); return; }
      data = { title: tab.title || "", url: tab.url || "",
               text: "", selection: sel };
    } else {
      const [res] = await apiX.scripting.executeScript({
        target: { tabId: tab.id }, func: pageExtractArticle,
        args: [40000],
      });
      data = (res && res.result) || null;
      if (data) data.tab_id = tab.id;
      data = { title: (data && data.title) || tab.title || "",
               url: (data && data.url) || tab.url || "",
               text: (data && data.text) || "",
               selection: (data && data.selection) || "",
               tab_id: tab.id };
    }
    const r = await apiFetch("/api/bridge/capture",
      { method: "POST", body: data }, 15000);
    notify("Захват сохранён" + (selectionOnly ? " (выделение)" : ""),
      (data.title || data.url) + " · #" + (r && r.id));
  } catch (e) {
    notify("Захват не удался", String(e.message || e));
  }
}

async function doAsk(tab, selectionOnly) {
  let context = "";
  try {
    if (selectionOnly) {
      const [res] = await apiX.scripting.executeScript({
        target: { tabId: tab.id }, func: pageExtractSelection,
      });
      context = (res && res.result) || "";
      if (!context) { notify("Спросить", "Нет выделенного текста"); return; }
      context = "Выделенный фрагмент со страницы «" + (tab.title || "") +
        "» (" + (tab.url || "") + "):\n\n" + context.slice(0, 4000);
    } else {
      const [res] = await apiX.scripting.executeScript({
        target: { tabId: tab.id }, func: pageExtractArticle,
        args: [12000],
      });
      const d = (res && res.result) || {};
      context = "Содержимое страницы «" + (d.title || tab.title || "") +
        "» (" + (d.url || tab.url || "") + "):\n\n" +
        (d.text || "").slice(0, 12000);
    }
  } catch (e) {
    notify("Не удалось прочитать страницу", String(e.message || e));
    return;
  }
  await apiX.storage.local.set({
    pendingAsk: { query: "Проанализируй: " + context.slice(0, 16000),
                  ts: Date.now() },
  });
  await openPanel();
}

/* Суммаризация страницы: полный текст уходит в захваты, панель чата
   получает готовую задачу с началом текста и ссылкой на захват —
   агент при необходимости дочитает через bridge_get_capture. */
async function doSummarize(tab) {
  let data = null;
  try {
    const [res] = await apiX.scripting.executeScript({
      target: { tabId: tab.id }, func: pageExtractArticle,
      args: [40000],
    });
    data = (res && res.result) || null;
  } catch (e) {
    notify("Суммаризация не удалась", String(e.message || e));
    return;
  }
  if (!data || !data.text) {
    notify("Суммаризация", "страница не дала текста (about: / pdf?)");
    return;
  }
  let capId = null;
  try {
    const r = await apiFetch("/api/bridge/capture", { method: "POST",
      body: { title: data.title || tab.title || "",
              url: data.url || tab.url || "", text: data.text,
              selection: data.selection || "", tab_id: tab.id } },
      15000);
    capId = (r && r.id) || null;
  } catch (e) { /* без захвата — суммаризуем по цитате */ }
  const head = (data.text || "").slice(0, 3500);
  const q = "Суммаризируй страницу «" + (data.title || tab.title || "") +
    "» (" + (data.url || tab.url || "") + ").\n\n" +
    "Начало текста:\n" + head +
    (capId ? ("\n\nПолный текст (" + data.text.length + " симв.) сохранён " +
      "в захвате моста #" + capId + " — при необходимости дочитай " +
      "его инструментом bridge_get_capture.") : "");
  await apiX.storage.local.set({
    pendingAsk: { query: q.slice(0, 16000), ts: Date.now() },
  });
  notify("Суммаризация запущена", data.title || data.url);
  await openPanel();
}

/* Открыть панель чата: нативный side panel/sidebar, иначе вкладка. */
async function openPanel() {
  // Chromium: sidePanel требует user gesture — на клик меню его даёт.
  try {
    if (apiX.sidePanel && apiX.sidePanel.open) {
      const win = await apiX.windows.getCurrent();
      await apiX.sidePanel.open({ windowId: win.id });
      return;
    }
  } catch (e) { /* фолбэк на вкладку */ }
  try {
    if (apiX.sidebarAction && apiX.sidebarAction.open) {
      await apiX.sidebarAction.open();
      return;
    }
  } catch (e) { /* фолбэк на вкладку */ }
  const url = apiX.runtime.getURL("panel/panel.html");
  try { await apiX.tabs.create({ url }); } catch (e) { /* уже открыто */ }
}

/* ── Omnibox: ag <запрос> ───────────────────────────────── */
if (apiX.omnibox) {
  apiX.omnibox.setDefaultSuggestion({
    description: "Спросить LLM Agent: %s",
  });
  apiX.omnibox.onInputEntered.addListener(async (text) => {
    const q = (text || "").trim();
    if (!q) return;
    await loadCfg();
    await apiX.storage.local.set({ pendingAsk: { query: q,
                                                 ts: Date.now() } });
    await openPanel();
  });
}

/* ── Горячая клавиша: «разбудить агента» (wake-agent) ───── */
/* Панель уже открыта → живое сообщение bridge_wake (фокус+префилл);
   закрыта → панель возьмёт pendingWake из storage при загрузке. */
if (apiX.commands && apiX.commands.onCommand) {
  apiX.commands.onCommand.addListener(async (cmd) => {
    if (cmd !== "wake-agent") return;
    await loadCfg();
    setBadge("AG", "#5b9cf5");
    let prefill = "";
    try {
      const [tab] = await apiX.tabs.query({ active: true,
                                            currentWindow: true });
      if (tab && /^https?:/i.test(tab.url || "")) {
        const [res] = await apiX.scripting.executeScript({
          target: { tabId: tab.id }, func: pageExtractSelection,
        });
        prefill = String((res && res.result) || "").slice(0, 2000);
      }
    } catch (e) { /* страница недоступна — просто разбудим */ }
    await apiX.storage.local.set({
      pendingWake: { prefill, ts: Date.now() } });
    await openPanel();
    try {
      await apiX.runtime.sendMessage({ type: "bridge_wake", prefill });
    } catch (e) { /* приёмника нет — сработает pendingWake */ }
    setTimeout(() => { loadCfg().then(() =>
      bridgeCfg.enabled ? badgeOk() : badgeOff()); }, 1500);
  });
}

/* ── Обработчики событий браузера ───────────────────────── */
if (apiX.contextMenus && apiX.contextMenus.onClicked) {
  apiX.contextMenus.onClicked.addListener((info, tab) => {
    if (!tab) return;
    switch (info.menuItemId) {
      case MENU_CAP_SEL:  doCapture(tab, true);  break;
      case MENU_CAP_PAGE: doCapture(tab, false); break;
      case MENU_ASK_SEL:  doAsk(tab, true);      break;
      case MENU_ASK_PAGE: doAsk(tab, false);     break;
      case MENU_SUM_PAGE: doSummarize(tab);      break;
    }
  });
}

if (apiX.tabs) {
  apiX.tabs.onUpdated.addListener((id, ch, tab) => {
    if (ch.status === "complete" && /^https?:/i.test(tab.url || "")) {
      scheduleRegisterTabs();
      // авто-детект входа: загрузилась страница провайдера при pending
      authCheckPending();
    }
  });
  apiX.tabs.onActivated.addListener(() => scheduleRegisterTabs(400));
  apiX.tabs.onRemoved.addListener(() => scheduleRegisterTabs(400));
}

apiX.runtime.onInstalled.addListener(() => {
  setupMenus();
  apiX.alarms.create("bridge-pull", { periodInMinutes: 0.5 });
  registerTabs();
});
apiX.runtime.onStartup.addListener(() => {
  setupMenus();
  apiX.alarms.create("bridge-pull", { periodInMinutes: 0.5 });
  ensureLoop();
});

if (apiX.alarms) {
  apiX.alarms.onAlarm.addListener(al => {
    if (al.name === "bridge-pull") ensureLoop();
    if (al.name === "bridge-auth") authCheckPending();
  });
}

/* Старт (service worker проснулся — запускаем всё). */
setupMenus();
ensureLoop();
registerTabs().then(n => { if (n >= 0) badgeOk(); else badgeOff(); });
