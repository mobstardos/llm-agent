/* Кластер — вкладка фичи: мультиинстанс-аналитика (/api/cluster/*).
   Реестр инстансов (активные/отсутствующие, метрики), нагрузка по зеркалам
   за окно, топ инструментов/агентов, чистка устаревших записей. */
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
    var init = { method: opts.method || "GET" };
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
    if (!modal || $("#cluster-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🌐 Кластер");
    btn.id = "cluster-tab-btn";
    btn.setAttribute("data-tab", "cluster");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "cluster");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="clu-refresh" class="secondary">🔄 Обновить</button>' +
        '<select id="clu-hours">' +
          '<option value="24">24 часа</option>' +
          '<option value="72">3 дня</option>' +
          '<option value="168">7 дней</option>' +
        "</select>" +
        '<span id="clu-status" class="summary">—</span>' +
      "</div>" +
      '<div id="clu-body" class="list"></div>';
    body.appendChild(panel);

    $("#clu-refresh").onclick = render;
    $("#clu-hours").onchange = render;

    btn.addEventListener("click", function once() {
      btn.removeEventListener("click", once);
      render();
    });
  }

  function setStatus(text) {
    var s = $("#clu-status");
    if (s) s.textContent = text;
  }

  function infoRow(text) {
    return el("div", "summary", text);
  }

  function ago(iso) {
    if (!iso) return "—";
    var t = new Date(iso).getTime();
    if (!t) return "—";
    var s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (s < 90) return s + " с назад";
    if (s < 5400) return Math.round(s / 60) + " мин назад";
    if (s < 172800) return Math.round(s / 3600) + " ч назад";
    return Math.round(s / 86400) + " дн назад";
  }

  function instCard(inst, active) {
    var card = el("div", "agent-card");
    var m = inst.metrics || {};
    card.appendChild(el("div", "agent-name",
      (active ? "🟢 " : "⚪ ") + inst.instance_id +
      "  ·  " + (inst.role || "agent")));
    card.appendChild(el("div", "agent-desc",
      "🖥 " + (inst.hostname || "?") + " · pid " + (inst.pid || "?") +
      (inst.version ? " · v" + inst.version : "") +
      " · heartbeat " + ago(inst.last_heartbeat)));
    var mk = Object.keys(m);
    if (mk.length) {
      card.appendChild(el("div", "summary", mk.map(function (k) {
        return k + ": " + m[k];
      }).join(" · ")));
    }
    return card;
  }

  function metricCards(a, body) {
    var grid = el("div", "panel-actions");
    [["События", a.events_total], ["Tool-calls", a.tool_calls_total],
     ["Сессии", a.sessions_total], ["Планы", a.plans_total]]
      .forEach(function (kv) {
        var c = el("div", "agent-card");
        c.appendChild(el("div", "agent-name", String(kv[1] == null
          ? "—" : kv[1])));
        c.appendChild(el("div", "summary", kv[0] + " за " + a.hours + " ч"));
        grid.appendChild(c);
      });
    body.appendChild(grid);
  }

  function render() {
    var body = $("#clu-body");
    if (!body) return;
    body.innerHTML = "";
    var hours = $("#clu-hours") ? $("#clu-hours").value : "24";

    api("/api/cluster/instances").then(function (d) {
      body.appendChild(infoRow("Инстансов: активных " + d.active_count +
        " · отсутствующих " + d.stale_count));
      d.active.forEach(function (i) { body.appendChild(instCard(i, true)); });
      d.stale.forEach(function (i) { body.appendChild(instCard(i, false)); });
      if (!d.active_count && !d.stale_count) {
        body.appendChild(infoRow(
          "Реестр пуст — ни один инстанс ещё не зарегистрировался " +
          "(нужен доступный PostgreSQL, схема ops.instances)"));
      }
      var cleanup = el("button", "secondary",
        "🧹 Вычистить записи старше 6 часов");
      cleanup.onclick = function () {
        api("/api/cluster/cleanup", { method: "POST" }).then(function (r) {
          setStatus("✅ удалено записей: " + r.removed);
          render();
        }).catch(function (e) { setStatus("❌ " + e.message, true); });
      };
      body.appendChild(cleanup);

      return api("/api/cluster/analytics?hours=" + hours);
    }).then(function (a) {
      if (!a) return;
      body.appendChild(el("div", "agent-name",
        "Нагрузка за " + a.hours + " ч"));
      metricCards(a, body);

      if ((a.hourly || []).length) {
        var maxEv = 1;
        a.hourly.forEach(function (h) {
          maxEv = Math.max(maxEv, h.events || 0);
        });
        var card = el("div", "agent-card");
        card.appendChild(el("div", "agent-name", "Почасовая нагрузка"));
        a.hourly.slice(-24).forEach(function (h) {
          var bar = Math.max(1, Math.round((h.events / maxEv) * 30));
          card.appendChild(el("div", "summary",
            h.hour + "  " + "█".repeat(bar) + " " + h.events +
            " (tools: " + h.tool_calls + ")"));
        });
        body.appendChild(card);
      }

      function topList(title, rows, key) {
        if (!(rows || []).length) return;
        var card = el("div", "agent-card");
        card.appendChild(el("div", "agent-name", title));
        rows.forEach(function (r) {
          card.appendChild(el("div", "summary",
            (r[key] || "?") + " — " + r.count));
        });
        body.appendChild(card);
      }
      topList("Топ инструментов", a.top_tools, "tool");
      topList("Топ агентов", a.top_agents, "agent");
      if (a.mirror_error) {
        body.appendChild(infoRow("⚠ Зеркала: " + a.mirror_error));
      }
    }).catch(function (e) {
      body.innerHTML = "";
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectTab);
  } else {
    injectTab();
  }
})();
