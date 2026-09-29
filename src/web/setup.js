/* ═══════════════════════════════════════════════════════════
   Быстрая настройка модулей — вкладка «Настройка», диалог
   настройки (env → .env → запуск) и бейдж на кнопке ⚙.

   Самодостаточный модуль в духе journal.js: app.js почти не
   трогаем (только вызов SetupCenter.load() в onTabOpen).
   Подключение:
     <script src="/static/app.js"></script>
     <script src="/static/setup.js"></script>
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  /* ── Helpers ─────────────────────────────────────────── */
  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }
  function api(path, opts) {
    opts = opts || {};
    return fetch(path, {
      method: opts.method || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) {
        if (!r.ok) throw new Error(d.detail || ("HTTP " + r.status));
        return d;
      });
    });
  }

  var SECRET_RE = /TOKEN|SECRET|KEY|PASSWORD|PASS|COOKIE|CREDENTIAL|DSN/i;
  var BADGE_AGENT = {
    active: ["badge-green", "активен"],
    degraded: ["badge-yellow", "деградировал"],
    unavailable: ["badge-red", "недоступен"],
    disabled: ["badge-gray", "отключён"],
    failed: ["badge-red", "ошибка"],
  };

  function isSecret(name) { return SECRET_RE.test(String(name)); }

  /* ═══════════════════════════════════════════════════════
     Диалог настройки одного модуля (MCP или агент)
     ═══════════════════════════════════════════════════════ */
  var SetupDialog = {
    overlay: null,
    current: null, // {kind, id, opts}

    ensureOverlay: function () {
      if (this.overlay) return this.overlay;
      var self = this;
      var ov = el("div", "modal hidden");
      ov.id = "setup-dialog";
      ov.innerHTML =
        '<div class="modal-content setup-dialog">' +
        '  <h3 id="setup-dlg-title">⚙️ Настройка</h3>' +
        '  <div id="setup-dlg-state" class="setup-dlg-state"></div>' +
        '  <div id="setup-dlg-body"></div>' +
        '  <div id="setup-dlg-result" class="setup-dlg-result hidden"></div>' +
        '  <div class="settings-actions" style="margin-top:14px">' +
        '    <button id="setup-dlg-save" class="primary" type="button">💾 Сохранить</button>' +
        '    <button id="setup-dlg-close" class="secondary" type="button">Закрыть</button>' +
        '  </div>' +
        '</div>';
      document.body.appendChild(ov);
      ov.addEventListener("click", function (e) {
        if (e.target === ov) self.close();
      });
      document.addEventListener("keydown", function (e) {
        if (e.key === "Escape" && !ov.classList.contains("hidden")) self.close();
      });
      $("#setup-dlg-close", ov).onclick = function () { self.close(); };
      this.overlay = ov;
      return ov;
    },

    close: function () {
      if (this.overlay) this.overlay.classList.add("hidden");
      this.current = null;
      refreshBadge();
    },

    open: function (kind, id, opts) {
      var self = this;
      opts = opts || {};
      this.current = { kind: kind, id: id, opts: opts };
      var ov = this.ensureOverlay();
      ov.classList.remove("hidden");
      var body = $("#setup-dlg-body", ov);
      var result = $("#setup-dlg-result", ov);
      var saveBtn = $("#setup-dlg-save", ov);
      $("#setup-dlg-title", ov).textContent =
        (kind === "mcp" ? "⚙️ Настройка MCP: " : "⚙️ Настройка агента: ") + id;
      $("#setup-dlg-state", ov).textContent = "Загрузка…";
      body.innerHTML = "";
      result.classList.add("hidden");
      saveBtn.disabled = true;

      var url = kind === "mcp"
        ? "/api/registry/mcp/" + encodeURIComponent(id) + "/requirements"
        : "/api/registry/agents/" + encodeURIComponent(id) + "/requirements";

      api(url).then(function (rep) {
        self.render(rep);
      }).catch(function (e) {
        $("#setup-dlg-state", ov).textContent = "Ошибка: " + e.message;
      });
    },

    render: function (rep) {
      var self = this;
      var ov = this.overlay;
      var kind = this.current.kind;
      var body = $("#setup-dlg-body", ov);
      var saveBtn = $("#setup-dlg-save", ov);
      var state = $("#setup-dlg-state", ov);

      /* — строка состояния — */
      var stateParts = [];
      if (kind === "mcp") {
        stateParts.push(rep.alive ? "🟢 запущен" : "⚪ не запущен");
        stateParts.push(rep.state === "enabled" ? "включён" : "выключен");
      } else {
        var b = BADGE_AGENT[rep.state] || ["badge-gray", rep.state];
        stateParts.push(b[1]);
      }
      if (rep.status === "ok") stateParts.push("требования выполнены ✓");
      else if (rep.status === "not_configured")
        stateParts.push("не хватает обязательного: " + rep.missing_hard.length);
      else stateParts.push("необязательное не выполнено: " + rep.missing_soft.length);
      state.textContent = stateParts.join(" · ");

      body.innerHTML = "";
      var items = rep.items || [];
      var hardItems = items.filter(function (i) { return i.level === "hard"; });
      var softItems = items.filter(function (i) { return i.level === "soft"; });
      var envInputs = {}; // name -> input (без дублей; path.env_ref может совпасть)

      function addEnvInput(container, name, hint) {
        if (envInputs[name]) return;
        var row = el("div", "setup-field");
        var label = el("label", null, name);
        label.title = "Сохранится в .env";
        var inp = el("input", "input");
        inp.type = isSecret(name) ? "password" : "text";
        inp.dataset.env = name;
        inp.placeholder = hint || "значение";
        inp.autocomplete = "off";
        row.appendChild(label);
        row.appendChild(inp);
        container.appendChild(row);
        envInputs[name] = inp;
      }

      function addStaticRow(container, icon, text, cls) {
        var row = el("div", "setup-static " + (cls || ""));
        row.appendChild(el("span", null, icon + " " + text));
        container.appendChild(row);
      }

      var hardBox = el("div", "setup-section");
      var hardTitle = el("h4", null, "Обязательное");
      hardBox.appendChild(hardTitle);
      var hardHas = false;
      hardItems.forEach(function (i) {
        if (i.kind === "env" && i.missing) {
          addEnvInput(hardBox, i.name, i.message);
          hardHas = true;
        } else if (i.kind === "package" && i.missing) {
          addStaticRow(hardBox, "📦", i.message + (i.hint ? " — " + i.hint : ""), "setup-warn");
          hardHas = true;
        } else if (i.kind === "path" && i.missing) {
          addStaticRow(hardBox, "📁", i.message || ("Путь недоступен: " + i.name), "setup-warn");
          if (i.env_ref) addEnvInput(hardBox, i.env_ref, "путь для " + i.name);
          hardHas = true;
        } else if (i.kind === "external" && i.missing) {
          addStaticRow(hardBox, "🌐", i.message || ("Недоступен: " + i.name), "setup-warn");
          hardHas = true;
        }
      });
      if (!hardHas) hardBox.appendChild(el("div", "setup-ok-line", "✓ Всё обязательное на месте"));
      body.appendChild(hardBox);

      if (softItems.length) {
        var softBox = el("div", "setup-section setup-soft");
        softBox.appendChild(el("h4", null, "Необязательное"));
        var softHas = false;
        softItems.forEach(function (i) {
          if (i.missing) {
            if (i.kind === "env" || (i.kind === "path" && i.env_ref)) {
              addEnvInput(softBox, i.kind === "env" ? i.name : i.env_ref, i.message);
            } else {
              addStaticRow(softBox, i.kind === "package" ? "📦" : "•",
                i.message || i.name, "setup-soft-row");
            }
            softHas = true;
          }
        });
        if (softHas) {
          softBox.appendChild(el("div", "setup-note",
            "Не блокирует запуск — можно заполнить или пропустить."));
          body.appendChild(softBox);
        }
      }

      var okNames = items.filter(function (i) { return !i.missing; })
        .map(function (i) { return i.name; });
      if (okNames.length) {
        body.appendChild(el("div", "setup-ok-line",
          "✓ Уже в порядке: " + okNames.slice(0, 8).join(", ") +
          (okNames.length > 8 ? " …" : "")));
      }

      /* — кнопка действия — */
      if (kind === "mcp") {
        saveBtn.textContent = Object.keys(envInputs).length
          ? "💾 Сохранить и запустить" : "▶ Запустить";
      } else {
        saveBtn.textContent = "💾 Сохранить";
      }
      saveBtn.disabled = false;
      saveBtn.onclick = function () { self.save(envInputs); };

      this._rep = rep;
    },

    save: function (envInputs) {
      var self = this;
      var cur = this.current;
      if (!cur) return;
      var saveBtn = $("#setup-dlg-save", this.overlay);
      var result = $("#setup-dlg-result", this.overlay);
      var values = {};
      Object.keys(envInputs).forEach(function (k) {
        var v = envInputs[k].value.trim();
        if (v) values[k] = v;
      });

      var hadValues = Object.keys(values).length > 0;
      if (cur.kind === "agent" && !hadValues) {
        result.classList.remove("hidden");
        result.textContent = "Нечего сохранять — все поля пустые.";
        return;
      }

      saveBtn.disabled = true;
      var oldText = saveBtn.textContent;
      saveBtn.textContent = "⏳ Применяю…";
      result.classList.remove("hidden");
      result.textContent = "Сохраняю…";

      var chain;
      if (cur.kind === "mcp") {
        chain = api("/api/registry/mcp/" + encodeURIComponent(cur.id) + "/config", {
          method: "POST", body: { env: values },
        }).then(function () {
          return api("/api/registry/mcp/" + encodeURIComponent(cur.id) + "/start", {
            method: "POST", body: { restart: hadValues },
          });
        }).then(function (r) {
          if (r.alive) {
            result.textContent = "✓ Запущен" + (hadValues ? " (с новыми настройками)" : "") +
              (r.error ? "" : "");
            result.className = "setup-dlg-result setup-result-ok";
          } else {
            result.textContent = "✗ Не запустился" + (r.error ? ": " + r.error : "");
            result.className = "setup-dlg-result setup-result-bad";
          }
        });
      } else {
        chain = api("/api/registry/agents/" + encodeURIComponent(cur.id) + "/config", {
          method: "POST", body: { env: values },
        }).then(function (r) {
          var a = r.agent || {};
          var st = a.status || "?";
          var reasons = [].concat(a.reasons || [], a.degraded_reasons || []);
          result.textContent = "✓ Сохранено в .env" + (r.path ? " (" + r.path + ")" : "") +
            ". Статус агента: " + st +
            (reasons.length ? " · " + reasons.slice(0, 2).join("; ") : " ✓");
          result.className = "setup-dlg-result " +
            (st === "active" ? "setup-result-ok" : "setup-result-mid");
        });
      }

      chain.then(function () {
        saveBtn.disabled = false;
        saveBtn.textContent = oldText;
        refreshBadge();
        if (cur.opts && cur.opts.onChange) {
          try { cur.opts.onChange(); } catch (e) { /* noop */ }
        }
      }).catch(function (e) {
        result.textContent = "✗ Ошибка: " + e.message;
        result.className = "setup-dlg-result setup-result-bad";
        saveBtn.disabled = false;
        saveBtn.textContent = oldText;
      });
    },
  };

  /* ═══════════════════════════════════════════════════════
     Вкладка «Настройка» (обзор всех модулей)
     ═══════════════════════════════════════════════════════ */
  var SetupCenter = {
    filter: "problems",

    panelHtml: function () {
      return (
        '<div class="panel-actions">' +
        '  <button id="setup-refresh" class="secondary">🔄 Обновить</button>' +
        '  <button id="setup-filter-problems" class="secondary active-filter">Только проблемы</button>' +
        '  <button id="setup-filter-all" class="secondary">Все</button>' +
        '  <span id="setup-summary" class="summary">—</span>' +
        "</div>" +
        '<div class="setup-progress"><div id="setup-progress-bar" class="setup-progress-bar" style="width:0%"></div></div>' +
        '<div id="setup-list" class="list"></div>'
      );
    },

    load: function () {
      var self = this;
      var list = $("#setup-list");
      if (!list) return;
      list.innerHTML = '<div class="summary">Загрузка…</div>';

      Promise.all([
        api("/api/registry/agents").catch(function () { return { agents: [] }; }),
        api("/api/registry/mcp").catch(function () { return { mcp_servers: [] }; }),
      ]).then(function (rs) {
        var agents = rs[0].agents || [];
        var mcps = rs[1].mcp_servers || [];
        self.render(agents, mcps);
      });
    },

    mcpBadge: function (m) {
      var req = m.requirements || {};
      if (m.alive) return ["badge-green", "alive"];
      if (req.status === "not_configured") return ["badge-red", "не настроен"];
      if (req.status === "degraded") return ["badge-yellow", "частично настроен"];
      if (!m.enabled) return ["badge-gray", "выключен"];
      return ["badge-gray", "offline"];
    },

    render: function (agents, mcps) {
      var list = $("#setup-list");
      var summary = $("#setup-summary");
      var bar = $("#setup-progress-bar");
      if (!list) return;

      var rows = [];
      mcps.forEach(function (m) {
        var req = m.requirements || {};
        var problems = (req.missing_hard || []).concat(req.missing_soft || []);
        var isProblem = !m.alive;
        rows.push({
          kind: "mcp", id: m.id, title: m.title || m.id,
          icon: "🔌", badge: this.mcpBadge(m),
          missing: problems, isProblem: isProblem,
          statusOk: m.alive,
        });
      }, this);
      agents.forEach(function (a) {
        var reasons = (a.reasons || []).concat(a.degraded_reasons || []);
        var b = BADGE_AGENT[a.status] || ["badge-gray", a.status];
        rows.push({
          kind: "agent", id: a.id, title: a.title || a.id,
          icon: a.icon || "🤖", badge: b,
          missing: reasons, isProblem: a.status !== "active" && a.status !== "disabled",
          statusOk: a.status === "active",
        });
      });

      var problems = rows.filter(function (r) { return r.isProblem; });
      problems.sort(function (a, b) { return b.missing.length - a.missing.length; });
      var okCount = rows.length - problems.length;

      if (summary) {
        summary.textContent = "Готовы к работе: " + okCount + " из " + rows.length +
          (problems.length ? " · проблемных: " + problems.length : " 🎉");
      }
      if (bar) {
        bar.style.width = rows.length
          ? Math.round((okCount / rows.length) * 100) + "%" : "100%";
        bar.className = "setup-progress-bar" +
          (problems.length ? " setup-progress-warn" : "");
      }

      var shown = this.filter === "problems" ? problems : rows;
      list.innerHTML = "";
      if (!shown.length) {
        list.appendChild(el("div", "summary",
          this.filter === "problems"
            ? "🎉 Проблемных модулей нет — всё настроено и запущено"
            : "Нет модулей"));
        return;
      }

      shown.forEach(function (r) {
        var card = el("div", "agent-card" + (r.isProblem ? " setup-row-problem" : ""));
        var missHtml = r.missing.length
          ? '<div style="color:#fca5a5;font-size:11px;margin-top:6px">' +
            r.missing.slice(0, 3).map(esc).join("<br>") +
            (r.missing.length > 3 ? "<br>… ещё " + (r.missing.length - 3) : "") +
            "</div>"
          : "";
        card.innerHTML =
          '<div class="agent-header">' +
          '  <div class="agent-title">' +
          '    <span>' + esc(r.icon) + "</span>" +
          '    <strong>' + esc(r.title) + "</strong>" +
          '    <code class="agent-id">' + esc(r.id) + "</code>" +
          '    <span class="badge ' + r.badge[0] + '">' + esc(r.badge[1]) + "</span>" +
          '    <span class="badge badge-blue">' + (r.kind === "mcp" ? "MCP" : "агент") + "</span>" +
          "  </div>" +
          "</div>" +
          missHtml +
          '<div class="agent-actions" style="margin-top:8px;display:flex;gap:6px">' +
          '  <button class="secondary" data-setup="' + esc(r.kind) + ":" + esc(r.id) + '">⚙️ Настроить</button>' +
          "</div>";
        list.appendChild(card);
      });

      list.querySelectorAll("[data-setup]").forEach(function (btn) {
        btn.onclick = function () {
          var parts = btn.dataset.setup.split(":");
          var kind = parts[0], id = parts.slice(1).join(":");
          SetupDialog.open(kind, id, { onChange: function () { SetupCenter.load(); } });
        };
      });
    },

    setFilter: function (f) {
      this.filter = f;
      var bp = $("#setup-filter-problems"), ba = $("#setup-filter-all");
      if (bp) bp.classList.toggle("active-filter", f === "problems");
      if (ba) ba.classList.toggle("active-filter", f === "all");
      this.load();
    },
  };

  /* ═══════════════════════════════════════════════════════
     Инжект вкладки, панели и бейджа
     ═══════════════════════════════════════════════════════ */
  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#setup-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🔧 Настройка");
    btn.id = "setup-tab-btn";
    btn.setAttribute("data-tab", "setup");
    tabs.insertBefore(btn, tabs.firstChild);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "setup");
    panel.innerHTML = SetupCenter.panelHtml();
    body.insertBefore(panel, body.firstChild);

    $("#setup-refresh", panel).onclick = function () { SetupCenter.load(); };
    $("#setup-filter-problems", panel).onclick = function () {
      SetupCenter.setFilter("problems");
    };
    $("#setup-filter-all", panel).onclick = function () {
      SetupCenter.setFilter("all");
    };
  }

  function injectBadge() {
    var gear = $("#settings-btn");
    if (!gear || $("#setup-badge")) return;
    var badge = el("span", "setup-badge hidden", "0");
    badge.id = "setup-badge";
    gear.style.position = "relative";
    gear.appendChild(badge);
  }

  function refreshBadge() {
    var badge = $("#setup-badge");
    if (!badge) return;
    api("/api/registry/snapshot").then(function (s) {
      var setup = s && s.setup;
      if (!setup) { badge.classList.add("hidden"); return; }
      var problems = (setup.mcp_not_configured || 0) + (setup.agents_broken || 0);
      badge.textContent = problems > 99 ? "99+" : String(problems);
      badge.classList.toggle("hidden", problems === 0);
    }).catch(function () { badge.classList.add("hidden"); });
  }

  /* ── Инициализация ───────────────────────────────────── */
  function init() {
    injectTab();
    injectBadge();
    refreshBadge();
    // Обновлять бейдж при каждом открытии настроек (кнопка ⚙ уже
    // имеет onclick в app.js — добавляем свой слушатель рядом)
    var gear = $("#settings-btn");
    if (gear) gear.addEventListener("click", refreshBadge);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.SetupDialog = SetupDialog;
  window.SetupCenter = SetupCenter;
})();
