/* Feature: __template__ — вкладка UI (эталонный скелетон).
 *
 * Контракт (такой же, как у features/journal/ui.js, features/notes/ui.js):
 *   - IIFE (function () { ... })();
 *   - идемпотентность через window.__llmTemplateFeatureLoaded;
 *   - mounted() вызывается из app.js после инъекции <script src="/features/__template__/ui.js">;
 *   - создает вкладку в settings-модалке или side panel.
 *
 * Монтаж через FeatureLoader:
 *   GET /api/features → [{id: "__template__", js_url: "/features/__template__/ui.js", ...}]
 *   app.js подгружает каждый ui.js, вызывает window.__llmTemplateFeature.mounted().
 *
 * НЕ используйте глобальные переменные, кроме одного объекта window.__llmTemplateFeature.
 */
(function () {
  "use strict";

  if (window.__llmTemplateFeatureLoaded) return;
  window.__llmTemplateFeatureLoaded = true;

  // ── State (closure) ─────────────────────────────────────────
  var state = {
    items: [],
    refreshTimer: null,
  };

  // ── Helpers ──────────────────────────────────────────────────
  function api(path, opts) {
    opts = opts || {};
    return fetch("/api/__template__/" + path, {
      method: opts.method || "GET",
      headers: {"Content-Type": "application/json"},
      body: opts.body ? JSON.stringify(opts.body) : undefined,
    }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
  }

  function render(container) {
    container.innerHTML = [
      '<div class="template-feature">',
      '  <h3>📦 Шаблон фичи</h3>',
      '  <p>Список items из API /api/__template__/items:</p>',
      '  <div class="template-list" id="template-list"></div>',
      '  <button id="template-add" class="btn">Добавить</button>',
      '</div>',
    ].join("");

    var list = container.querySelector("#template-list");
    state.items.forEach(function (it) {
      var div = document.createElement("div");
      div.textContent = it.id + " — " + JSON.stringify(it.data);
      list.appendChild(div);
    });

    container.querySelector("#template-add").addEventListener("click", function () {
      api("items", {method: "POST", body: {label: "new item"}})
        .then(function (item) {
          state.items.push(item);
          render(container);
        })
        .catch(function (e) {
          console.error("[__template__] create failed:", e);
        });
    });
  }

  // ── Public API (вызывается из app.js) ───────────────────────
  window.__llmTemplateFeature = {
    // Вызывается после загрузки ui.js. Должна добавить вкладку
    // в settings-модалку или side panel.
    mounted: function () {
      console.log("[__template__] feature mounted");
      // Пример: добавить вкладку в settings-модалку
      var settingsModal = document.querySelector("#settings-modal");
      if (!settingsModal) {
        // Модалка ещё не открыта — подписываемся на открытие
        document.addEventListener("llm-agent:settings-opened", function () {
          window.__llmTemplateFeature.attachToSettings();
        }, {once: true});
        return;
      }
      window.__llmTemplateFeature.attachToSettings();
    },

    attachToSettings: function () {
      var settingsModal = document.querySelector("#settings-modal");
      if (!settingsModal) return;

      // Проверяем, не добавили ли уже вкладку
      if (settingsModal.querySelector('[data-feature-tab="__template__"]')) return;

      // Создаём вкладку (кнопку в таб-баре и панель-контент)
      var tabButton = document.createElement("button");
      tabButton.dataset.featureTab = "__template__";
      tabButton.textContent = "📦 Шаблон";
      tabButton.addEventListener("click", function () {
        var panel = settingsModal.querySelector('[data-feature-panel="__template__"]');
        Array.prototype.forEach.call(
          settingsModal.querySelectorAll("[data-feature-panel]"),
          function (p) { p.style.display = "none"; }
        );
        if (panel) panel.style.display = "block";
      });

      var tabBar = settingsModal.querySelector(".tab-bar");
      if (tabBar) tabBar.appendChild(tabButton);

      // Контент-панель
      var panel = document.createElement("div");
      panel.dataset.featurePanel = "__template__";
      panel.style.display = "none";
      settingsModal.querySelector(".modal-body").appendChild(panel);

      render(panel);

      // Загружаем данные с API
      api("items").then(function (r) {
        state.items = r.items || [];
        render(panel);
      }).catch(function (e) {
        console.warn("[__template__] API not available:", e);
      });
    },

    // Health-check для /api/features/health
    health: function () {
      return api("health").then(function (r) {
        return r.status === "ok";
      }).catch(function () {
        return false;
      });
    },

    // Cleanup — вызывается при выключении фичи
    unmount: function () {
      if (state.refreshTimer) clearInterval(state.refreshTimer);
      state.refreshTimer = null;
      var settingsModal = document.querySelector("#settings-modal");
      if (settingsModal) {
        var tab = settingsModal.querySelector('[data-feature-tab="__template__"]');
        if (tab) tab.remove();
        var panel = settingsModal.querySelector('[data-feature-panel="__template__"]');
        if (panel) panel.remove();
      }
      window.__llmTemplateFeatureLoaded = false;
    },
  };

  // ── Auto-mount if document already loaded ───────────────────
  if (document.readyState !== "loading") {
    window.__llmTemplateFeature.mounted();
  } else {
    document.addEventListener("DOMContentLoaded", function () {
      window.__llmTemplateFeature.mounted();
    });
  }
})();
