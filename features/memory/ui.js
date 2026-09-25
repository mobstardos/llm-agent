/* Память — вкладка фичи: семантический индекс прошлых задач (pgvector).
   Поиск по смыслу (vector → FTS → ILIKE), статистика, последние записи,
   ручная индексация зеркал и очистка. Подключается через GET /api/features. */
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

  var state = { view: "search" };

  function injectTab() {
    var modal = $("#settings-modal");
    if (!modal || $("#memory-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "🧠 Память");
    btn.id = "memory-tab-btn";
    btn.setAttribute("data-tab", "memory");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "memory");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="mem-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="mem-status" class="summary">—</span>' +
      "</div>" +
      '<div class="panel-actions" id="mem-nav">' +
        '<button class="secondary" data-mview="search">🔎 Поиск</button>' +
        '<button class="secondary" data-mview="recent">🕘 Последние</button>' +
        '<button class="secondary" data-mview="stats">📊 Статистика</button>' +
        '<button class="secondary" data-mview="index">⚙ Индексация</button>' +
      "</div>" +
      '<div id="mem-body" class="list"></div>';
    body.appendChild(panel);

    $("#mem-refresh").onclick = render;
    Array.prototype.forEach.call(
      panel.querySelectorAll("[data-mview]"), function (b) {
        b.onclick = function () {
          state.view = b.getAttribute("data-mview");
          render();
        };
      });

    btn.addEventListener("click", function once() {
      btn.removeEventListener("click", once);
      loadStatus();
      render();
    });
  }

  function setStatus(text) {
    var s = $("#mem-status");
    if (s) s.textContent = text;
  }

  function loadStatus() {
    api("/api/memory/stats").then(function (d) {
      var mode = d.pgvector ? "pgvector" : (d.fts ? "FTS" : "нет PG");
      setStatus("✅ записей: " + d.total + " · embedder: " + d.embedder +
        " · режим: " + mode);
    }).catch(function (e) { setStatus("❌ " + e.message, true); });
  }

  function infoRow(text) {
    return el("div", "summary", text);
  }

  var KIND_LABEL = {
    chat: "💬 чат", plan: "🧭 план", event: "⚠ событие", note: "📝 заметка"
  };

  function itemCard(item, withScore) {
    var card = el("div", "agent-card");
    var head = el("div", "agent-name",
      (KIND_LABEL[item.kind] || item.kind) +
      (withScore ? " · " + Math.round((item.score || 0) * 100) + "%" : "") +
      (item.session_id ? " · сессия " + item.session_id.slice(0, 12) : "") +
      (item.plan_id ? " · план " + item.plan_id.slice(0, 12) : ""));
    card.appendChild(head);
    var txt = el("div", "agent-desc", String(item.text || "").slice(0, 300));
    card.appendChild(txt);
    if (item.created_at) {
      card.appendChild(el("div", "summary", String(item.created_at)
        .replace("T", " ").slice(0, 19)));
    }
    return card;
  }

  function render() {
    var body = $("#mem-body");
    if (!body) return;
    body.innerHTML = "";
    var fn = { search: viewSearch, recent: viewRecent,
               stats: viewStats, index: viewIndex }[state.view] || viewSearch;
    try { fn(body); } catch (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    }
  }

  function viewSearch(body) {
    var wrap = el("div", "panel-actions");
    var inp = el("input");
    inp.type = "text";
    inp.placeholder = "Найти по смыслу: «настройка бэкапов», «ошибка подключения»…";
    inp.style.flex = "1";
    var sel = el("select");
    [["", "все виды"], ["chat", "чат"], ["plan", "планы"],
     ["event", "события"], ["note", "заметки"]].forEach(function (kv) {
      var o = el("option", "", kv[1]);
      o.value = kv[0];
      sel.appendChild(o);
    });
    var go = el("button", "primary", "Найти");
    wrap.appendChild(inp); wrap.appendChild(sel); wrap.appendChild(go);
    body.appendChild(wrap);

    go.onclick = function () {
      var q = inp.value.trim();
      if (!q) return;
      body.appendChild(infoRow("Ищу…"));
      api("/api/memory/search", {
        method: "POST",
        body: { query: q, top_k: 15,
                kind: sel.value || null }
      }).then(function (d) {
        body.innerHTML = "";
        var modeLabel = { vector: "🧠 pgvector", fts: "🔎 FTS",
                          ilike: "🔤 подстрока", none: "—" }[d.mode] || d.mode;
        body.appendChild(infoRow("Режим поиска: " + modeLabel +
          " · найдено: " + (d.items || []).length));
        if (!(d.items || []).length) {
          body.appendChild(infoRow("Ничего не найдено"));
          return;
        }
        d.items.forEach(function (item) {
          body.appendChild(itemCard(item, true));
        });
      }).catch(function (e) {
        body.innerHTML = "";
        body.appendChild(infoRow("Ошибка: " + e.message));
      });
    };
    inp.onkeydown = function (e) { if (e.key === "Enter") go.onclick(); };
  }

  function viewRecent(body) {
    api("/api/memory/recent?limit=30").then(function (d) {
      if (!(d.items || []).length) {
        body.appendChild(infoRow("Память пуста — запустите индексацию"));
        return;
      }
      d.items.forEach(function (item) {
        body.appendChild(itemCard(item, false));
      });
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  function viewStats(body) {
    api("/api/memory/stats").then(function (d) {
      var card = el("div", "agent-card");
      card.appendChild(el("div", "agent-name",
        "Всего записей: " + d.total +
        " · с вектором: " + (d.pgvector ? d.embedded : "n/a")));
      var kinds = Object.keys(d.by_kind || {});
      if (kinds.length) {
        card.appendChild(el("div", "agent-desc", kinds.map(function (k) {
          return (KIND_LABEL[k] || k) + ": " + d.by_kind[k];
        }).join(" · ")));
      }
      card.appendChild(el("div", "summary",
        "Embedder: " + d.embedder + " · dim: " + d.dim +
        " · pgvector: " + (d.pgvector ? "да" : "нет (поиск по FTS)") +
        " · FTS: " + (d.fts ? "да" : "нет")));
      body.appendChild(card);
    }).catch(function (e) {
      body.appendChild(infoRow("Ошибка: " + e.message));
    });
  }

  function viewIndex(body) {
    var row = el("div", "panel-actions");
    var runBtn = el("button", "primary", "⚙ Индексировать сейчас");
    var clearBtn = el("button", "secondary", "🗑 Очистить память");
    row.appendChild(runBtn);
    row.appendChild(clearBtn);
    body.appendChild(row);

    runBtn.onclick = function () {
      runBtn.disabled = true;
      setStatus("Индексирую зеркала…");
      api("/api/memory/index/run", { method: "POST", body: { limit: 500 } })
        .then(function (d) {
          runBtn.disabled = false;
          setStatus("✅ добавлено: чат " + (d.chat || 0) +
            " · планы " + (d.plan || 0) + " · события " + (d.event || 0));
          render();
        }).catch(function (e) {
          runBtn.disabled = false;
          setStatus("❌ " + e.message, true);
        });
    };

    clearBtn.onclick = function () {
      if (!window.confirm("Удалить ВСЕ записи памяти агентов?")) return;
      api("/api/memory?confirm=true", { method: "DELETE" })
        .then(function (d) {
          setStatus("✅ удалено: " + d.deleted);
          render();
        }).catch(function (e) { setStatus("❌ " + e.message, true); });
    };

    body.appendChild(infoRow(
      "Фоновый индексатор читает зеркала (диалоги, планы, ошибки " +
      "инструментов) и складывает их в memory.tasks каждые 2 минуты. " +
      "Кнопка выше — прогон вручную без ожидания."));
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", injectTab);
  } else {
    injectTab();
  }
})();
