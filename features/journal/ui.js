/* Журнал — вкладка фичи (Этап 4).
   Сам монтаж делает самодостаточный /static/journal.js (он добавляет
   вкладку «Журнал» в модалку настроек сам). Эта обёртка существует,
   чтобы вкладка подключалась через реестр фич (GET /api/features →
   js_url) без единой правки index.html / app.js. */
(function () {
  "use strict";
  if (window.__llmJournalFeatureLoaded) return;
  window.__llmJournalFeatureLoaded = true;

  function load() {
    var s = document.createElement("script");
    s.src = "/static/journal.js";
    s.defer = true;
    document.head.appendChild(s);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
