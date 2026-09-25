/* Мост (браузер) — вкладка фичи.
   Самодостаточная вкладка в настройках: статус связи с расширением,
   открытые вкладки, захваты (поиск/просмотр/удаление), задачи.
   Подключается через реестр: GET /api/features → js_url.
   Живые обновления — DOM-событие llm-event (bridge.capture/bridge.job). */
(function () {
  "use strict";
  if (window.__llmBridgeFeatureLoaded) return;
  window.__llmBridgeFeatureLoaded = true;

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function api(path, opts) {
    return fetch(path, {
      method: (opts && opts.method) || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts && opts.body !== undefined
        ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      if (!r.ok) return r.json().then(function (d) {
        throw new Error(d.detail || ("HTTP " + r.status));
      });
      return r.json();
    });
  }
  function esc(s) {
    var d = document.createElement("div");
    d.textContent = s == null ? "" : String(s);
    return d.innerHTML;
  }

  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#bridge-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🌐 Мост");
    btn.id = "bridge-tab-btn";
    btn.setAttribute("data-tab", "bridge");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "bridge");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="bridge-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="bridge-summary" class="summary">—</span>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Состояние моста</h4>' +
        '<div id="bridge-status" class="agent-desc">…</div>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Открытые вкладки браузера</h4>' +
        '<div id="bridge-tabs" class="list"></div>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Прочитать вкладку сейчас</h4>' +
        '<div class="form-row">' +
          '<input id="bridge-read-q" class="input" ' +
          'placeholder="URL содержит… (пусто = активная вкладка)">' +
        "</div>" +
        '<div class="settings-actions">' +
          '<button id="bridge-read" class="primary" type="button">📖 Читать</button>' +
          '<span id="bridge-read-out" class="summary"></span>' +
        "</div>" +
        '<pre id="bridge-read-pre" class="journal-pre" style="display:none;max-height:220px;overflow:auto;white-space:pre-wrap"></pre>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Захваты (страницы и выделения из браузера)</h4>' +
        '<div class="form-row">' +
          '<input id="bridge-cap-q" class="input" placeholder="Поиск по захватам…">' +
        "</div>" +
        '<div class="settings-actions">' +
          '<button id="bridge-cap-find" class="secondary" type="button">🔎 Найти</button>' +
          '<span id="bridge-cap-summary" class="summary">—</span>' +
        "</div>" +
        '<div id="bridge-captures" class="list"></div>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Куки веб-чатов (DeepSeek/Qwen → .env)</h4>' +
        '<div id="bridge-cookies" class="list"></div>' +
        '<div class="agent-desc">Кнопка в popup расширения — открыть ' +
        'страницу входа и забрать куки автоматически. Значения кук не ' +
        'отображаются — только факт настройки.</div>' +
      "</div>" +

      '<div class="settings-section">' +
        '<h4>Задачи (сервер → расширение)</h4>' +
        '<div id="bridge-jobs" class="list"></div>' +
      "</div>";
    body.appendChild(panel);

    // ── Статус + вкладки ────────────────────────────────
    async function loadStatus() {
      try {
        var s = await api("/api/bridge/status");
        var age = s.tabs.age_s;
        $("#bridge-summary").textContent =
          "вкладок: " + s.tabs.count + " · захватов: " + s.captures +
          " · задач в очереди: " + s.jobs.pending;
        $("#bridge-status").innerHTML =
          "расширение: <b>" + esc(s.extension) + "</b>" +
          " (снапшот " + (age > 60 ? Math.round(age / 60) + " мин" :
            Math.round(age) + " с") + " назад)" +
          " · задачи: " + esc(JSON.stringify(s.jobs.statuses || {}));
      } catch (e) {
        $("#bridge-status").textContent = "ошибка: " + e.message;
      }
    }

    async function loadTabs() {
      var box = $("#bridge-tabs");
      try {
        var d = await api("/api/bridge/tabs");
        box.innerHTML = "";
        (d.tabs || []).forEach(function (t) {
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("code", "agent-id", "#" + t.id));
          title.appendChild(el("span", "summary", t.title || "(без названия)"));
          head.appendChild(title);
          if (t.active) head.appendChild(el("span", "summary", " ● активная"));
          row.appendChild(head);
          row.appendChild(el("div", "agent-desc", t.url));
          box.appendChild(row);
        });
        if (!(d.tabs || []).length) {
          box.appendChild(el("div", "agent-desc",
            "нет данных — откройте браузер с установленным расширением"));
        }
      } catch (e) {
        box.textContent = "ошибка: " + e.message;
      }
    }

    // ── Читать вкладку ──────────────────────────────────
    $("#bridge-read").onclick = async function () {
      var out = $("#bridge-read-out");
      var pre = $("#bridge-read-pre");
      out.textContent = "ждём расширение…";
      pre.style.display = "none";
      try {
        var d = await api("/api/bridge/read", {
          method: "POST",
          body: { url_contains: $("#bridge-read-q").value.trim(),
                  wait_s: 60 },
        });
        if (d.ok) {
          var r = d.result || {};
          out.textContent = "OK: " + (r.title || "");
          pre.textContent = (r.url ? r.url + "\n\n" : "") +
            (r.text || "(пусто)");
          pre.style.display = "";
        } else {
          out.textContent = "не удалось: " + (d.error || "?");
        }
      } catch (e) {
        out.textContent = "ошибка: " + e.message;
      }
    };

    // ── Захваты ─────────────────────────────────────────
    async function loadCaptures(q) {
      var box = $("#bridge-captures");
      try {
        var d = await api("/api/bridge/captures?q=" +
          encodeURIComponent(q || "") + "&limit=20");
        $("#bridge-cap-summary").textContent =
          "найдено: " + d.matched + " из " + d.total_all;
        box.innerHTML = "";
        (d.captures || []).forEach(function (c) {
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("code", "agent-id", "#" + c.id));
          title.appendChild(el("span", "summary",
            (c.selection ? "✂️ " : "📄 ") + (c.title || c.url)));
          head.appendChild(title);
          var del = el("button", "secondary danger-text", "✕");
          del.onclick = function () {
            api("/api/bridge/captures/" + c.id,
                { method: "DELETE" }).then(loadCapturesNow);
          };
          head.appendChild(del);
          row.appendChild(head);
          var preview = (c.text || "").slice(0, 220);
          row.appendChild(el("div", "agent-desc",
            new Date((c.ts || 0) * 1000).toLocaleString() +
            " · " + c.url));
          if (preview) row.appendChild(el("div", "agent-desc", preview));
          box.appendChild(row);
        });
        if (!(d.captures || []).length) {
          box.appendChild(el("div", "agent-desc",
            "захватов нет — выделите текст на странице и выберите " +
            "«Захватить в LLM Agent» в контекстном меню"));
        }
      } catch (e) {
        box.textContent = "ошибка: " + e.message;
      }
    }
    function loadCapturesNow() {
      loadCaptures($("#bridge-cap-q").value.trim());
    }
    $("#bridge-cap-find").onclick = loadCapturesNow;
    $("#bridge-cap-q").addEventListener("keydown", function (e) {
      if (e.key === "Enter") loadCapturesNow();
    });

    // ── Задачи ──────────────────────────────────────────
    async function loadJobs() {
      var box = $("#bridge-jobs");
      try {
        var d = await api("/api/bridge/jobs?limit=15");
        box.innerHTML = "";
        (d.jobs || []).forEach(function (j) {
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("code", "agent-id", "#" + j.id));
          title.appendChild(el("span", "summary",
            j.kind + " → " + j.status +
            (j.requeues ? " (requeue×" + j.requeues + ")" : "")));
          head.appendChild(title);
          row.appendChild(head);
          row.appendChild(el("div", "agent-desc",
            new Date((j.created_at || 0) * 1000).toLocaleString() +
            " · источник: " + (j.source || "?") +
            (j.error ? " · ошибка: " + j.error : "")));
          box.appendChild(row);
        });
        if (!(d.jobs || []).length) {
          box.appendChild(el("div", "agent-desc", "задач пока не было"));
        }
      } catch (e) {
        box.textContent = "ошибка: " + e.message;
      }
    }

    // ── Куки веб-чатов (Task 24-a) ──────────────────────
    async function loadCookies() {
      var box = $("#bridge-cookies");
      if (!box) return;
      try {
        var d = await api("/api/bridge/cookies/status");
        box.innerHTML = "";
        Object.keys(d.providers || {}).forEach(function (pid) {
          var p = d.providers[pid];
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("span", "summary",
            (p.configured ? "✅ " : "⬜ ") + p.label));
          head.appendChild(title);
          var del = el("button", "secondary danger-text", "✕");
          del.title = "затереть переменные в .env и окружении";
          del.onclick = function () {
            api("/api/bridge/cookies/" + pid, { method: "DELETE" })
              .then(loadCookies);
          };
          head.appendChild(del);
          row.appendChild(head);
          var age = p.age_min == null ? "не настраивался"
            : p.age_min < 90
              ? Math.round(p.age_min) + " мин назад"
              : Math.round(p.age_min / 60) + " ч назад";
          row.appendChild(el("div", "agent-desc",
            (p.configured ? "ключи: " + (p.env_present || []).join(", ")
                          : "нет в .env") +
            " · обновлён: " + age +
            " · вход: " + p.login_url));
          box.appendChild(row);
        });
      } catch (e) {
        box.textContent = "ошибка: " + e.message;
      }
    }

    function loadAll() {
      loadStatus(); loadTabs(); loadCapturesNow(); loadJobs();
      loadCookies();
    }
    $("#bridge-refresh").onclick = loadAll;

    // Живые обновления: шина событий сервера приходит в чат как
    // {"type":"event","kind":"bridge.capture"} — app.js раздаёт её
    // как DOM-событие llm-event.
    window.addEventListener("llm-event", function (ev) {
      var kind = ev.detail && ev.detail.kind;
      if (kind === "bridge.capture") loadCapturesNow();
      if (kind === "bridge.job") { loadStatus(); loadJobs(); }
      if (kind === "bridge.cookies") loadCookies();
    });

    loadAll();
  }

  // Модалка настроек создаётся лениво — вешаемся на открытие
  function boot() {
    injectTab();
    var mo = new MutationObserver(function () { injectTab(); });
    mo.observe(document.body, { childList: true, subtree: true });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
