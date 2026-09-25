/* ═══════════════════════════════════════════════════════════
   Журнал (чёрный ящик) — вкладка в настройках LLM Agent.

   Самодостаточный модуль: добавляет вкладку «Журнал» в модалку
   настроек сам (app.js не трогаем). Подключение:
     <script src="/static/app.js"></script>
     <script src="/static/journal.js"></script>
   ═══════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  var state = {
    events: [],
    stats: null,
    filters: { kind: "", task: "", search: "", mutating: false },
    selected: null,
  };

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
  function fmtSize(n) {
    if (!n && n !== 0) return "—";
    var u = ["Б", "КБ", "МБ", "ГБ"], i = 0;
    while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + " " + u[i];
  }
  function api(path, opts) {
    return fetch(path, opts).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  /* ── Встраивание вкладки в модалку настроек ────────────── */
  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#journal-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "📓 Журнал");
    btn.id = "journal-tab-btn";
    btn.setAttribute("data-tab", "journal");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "journal");
    panel.innerHTML = panelHtml();
    body.appendChild(panel);

    btn.addEventListener("click", function () {
      // активируем по образцу соседних вкладок
      Array.prototype.forEach.call($(".tab-btn", modal).parentNode.children, function (b) {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      Array.prototype.forEach.call(body.children, function (p) {
        p.classList.remove("active");
      });
      panel.classList.add("active");
      refreshAll();
    });

    bindPanel(panel);
  }

  /* ── Разметка панели ────────────────────────────────────── */
  function panelHtml() {
    return [
      '<div class="panel-actions">',
      '  <button id="jr-refresh" class="secondary">🔄 Обновить</button>',
      '  <button id="jr-sweep" class="secondary">🧹 Ретенция</button>',
      '  <span id="jr-summary" class="summary">—</span>',
      '</div>',
      '<div class="jr-filters">',
      '  <select id="jr-kind">',
      '    <option value="">все виды</option>',
      '    <option value="tool_call">tool_call</option>',
      '    <option value="task">task</option>',
      '    <option value="session">session</option>',
      '    <option value="file_change">file_change</option>',
      '    <option value="rollback">rollback</option>',
      '    <option value="replay">replay</option>',
      '    <option value="system">system</option>',
      '  </select>',
      '  <input id="jr-task" type="text" placeholder="task_id…" />',
      '  <input id="jr-search" type="text" placeholder="поиск…" />',
      '  <label><input id="jr-mutating" type="checkbox" /> только правки</label>',
      '</div>',
      '<div id="jr-list" class="list jr-list"></div>',
      '<div id="jr-detail" class="jr-detail hidden"></div>',
    ].join("");
  }

  /* ── Загрузка данных ────────────────────────────────────── */
  function refreshAll() {
    loadStats();
    loadEvents();
  }

  function loadStats() {
    api("/api/journal/stats").then(function (s) {
      state.stats = s;
      var ret = s.retention_needed ? " ⚠️ ретенция" : " ✓";
      $("#jr-summary").textContent =
        "событий: " + s.total_events +
        " | правок: " + s.mutating_events +
        " | журнал: " + s.journal_size_human +
        " | диск: " + s.disk.free_gb + " ГБ своб." + ret;
    }).catch(function (e) {
      $("#jr-summary").textContent = "журнал недоступен: " + e.message;
    });
  }

  function loadEvents() {
    var f = state.filters;
    var params = new URLSearchParams({ limit: "100" });
    if (f.kind) params.set("kind", f.kind);
    if (f.task) params.set("task_id", f.task);
    if (f.mutating) params.set("mutating", "true");

    var req = f.search
      ? api("/api/journal/search?q=" + encodeURIComponent(f.search) + "&limit=100")
      : api("/api/journal/events?" + params.toString());

    req.then(function (d) {
      state.events = d.events || [];
      renderList();
    }).catch(function (e) {
      $("#jr-list").innerHTML = "<div class='stats'>Ошибка: " + esc(e.message) + "</div>";
    });
  }

  /* ── Список событий ─────────────────────────────────────── */
  function renderList() {
    var box = $("#jr-list");
    box.innerHTML = "";
    if (!state.events.length) {
      box.appendChild(el("div", "stats", "Событий нет (пока). Действия агентов появятся здесь."));
      return;
    }
    var table = el("table", "jr-table");
    table.innerHTML =
      "<thead><tr><th>#</th><th>время</th><th>вид</th><th>действие</th>" +
      "<th>цели</th><th>агент</th><th>статус</th><th></th></tr></thead>";
    var tbody = el("tbody");
    state.events.forEach(function (e) {
      var tr = el("tr");
      var what = e.tool_name ? (e.server_name + "." + e.tool_name) : (e.action || e.kind);
      var st = e.status === "ok" ? "✓" : "⚠ " + e.status;
      tr.innerHTML =
        "<td>" + e.seq + "</td>" +
        "<td>" + esc((e.time || e.iso_time || "").slice(11, 19)) + "</td>" +
        "<td>" + esc(e.kind) + "</td>" +
        "<td>" + esc(what) + "</td>" +
        "<td>" + esc((e.targets || []).slice(0, 2).join(", ")) + "</td>" +
        "<td>" + esc(e.agent || e.agent_id || "—") + "</td>" +
        "<td>" + esc(st) + "</td>" +
        "<td>" + ((e.reversible > 0) ? "<button class='secondary jr-mini' data-ev='" +
          esc(e.event_id) + "'>откат</button>" : "") + "</td>";
      tr.addEventListener("click", function (ev) {
        if (ev.target.classList.contains("jr-mini")) return;
        showDetail(e.event_id);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    box.appendChild(table);

    Array.prototype.forEach.call(box.querySelectorAll(".jr-mini"), function (b) {
      b.addEventListener("click", function (ev) {
        ev.stopPropagation();
        doRollback(b.getAttribute("data-ev"), "dry_run");
      });
    });
  }

  /* ── Детали события ─────────────────────────────────────── */
  function showDetail(eventId) {
    api("/api/journal/event/" + eventId).then(function (e) {
      state.selected = e;
      var d = $("#jr-detail");
      d.classList.remove("hidden");
      var what = e.tool_name ? (e.server_name + "." + e.tool_name) : e.action;
      var html = [
        "<h4>" + esc(what) + " <small>#" + e.seq + " " + esc(e.event_id) + "</small></h4>",
        "<div class='stats'>" +
        "время: " + esc(e.iso_time) +
        " | агент: " + esc(e.agent_id || "—") +
        " | задача: " + esc(e.task_id || "—") +
        " | " + e.duration_ms.toFixed(0) + " мс" +
        " | обратимость: " + e.reversible + "</div>",
      ];
      if (e.targets && e.targets.length) {
        html.push("<div class='modal-label'>Цели:</div><pre>" +
          esc(e.targets.join("\n")) + "</pre>");
      }
      if (e.shadow_dir) {
        html.push("<button id='jr-diff' class='secondary'>📄 Diff</button> ");
      }
      if (e.reversible > 0) {
        html.push("<button id='jr-rollback-dry' class='secondary'>↩ План отката</button> ");
        html.push("<button id='jr-rollback-run' class='danger'>⚠ Откатить</button>");
      }
      if (e.task_id) {
        html.push("<button id='jr-replay' class='secondary'>▶ План воспроизведения задачи</button>");
      }
      if (e.args && Object.keys(e.args).length) {
        html.push("<div class='modal-label'>Аргументы:</div><pre>" +
          esc(JSON.stringify(e.args, null, 2)).slice(0, 4000) + "</pre>");
      }
      if (e.error) {
        html.push("<div class='modal-label'>Ошибка:</div><pre>" + esc(e.error) + "</pre>");
      }
      if (e.result && typeof e.result === "string") {
        html.push("<div class='modal-label'>Результат:</div><pre>" +
          esc(e.result.slice(0, 2000)) + "</pre>");
      }
      d.innerHTML = html.join("");

      var diffBtn = $("#jr-diff");
      if (diffBtn) diffBtn.addEventListener("click", function () { showDiff(eventId); });
      var dry = $("#jr-rollback-dry");
      if (dry) dry.addEventListener("click", function () { doRollback(eventId, "dry_run"); });
      var run = $("#jr-rollback-run");
      if (run) run.addEventListener("click", function () {
        if (confirm("Откатить это событие? Файлы вернутся к состоянию до правки."))
          doRollback(eventId, "execute");
      });
      var rp = $("#jr-replay");
      if (rp) rp.addEventListener("click", function () { doReplayPlan(e.task_id); });
    }).catch(function (err) {
      alert("Не удалось загрузить событие: " + err.message);
    });
  }

  function showDiff(eventId) {
    api("/api/journal/event/" + eventId + "/diff").then(function (d) {
      var box = $("#jr-detail");
      var pre = el("pre", "jr-diff");
      pre.textContent = (d.diffs || []).map(function (x) {
        return "─── " + x.state + " ───\n" + (x.diff || "(нет различий)");
      }).join("\n\n").slice(0, 20000) || "(нет различий)";
      box.appendChild(pre);
    }).catch(function (e) {
      alert("Diff недоступен: " + e.message);
    });
  }

  function doRollback(eventId, mode) {
    api("/api/journal/rollback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ event_id: eventId, mode: mode }),
    }).then(function (r) {
      showRollbackResult(r);
      if (mode === "execute") { loadEvents(); loadStats(); }
    }).catch(function (e) {
      alert("Ошибка отката: " + e.message);
    });
  }

  function showRollbackResult(r) {
    var box = $("#jr-detail");
    if (!box || box.classList.contains("hidden")) {
      box = $("#jr-list");
    }
    var pre = el("pre", "jr-diff");
    var lines = ["РЕЖИМ: " + r.mode];
    if (r.planned !== undefined) lines.push("шагов в плане: " + r.planned);
    if (r.executed_count !== undefined) lines.push("выполнено: " + r.executed_count);
    (r.skipped || []).slice(0, 10).forEach(function (s) {
      lines.push("пропущено: " + (s.path || s.event_id) + " — " + (s.reason || ""));
    });
    (r.errors || []).slice(0, 10).forEach(function (x) { lines.push("ошибка: " + x); });
    (r.plan || []).slice(0, 20).forEach(function (p) {
      lines.push("  " + (p.action || "?") + " " + ((p.targets || []).join(", ")) +
        (p.reason ? " — " + p.reason : ""));
    });
    pre.textContent = lines.join("\n");
    box.appendChild(pre);
  }

  function doReplayPlan(taskId) {
    api("/api/journal/replay/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task_id: taskId }),
    }).then(function (plan) {
      var box = $("#jr-detail");
      var pre = el("pre", "jr-diff");
      var lines = ["ПЛАН ВОСПРОИЗВЕДЕНИЯ: шагов " + plan.steps_count +
        " (пропущено " + plan.skipped_count + ")"];
      (plan.steps || []).slice(0, 50).forEach(function (s) {
        lines.push(s.n + ". " + s.server + "." + s.tool +
          " " + JSON.stringify(s.args).slice(0, 120));
      });
      pre.textContent = lines.join("\n");
      box.appendChild(pre);
    }).catch(function (e) {
      alert("Ошибка плана: " + e.message);
    });
  }

  /* ── Привязка фильтров ──────────────────────────────────── */
  function bindPanel(panel) {
    $("#jr-refresh", panel).addEventListener("click", refreshAll);
    $("#jr-sweep", panel).addEventListener("click", function () {
      api("/api/journal/retention/sweep", { method: "POST" })
        .then(function (r) {
          alert("Ретенция: " + (r.trigger || "?") +
            "\nсжато дней: " + (r.compressed_days || []).length +
            "\nосвобождено: " + fmtSize(r.freed_bytes || 0));
          loadStats();
        }).catch(function (e) { alert("Ошибка: " + e.message); });
    });
    $("#jr-kind", panel).addEventListener("change", function (e) {
      state.filters.kind = e.target.value; loadEvents();
    });
    $("#jr-task", panel).addEventListener("change", function (e) {
      state.filters.task = e.target.value.trim(); loadEvents();
    });
    $("#jr-search", panel).addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        state.filters.search = e.target.value.trim(); loadEvents();
      }
    });
    $("#jr-mutating", panel).addEventListener("change", function (e) {
      state.filters.mutating = e.target.checked; loadEvents();
    });
  }

  /* ── Инициализация ──────────────────────────────────────── */
  function init() {
    injectTab();
    // модалка могла отрисоваться позже — пробуем ещё пару раз
    var tries = 0;
    var t = setInterval(function () {
      tries += 1;
      if ($("#journal-tab-btn") || tries > 10) clearInterval(t);
      else injectTab();
    }, 500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
