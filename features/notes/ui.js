/* Заметки — вкладка фичи (эталонный пример Feature SDK, Этап 4).
   Самодостаточная вкладка в настройках: список + добавление.
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
  function api(path, opts) {
    return fetch(path, {
      method: (opts && opts.method) || "GET",
      headers: { "Content-Type": "application/json" },
      body: opts && opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#notes-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🗒️ Заметки");
    btn.id = "notes-tab-btn";
    btn.setAttribute("data-tab", "notes");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "notes");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="notes-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="notes-summary" class="summary">—</span>' +
      "</div>" +
      '<div class="settings-section">' +
        '<div class="form-row"><input id="notes-input" class="input" ' +
        'placeholder="Текст заметки… (Enter — добавить)"></div>' +
        '<div class="settings-actions">' +
          '<button id="notes-add" class="primary" type="button">➕ Добавить</button>' +
        "</div>" +
      "</div>" +
      '<div id="notes-list" class="list"></div>';
    body.appendChild(panel);

    async function load() {
      try {
        var d = await api("/api/notes");
        $("#notes-summary").textContent = "заметок: " + d.count;
        var list = $("#notes-list");
        list.innerHTML = "";
        (d.notes || []).forEach(function (n) {
          var row = el("div", "agent-card");
          var head = el("div", "agent-header");
          var title = el("div", "agent-title");
          title.appendChild(el("code", "agent-id", "#" + n.id));
          title.appendChild(el("span", "summary", n.text));
          head.appendChild(title);
          var del = el("button", "secondary danger-text", "✕");
          del.onclick = function () {
            api("/api/notes/" + n.id, { method: "DELETE" }).then(load);
          };
          head.appendChild(del);
          row.appendChild(head);
          row.appendChild(el("div", "agent-desc",
            new Date((n.created_at || 0) * 1000).toLocaleString()));
          list.appendChild(row);
        });
      } catch (e) {
        $("#notes-summary").textContent = "ошибка: " + e.message;
      }
    }

    function add() {
      var input = $("#notes-input");
      var text = input.value.trim();
      if (!text) return;
      api("/api/notes", { method: "POST", body: { text: text } })
        .then(function () { input.value = ""; load(); })
        .catch(function (e) { alert("Не добавлено: " + e.message); });
    }

    $("#notes-refresh").onclick = load;
    $("#notes-add").onclick = add;
    $("#notes-input").addEventListener("keydown", function (e) {
      if (e.key === "Enter") add();
    });

    // ленивая загрузка при первом открытии вкладки
    btn.addEventListener("click", function once() {
      btn.removeEventListener("click", once);
      load();
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectTab);
  } else {
    injectTab();
  }
})();
