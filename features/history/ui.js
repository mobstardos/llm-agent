/* История — вкладка фичи: чтение зеркал PostgreSQL (/api/ops/*).
   Сессии чата, планы Supervisor, события журнала, поиск, экспорт MD.
   Подключается через реестр: GET /api/features → js_url. */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function api(path) {
    return fetch(path).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          throw new Error(b.detail || ("HTTP " + r.status));
        });
      }
      return r.json();
    });
  }
  function esc(s) {
    return String(s == null ? "" : s);
  }

  var state = { view: "sessions", session: null };

  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#history-tab-btn")) return;
    // Примечание: в index.html есть статичная вкладка «История»
    // (data-tab="history", локальные дампы чатов). Эта фича читает
    // зеркала PostgreSQL — ей даётся отдельный ключ history_pg,
    // чтобы кнопки и панели не дублировались.
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "📜 История (PG)");
    btn.id = "history-tab-btn";
    btn.setAttribute("data-tab", "history_pg");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "history_pg");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="hist-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="hist-status" class="summary">—</span>' +
      "</div>" +
      '<div class="panel-actions" id="hist-nav">' +
        '<button class="secondary" data-hview="sessions">💬 Сессии</button>' +
        '<button class="secondary" data-hview="plans">🧭 Планы</button>' +
        '<button class="secondary" data-hview="events">📓 События</button>' +
        '<button class="secondary" data-hview="search">🔎 Поиск</button>' +
      "</div>" +
      '<div id="hist-body" class="list"></div>';
    body.appendChild(panel);

    $("#hist-refresh").onclick = render;
    Array.prototype.forEach.call(
      panel.querySelectorAll("[data-hview]"), function (b) {
        b.onclick = function () {
          state.view = b.getAttribute("data-hview");
          state.session = null;
          render();
        };
      });

    btn.addEventListener("click", function once() {
      btn.removeEventListener("click", once);
      loadStatus();
      render();
    });
  }

  function setStatus(text, bad) {
    var s = $("#hist-status");
    if (s) s.textContent = text;
  }

  function loadStatus() {
    api("/api/ops/status").then(function (d) {
      if (!d.pg || !d.pg.available) {
        setStatus("❌ PostgreSQL недоступен", true);
        return;
      }
      var c = d.counts || {};
      var parts = [];
      ["sessions", "messages", "plans", "events"].forEach(function (k) {
        if (c[k] !== null && c[k] !== undefined) {
          parts.push(k + ": " + c[k]);
        }
      });
      var fts = (d.fts && (d.fts.chat || d.fts.events)) ? " · FTS вкл" : "";
      setStatus("✅ PG " + (d.pg.dsn || "") + " · " +
        parts.join(" · ") + fts);
    }).catch(function (e) { setStatus("❌ " + e.message, true); });
  }

  function render() {
    var body = $("#hist-body");
    if (!body) return;
    body.innerHTML = "";
    var fn = { sessions: viewSessions, messages: viewMessages,
               plans: viewPlans, events: viewEvents,
               search: viewSearch }[state.view] || viewSessions;
    try { fn(body); } catch (e) { body.appendChild(el("div", "summary", "Ошибка: " + e.message)); }
  }

  function infoRow(text) {
    return el("div", "summary", text);
  }

  /* ── Сессии ─────────────────────────────────────────────── */
  function viewSessions(body) {
    api("/api/ops/sessions?limit=50").then(function (d) {
      if (!(d.items || []).length) {
        body.appendChild(infoRow("Сессий в зеркале пока нет — они появятся после работы в чате (репликатор переносит их каждые ~15 c)."));
        return;
      }
      d.items.forEach(function (s) {
        var row = el("div", "agent-card");
        var head = el("div", "agent-header");
        var title = el("div", "agent-title");
        title.appendChild(el("code", "agent-id", s.session_id));
        title.appendChild(el("span", "summary",
          "сообщений: " + s.message_count));
        head.appendChild(title);
        var open = el("button", "secondary", "Открыть");
        open.onclick = function () {
          state.view = "messages"; state.session = s.session_id; render();
        };
        var exp = el("button", "secondary", "⬇ MD");
        exp.title = "Экспорт сессии в Markdown";
        exp.onclick = function () {
          window.open("/api/ops/sessions/" +
            encodeURIComponent(s.session_id) + "/export", "_blank");
        };
        head.appendChild(open); head.appendChild(exp);
        row.appendChild(head);
        row.appendChild(el("div", "agent-desc",
          (s.last_seen || "") + " · " + esc(s.preview || "")));
        row.onclick = null;
        body.appendChild(row);
      });
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  /* ── Сообщения сессии ───────────────────────────────────── */
  function viewMessages(body) {
    var back = el("button", "secondary", "← к сессиям");
    back.onclick = function () { state.view = "sessions"; render(); };
    body.appendChild(back);
    var sid = state.session || "";
    if (!sid) { state.view = "sessions"; render(); return; }
    body.appendChild(el("code", "agent-id", sid));
    api("/api/ops/sessions/" + encodeURIComponent(sid) +
        "/messages?limit=200").then(function (d) {
      (d.items || []).forEach(function (m) {
        var row = el("div", "agent-card");
        var head = el("div", "agent-header");
        var title = el("div", "agent-title");
        title.appendChild(el("span", "agent-id",
          (m.role || "?") + (m.agent ? " · " + m.agent : "")));
        title.appendChild(el("span", "summary", m.ts || ""));
        head.appendChild(title);
        row.appendChild(head);
        var text = el("div", "agent-desc");
        text.style.whiteSpace = "pre-wrap";
        text.textContent = m.content || "";
        row.appendChild(text);
        body.appendChild(row);
      });
      if (!(d.items || []).length) {
        body.appendChild(infoRow("Сообщений нет."));
      }
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  /* ── Планы ──────────────────────────────────────────────── */
  function viewPlans(body) {
    api("/api/ops/plans?limit=50").then(function (d) {
      if (!(d.items || []).length) {
        body.appendChild(infoRow("Планов в зеркале пока нет."));
        return;
      }
      d.items.forEach(function (p) {
        var row = el("div", "agent-card");
        var head = el("div", "agent-header");
        var title = el("div", "agent-title");
        title.appendChild(el("code", "agent-id", p.plan_id));
        title.appendChild(el("span", "summary",
          p.status + " · " + p.mode + " · шаги " +
          p.steps_done + "/" + p.steps_total +
          (p.steps_failed ? " (упало " + p.steps_failed + ")" : "")));
        head.appendChild(title);
        row.appendChild(head);
        row.appendChild(el("div", "agent-desc",
          (p.updated_at || "") + " · " + esc(p.intent || "") + " · " +
          esc(p.query || "")));
        var more = el("button", "secondary", "Детали");
        more.onclick = function () {
          api("/api/ops/plans/" + encodeURIComponent(p.plan_id))
            .then(function (full) {
              var pre = el("div", "agent-desc");
              pre.style.whiteSpace = "pre-wrap";
              pre.textContent = JSON.stringify(
                full.payload || full, null, 2).slice(0, 4000);
              row.appendChild(pre);
            }).catch(function (e) { alert(e.message); });
        };
        row.appendChild(more);
        body.appendChild(row);
      });
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  /* ── События журнала ────────────────────────────────────── */
  function viewEvents(body) {
    var filter = el("div", "form-row");
    var inp = el("input", "input");
    inp.placeholder = "tool_name или kind (например file.write)…";
    filter.appendChild(inp);
    body.appendChild(filter);
    var go = el("button", "primary", "Показать");
    function load() {
      var t = inp.value.trim();
      var url = "/api/ops/events?limit=100";
      if (t) {
        url += (t.indexOf(".") > 0 ? "&tool=" : "&kind=") +
          encodeURIComponent(t);
      }
      api(url).then(function (d) {
        body.querySelectorAll(".agent-card, .summary").forEach(
          function (n) { n.remove(); });
        if (!(d.items || []).length) {
          body.appendChild(infoRow("Событий нет."));
          return;
        }
        d.items.forEach(function (ev) {
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("span", "agent-id",
            ev.kind + (ev.tool_name ? " · " + ev.server_name + "/" +
              ev.tool_name : "")));
          title.appendChild(el("span", "summary",
            ev.status + " · " + (ev.duration_ms || 0) + " мс"));
          head.appendChild(title);
          row.appendChild(head);
          row.appendChild(el("div", "agent-desc",
            (ev.ts || "") + " · trace " + (ev.trace_id || "—") +
            (ev.error ? " · ⚠ " + ev.error.slice(0, 120) : "")));
          body.appendChild(row);
        });
      }).catch(function (e) {
        body.appendChild(infoRow("Ошибка: " + e.message));
      });
    }
    go.onclick = load;
    inp.onkeydown = function (e) { if (e.key === "Enter") load(); };
    body.appendChild(go);
    load();
  }

  /* ── Поиск ──────────────────────────────────────────────── */
  function viewSearch(body) {
    var row = el("div", "form-row");
    var inp = el("input", "input");
    inp.placeholder = "Полнотекстовый поиск по перепискам и событиям…";
    row.appendChild(inp);
    body.appendChild(row);
    var go = el("button", "primary", "Найти");
    go.onclick = function () {
      var q = inp.value.trim();
      if (!q) return;
      api("/api/ops/search?limit=30&q=" + encodeURIComponent(q))
        .then(function (d) {
          body.querySelectorAll(".agent-card, .summary").forEach(
            function (n) { n.remove(); });
          body.appendChild(infoRow("Режим: " + d.mode +
            " · чат: " + (d.chat || []).length +
            " · события: " + (d.events || []).length));
          (d.chat || []).forEach(function (hit) {
            var r = el("div", "agent-card");
            var head = el("div", "agent-header");
            var title = el("div", "agent-title");
            title.appendChild(el("span", "agent-id",
              "💬 " + hit.role + " · " + hit.session_id.slice(0, 12)));
            title.appendChild(el("span", "summary", hit.ts || ""));
            head.appendChild(title);
            r.appendChild(head);
            r.appendChild(el("div", "agent-desc", hit.snippet || ""));
            r.style.cursor = "pointer";
            r.onclick = function () {
              state.view = "messages";
              state.session = hit.session_id;
              render();
            };
            body.appendChild(r);
          });
          (d.events || []).forEach(function (ev) {
            var r = el("div", "agent-card");
            r.appendChild(el("div", "agent-desc",
              "📓 " + (ev.ts || "") + " · " + ev.kind + " · " +
              (ev.tool_name || "") +
              (ev.error ? " · ⚠ " + ev.error.slice(0, 100) : "")));
            body.appendChild(r);
          });
        }).catch(function (e) {
          body.appendChild(infoRow("Ошибка: " + e.message));
        });
    };
    inp.onkeydown = function (e) { if (e.key === "Enter") go.onclick(); };
    body.appendChild(go);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectTab);
  } else {
    injectTab();
  }
})();
