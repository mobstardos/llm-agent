/* Эксплуатация — вкладка фичи: бэкапы и ретенция (/api/ops-admin/*).
   Сводка по PG, список бэкапов + создание, ретенция зеркал
   (dry-run по умолчанию, удаление с подтверждением). */
(function () {
  "use strict";

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }
  function api(path, opts) {
    opts = opts || {};
    var init = { method: opts.method || "GET", headers: {} };
    if (opts.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(opts.body);
    }
    return fetch(path, init).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          throw new Error(b.detail || ("HTTP " + r.status));
        });
      }
      return r.json();
    });
  }

  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#opsadm-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🛠 Эксплуатация");
    btn.id = "opsadm-tab-btn";
    btn.setAttribute("data-tab", "opsadmin");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "opsadmin");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="ops-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="ops-status" class="summary">—</span>' +
      "</div>" +
      '<div class="panel-actions" id="ops-nav">' +
        '<button class="secondary" data-oview="backups">💾 Бэкапы</button>' +
        '<button class="secondary" data-oview="retention">🧹 Ретенция</button>' +
      "</div>" +
      '<div id="ops-body" class="list"></div>';
    body.appendChild(panel);

    $("#ops-refresh").onclick = render;
    Array.prototype.forEach.call(
      panel.querySelectorAll("[data-oview]"), function (b) {
        b.onclick = function () {
          state.view = b.getAttribute("data-oview");
          render();
        };
      });

    btn.addEventListener("click", function once() {
      btn.removeEventListener("click", once);
      render();
    });
  }

  var state = { view: "backups" };

  function setStatus(text) {
    var s = $("#ops-status");
    if (s) s.textContent = text;
  }

  function infoRow(text) {
    return el("div", "summary", text);
  }

  function render() {
    var body = $("#ops-body");
    if (!body) return;
    body.innerHTML = "";
    var fn = { backups: viewBackups,
               retention: viewRetention }[state.view] || viewBackups;
    try { fn(body); } catch (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    }
  }

  function viewBackups(body) {
    api("/api/ops-admin/overview").then(function (d) {
      var pg = d.pg || {};
      var card = el("div", "agent-card");
      card.appendChild(el("div", "agent-name",
        pg.available ? "🟢 PostgreSQL доступен" : "⚪ PostgreSQL недоступен"));
      if (pg.version) {
        card.appendChild(el("div", "summary",
          pg.version + (pg.server_time
            ? " · " + pg.server_time.slice(0, 19) : "")));
      }
      card.appendChild(el("div", "summary",
        "Бэкапов: " + (d.backups ? d.backups.count : 0) +
        " · всего МБ: " + (d.backups ? d.backups.total_mb : 0) +
        " · хранить последних: " +
        (d.backups ? d.backups.keep_last : "?")));
      card.appendChild(el("div", "summary",
        "Авто-ретенция зеркал: " +
        (d.retention_days > 0
          ? "вкл (>" + d.retention_days + " дн)"
          : "выкл (PG_RETENTION_DAYS=0)")));
      body.appendChild(card);
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });

    api("/api/ops-admin/backups").then(function (d) {
      var row = el("div", "panel-actions");
      var mk = el("button", "primary", "💾 Создать бэкап сейчас");
      mk.onclick = function () {
        mk.disabled = true;
        setStatus("Делаю pg_dump…");
        api("/api/ops-admin/backups?confirm=true", { method: "POST" })
          .then(function (r) {
            mk.disabled = false;
            setStatus("✅ " + r.path);
            render();
          }).catch(function (e) {
            mk.disabled = false;
            setStatus("❌ " + e.message, true);
          });
      };
      row.appendChild(mk);
      body.appendChild(row);

      if (!(d.items || []).length) {
        body.appendChild(infoRow("Бэкапов пока нет"));
        return;
      }
      d.items.forEach(function (b) {
        var card = el("div", "agent-card");
        card.appendChild(el("div", "agent-name",
          "📦 " + b.name + " · " + b.size_mb + " МБ"));
        card.appendChild(el("div", "summary",
          b.modified + " · " + b.age_hours + " ч назад"));
        body.appendChild(card);
      });
    }).catch(function (e) {
      body.appendChild(infoRow("Бэкапы: " + e.message));
    });
  }

  function viewRetention(body) {
    var row = el("div", "panel-actions");
    var inp = el("input");
    inp.type = "number";
    inp.value = "180";
    inp.min = "1";
    inp.max = "3650";
    inp.style.width = "90px";
    row.appendChild(el("span", "summary", "Старше, дней:"));
    row.appendChild(inp);
    var checkBtn = el("button", "secondary", "🔍 Проверить (dry-run)");
    var runBtn = el("button", "primary", "🧹 Выполнить удаление");
    row.appendChild(checkBtn);
    row.appendChild(runBtn);
    body.appendChild(row);

    var out = el("div");
    body.appendChild(out);

    function fmt(rep) {
      out.innerHTML = "";
      (rep.tables || []).forEach(function (t) {
        var line = t.table + ": всего " + t.total;
        if (t.deletable !== undefined) {
          line += " · удаляемо " + t.deletable;
        }
        if (t.deleted) line += " · удалено " + t.deleted;
        if (t.oldest) line += " · старейшая " + String(t.oldest)
          .slice(0, 19);
        if (t.error) line += " · ⚠ " + t.error;
        out.appendChild(infoRow(line));
      });
      if (rep.reason) out.appendChild(infoRow("⚠ " + rep.reason));
      if (rep.seconds !== undefined) {
        out.appendChild(infoRow("Заняло: " + rep.seconds + " с" +
          (rep.deleted_total !== undefined
            ? " · всего удалено: " + rep.deleted_total : "")));
      }
    }

    checkBtn.onclick = function () {
      var days = parseInt(inp.value, 10) || 0;
      if (days <= 0) { setStatus("Укажите дней > 0", true); return; }
      setStatus("Считаю…");
      api("/api/ops-admin/retention/run", {
        method: "POST",
        body: { days: days, dry_run: true }
      }).then(function (rep) {
        setStatus("✅ dry-run готов");
        fmt(rep);
      }).catch(function (e) { setStatus("❌ " + e.message, true); });
    };

    runBtn.onclick = function () {
      var days = parseInt(inp.value, 10) || 0;
      if (days <= 0) { setStatus("Укажите дней > 0", true); return; }
      if (!window.confirm(
        "Удалить из зеркал ВСЕ строки старше " + days + " дней? " +
        "Это необратимо (локальные файлы останутся).")) return;
      setStatus("Удаляю…");
      api("/api/ops-admin/retention/run", {
        method: "POST",
        body: { days: days, dry_run: false, confirm: true }
      }).then(function (rep) {
        setStatus("✅ удалено: " + rep.deleted_total);
        fmt(rep);
      }).catch(function (e) { setStatus("❌ " + e.message, true); });
    };

    body.appendChild(infoRow(
      "Ретенция чистит зеркала в PG (события, tool-calls, сообщения чата, " +
      "планы, память). Курсоры репликатора не затрагиваются — после " +
      "удаления новые данные продолжают догоняться. Авто-режим: " +
      "PG_RETENTION_DAYS>0 (по умолчанию выключен)."));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectTab);
  } else {
    injectTab();
  }
})();
