/* ─── LLM Agent — интерактивный ознакомительный курс (/guide) ─────────
   Зависимостей нет: vanilla JS. Прогресс — в localStorage.
   Живые проверки дергают реальные API сервера и честно показывают,
   если что-то недоступно (совет из FAQ). */

"use strict";

const LS_KEY = "llm-guide-progress-v1";

/* ─── Утилиты ───────────────────────────────────────────────────────── */
const $ = (sel) => document.querySelector(sel);

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(url) {
  const r = await fetch(url, { headers: { Accept: "application/json" } });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

/* Фабрика живой проверки: HTTP 200 + валидный JSON → ok. */
function apiCheck(name, url, summarize) {
  return {
    name,
    run: async () => {
      try {
        const data = await api(url);
        let detail = "";
        try { detail = summarize ? summarize(data) : "HTTP 200, JSON"; }
        catch (e) { detail = "HTTP 200, JSON"; }
        return { ok: true, detail };
      } catch (e) {
        return { ok: false, detail: String(e.message || e) };
      }
    },
  };
}

function copyBtn(text) {
  const id = "c" + Math.random().toString(36).slice(2, 8);
  setTimeout(() => {
    const el = document.getElementById(id);
    if (el) el.addEventListener("click", () => {
      navigator.clipboard && navigator.clipboard.writeText(text);
      el.textContent = "✓ скопировано";
      setTimeout(() => { el.textContent = "копировать"; }, 1500);
    });
  }, 0);
  return '<pre><code>' + esc(text) + '</code>' +
         '<button class="g-copy" id="' + id + '" type="button">копировать</button></pre>';
}

/* ─── Уроки ─────────────────────────────────────────────────────────── */
const LESSONS = [
{
  id: "l1", icon: "🤖", title: "Знакомство с системой",
  lead: "Что это такое и как устроено под капотом",
  body: `
  <p><b>LLM Agent</b> — мультиагентная система правки файлов и баз данных.
  Вы описываете задачу в чате обычным языком, а система сама планирует,
  выполняет и отчитывается за каждый шаг.</p>

  <h3>Как выполняется ваша задача</h3>
  <ol>
    <li><b>Супервизор</b> превращает сложный запрос в план из шагов (DAG);
        шаги видны прямо в чате.</li>
    <li><b>Оркестратор</b> на каждый шаг выбирает профильного агента
        (файлы, SQL, браузер, 1С, документы…).</li>
    <li><b>Агенты</b> действуют через MCP-инструменты: чтение/запись файлов,
        shell, SQL, HTTP, браузер и т.д.</li>
    <li><b>Журнал</b> записывает каждое действие — любое можно откатить
        (⚙️ → Rollback).</li>
    <li><b>Память</b> запоминает факты, события и удачные приёмы —
        со временем агент отвечает точнее.</li>
  </ol>

  <h3>Где что находится</h3>
  <table class="g-table">
    <tr><th>Место</th><th>Что там</th></tr>
    <tr><td>Шапка чата</td><td>статус системы, папка проекта 📁, выбор модели, последние чаты, 📊 аналитика, ⚙️ настройки, 🎓 этот курс</td></tr>
    <tr><td>Кнопка ⚙️</td><td>14 вкладок: агенты, MCP, политики, бэкапы, AGE, аудит, откат…</td></tr>
    <tr><td><code>/analytics</code></td><td>аналитика работы системы</td></tr>
    <tr><td><code>/guide</code></td><td>этот курс — вернуться можно всегда</td></tr>
  </table>

  <div class="g-tip"><b>Совет:</b> полный список возможностей с таблицами
  агентов и MCP-серверов — в файле <code>docs/CAPABILITIES.md</code> проекта.</div>
  `,
  quiz: {
    q: "Кто разбивает вашу сложную задачу на шаги?",
    options: [
      "Супервизор — он строит план (DAG) и показывает шаги в чате",
      "MCP-сервер файловой системы",
      "Браузерное расширение",
      "Кэш ответов",
    ],
    correct: 0,
    explain: "Супервизор планирует, оркестратор назначает агентов, агенты выполняют через MCP-инструменты.",
  },
},
{
  id: "l2", icon: "🩺", title: "Проверка системы",
  lead: "Убедимся, что сервер жив и отвечает — прямо сейчас, живыми запросами",
  body: `
  <p>Ниже — настоящие проверки текущего сервера. Нажмите «▶ Запустить проверки»:
  кнопки дергают реальные API так же, как это делает веб-интерфейс.</p>

  <h3>Если проверка не прошла</h3>
  <ul>
    <li>«fetch failed» / «HTTP 000» — сервер не запущен: выполните
        <code>run.bat</code> (Windows) или <code>bash run.sh</code> в папке проекта.</li>
    <li>Сервер запущен, а страница открыта не с того адреса — откройте
        <code>/guide</code> с того же хоста и порта, что и чат.</li>
    <li>Подробная диагностика — ⚙️ → «Диагностика» и <code>docs/FAQ.md</code>.</li>
  </ul>

  <h3>Проверка из консоли (альтернатива)</h3>
  ${copyBtn("python first_run.py --check-llm   # связь с LLM-провайдером\ncurl http://127.0.0.1:8000/api/db/status")}
  `,
  checks: [
    apiCheck("Сервер: /api/db/status", "/api/db/status",
      (d) => "ok=" + (d.ok !== undefined ? d.ok : "?") + " · backend=" + (d.backend || d.storage || "—")),
    apiCheck("Кэш ответов: /api/cache/stats", "/api/cache/stats",
      (d) => Object.keys(d).length + " полей метрик"),
    apiCheck("Фичи системы: /api/features", "/api/features",
      (d) => Array.isArray(d.features) ? ("фич: " + d.features.length)
           : Array.isArray(d) ? ("фич: " + d.length) : "HTTP 200"),
  ],
  quiz: {
    q: "Самый быстрый способ узнать, жив ли сервер?",
    options: [
      "Открыть http://127.0.0.1:8000/api/db/status",
      "Перезагрузить компьютер",
      "Переустановить Python",
      "Удалить папку data/",
    ],
    correct: 0,
    explain: "GET /api/db/status — лёгкий health-эндпоинт; его же использует смоук-тест установщика.",
  },
},
{
  id: "l3", icon: "🗨️", title: "Чат: постановка задач",
  lead: "Как формулировать задачи и что происходит после отправки",
  body: `
  <p>Введите задачу в поле внизу чата и нажмите <b>Ctrl+Enter</b> (или кнопку
  «Отправить»). Хорошая задача — конкретная: какой файл, какая база, какой
  результат нужен.</p>

  <h3>Что вы увидите</h3>
  <ul>
    <li><b>Шаги плана</b> — супервизор покажет, из чего состоит работа.</li>
    <li><b>Стриминг</b> — ответ появляется по мере генерации, не ждём конца.</li>
    <li><b>«Размышления»</b> — если модель отдаёт reasoning, он показывается
        отдельной дорожкой и сворачивается при первом токене ответа.</li>
    <li><b>Подтверждения</b> — опасные действия (запись файла, shell, SQL)
        потребуют вашего «Подтвердить»; можно запомнить решение для
        инструмента/файла/папки, чтобы не спрашивали каждый раз.</li>
    <li><b>Бейджи</b> — «💻 локальная модель» у офлайн-ответов и
        «♻️ фолбэк», если основной провайдер не ответил и система
        переключилась на резервный.</li>
  </ul>

  <div class="g-warn"><b>Важно:</b> агент реально меняет файлы выбранного
  проекта. Папка задаётся кнопкой 📁 в шапке — проверьте её перед большой
  задачей. Откат — ⚙️ → Rollback.</div>

  <h3>Переключение чатов</h3>
  <p>Выпадающий список «Последние чаты» в шапке: история чатов сохраняется,
  «＋ Новый чат» начинает с чистого листа. Заголовки чатов генерирует
  локальная модель — облако не тратится.</p>
  `,
  quiz: {
    q: "Агент хочет отредактировать файл. Что вы увидите?",
    options: [
      "Модальное окно подтверждения с инструментом и аргументами",
      "Ничего — файл молча перезапишется без ведома",
      "Синий экран",
      "Письмо на почту",
    ],
    correct: 0,
    explain: "Approval-модалка показывает инструмент, причину и аргументы; решение можно запомнить.",
  },
},
{
  id: "l4", icon: "🧠", title: "Выбор модели",
  lead: "13 провайдеров, локальные модели, живой список прямо с сервера",
  body: `
  <p>Кнопка с именем модели в шапке чата открывает комбо-бокс с поиском.
  Модели сгруппированы по провайдерам, доступные — сверху. Возможности
  каждого показаны бейджами: «💻 локальная», «без tools», причина
  недоступности (например «нет ключа DEEPSEEK_API_KEY»).</p>

  <h3>Живой список провайдеров</h3>
  <p>Ниже — реальный ответ <code>/api/models</code> вашего сервера:
  что доступно именно у вас сейчас.</p>

  <h3>Как это работает</h3>
  <ul>
    <li>Запрос уходит на endpoint выбранной модели — qwenproxy, DeepSeek,
        Ollama, OpenAI, OpenRouter, VseGPT и др.</li>
    <li>Выбор сохраняется и переживает перезагрузку страницы и сервера.</li>
    <li>Если endpoint молчит — автоматический фолбэк по цепочке
        (в чате появится бейдж «♻️ переключился на …»).</li>
    <li>Локальная Ollama (например <code>qwen2.5:1.5b-instruct</code>) —
        полноценный офлайн-режим без облака.</li>
  </ul>

  <div class="g-tip"><b>Добавить своего провайдера:</b> 3 строки в
  <code>config/models.yaml</code> — инструкция в <code>docs/providers.md</code>.</div>
  `,
  checks: [
    apiCheck("Провайдеры: /api/models", "/api/models",
      (d) => {
        const ps = (d.providers || []).length;
        const ms = (d.models || []).length || (Array.isArray(d) ? d.length : 0);
        return "провайдеров: " + ps + " · моделей: " + ms;
      }),
  ],
  quiz: {
    q: "Основной провайдер не ответил. Что сделает система?",
    options: [
      "Попробует следующий по цепочке фолбэков и покажет бейдж ♻️",
      "Удалит чат",
      "Выключит сервер",
      "Ничего, будет ждать вечно",
    ],
    correct: 0,
    explain: "Фолбэк-цепочка: LLM_FALLBACKS из .env или selection.fallbacks в models.yaml.",
  },
},
{
  id: "l5", icon: "🛠️", title: "Агенты и MCP-инструменты",
  lead: "37 агентов и 39 MCP-серверов — руки и ноги системы",
  body: `
  <p><b>Агент</b> — специалист со своим промптом и набором инструментов:
  <code>file</code> (файлы), <code>postgres</code> (SQL), <code>browser</code>
  (страницы), <code>onec_*</code> (всё для 1С), <code>document</code>
  (PDF/DOCX/XLSX), <code>media</code> (аудио/видео) и десятки других.</p>
  <p><b>MCP-серверы</b> — поставщики инструментов: безопасная работа
  с файловой системой, shell с подтверждением, SQL-клиенты, HTTP, LSP,
  grep по коду, OCR, vision, транскрипция речи.</p>

  <h3>Управление</h3>
  <ul>
    <li>⚙️ → <b>Агенты</b>: включить/выключить, параметры, «Проверить».</li>
    <li>⚙️ → <b>MCP</b>: то же для серверов; там же видно, кто на чём работает.</li>
    <li>⚙️ → <b>Capabilities</b>: декларативные описания умений
        (<code>capabilities/*.yaml</code>).</li>
  </ul>

  <h3>Живой реестр</h3>`,
  checksAfterBody: [
    apiCheck("Агенты: /api/registry/agents", "/api/registry/agents",
      (d) => Array.isArray(d) ? ("агентов: " + d.length)
           : (d.agents ? ("агентов: " + (Array.isArray(d.agents) ? d.agents.length : Object.keys(d.agents).length)) : "HTTP 200")),
    apiCheck("MCP: /api/registry/mcp", "/api/registry/mcp",
      (d) => Array.isArray(d) ? ("серверов: " + d.length)
           : (d.servers ? ("серверов: " + (Array.isArray(d.servers) ? d.servers.length : Object.keys(d.servers).length)) : "HTTP 200")),
  ],
  body2: `
  <div class="g-tip"><b>Свой агент:</b> <code>python scripts/new_agent.py имя</code>
  — скелет агента создастся по шаблону проекта.</div>
  `,
  quiz: {
    q: "Кому агенты «звонят», чтобы выполнить действие над файлами?",
    options: [
      "MCP-серверу filesystem (и другим профильным серверам)",
      "Напрямую в OpenAI",
      "В браузер пользователя",
      "В кэш",
    ],
    correct: 0,
    explain: "MCP — протокол инструментов; каждый сервер даёт агентов набором безопасных операций.",
  },
},
{
  id: "l6", icon: "🧷", title: "Память",
  lead: "Эпизодическая, семантическая, процедурная, граф — и AGE в PostgreSQL",
  body: `
  <p>Система помнит контекст между диалогами:</p>
  <ul>
    <li><b>Эпизодическая</b> — что происходило в ваших диалогах.</li>
    <li><b>Семантическая</b> — факты и знания, найдутся векторным поиском.</li>
    <li><b>Процедурная</b> — удачные приёмы решения задач.</li>
    <li><b>Граф знаний</b> — сущности проекта и связи; парсеры кода
        Python/TS/JS строят граф автоматически.</li>
  </ul>

  <h3>Хранилища и эмбеддеры</h3>
  <table class="g-table">
    <tr><th>Режим</th><th>Где лежит</th><th>Когда выбрать</th></tr>
    <tr><td>по умолчанию</td><td>SQLite + LanceDB (локально)</td><td>всегда, без настройки</td></tr>
    <tr><td>PostgreSQL</td><td>pgvector + Apache AGE</td><td>есть PG — долговременная память и граф</td></tr>
    <tr><td>embedder <code>hash</code></td><td>детерминированный хеш</td><td>мгновенно, без загрузки моделей</td></tr>
    <tr><td>embedder <code>bge-m3</code></td><td>качается при <code>auto</code></td><td>качественный поиск, ~1–2 ГБ</td></tr>
  </table>

  <h3>AGE-граф</h3>
  <p>Если PostgreSQL включён — граф можно смотреть и запрашивать Cypher-ом
  (⚙️ → AGE). Ниже — живая статистика; если она «недоступна», у вас
  просто не включён PG — это нормально, всё остальное работает.</p>`,
  checksAfterBody: [
    apiCheck("Статистика памяти/AGE: /api/age/stats", "/api/age/stats",
      (d) => "узлов: " + (d.nodes != null ? d.nodes : Object.keys(d).length)),
  ],
  quiz: {
    q: "Система работает без PostgreSQL?",
    options: [
      "Да — SQLite + LanceDB локально, PG только улучшает память и аналитику",
      "Нет, без PG ничего не запустится",
      "Только по вторникам",
      "Только с ключом OpenAI",
    ],
    correct: 0,
    explain: "PG — опциональное ускорение; локальный режим полноценный.",
  },
},
{
  id: "l7", icon: "📓", title: "Журнал и откат",
  lead: "Каждое действие записывается — и может быть отменено",
  body: `
  <p><b>Журнал (Journal)</b> — центральная идея безопасности проекта.
  Любое действие агента (запись файла, SQL-операция, настройка) попадает
  в журнал с деталями и может быть <b>откачено</b> одним кликом.</p>

  <h3>Где находится</h3>
  <ul>
    <li>⚙️ → <b>Rollback</b> — список операций и восстановление.</li>
    <li>⚙️ → <b>История</b> — диффы изменений реестра настроек.</li>
    <li>⚙️ → <b>Аудит</b> — кто и когда менял административные вещи.</li>
    <li>CLI: <code>python -m src.journal.cli --help</code> — журнал
        из консоли, включая реплей последовательностей.</li>
  </ul>

  <h3>Репликация и дайджест</h3>
  <p>При включённом PostgreSQL журнал зеркалируется в PG (репликатор с
  backoff — не забивает лог при недоступности). Локальная модель раз в цикл
  пишет <b>дайджест журнала</b> — ⚙️ → «Дайджест журнала».</p>`,
  checksAfterBody: [
    apiCheck("Планы супервизора: /api/plans", "/api/plans",
      (d) => Array.isArray(d) ? ("планов: " + d.length) : "HTTP 200"),
    apiCheck("Дайджест: /api/digest/latest", "/api/digest/latest",
      (d) => d.text ? ("текст " + String(d.text).length + " симв.") : "пусто/пока не готов"),
  ],
  quiz: {
    q: "Агент перезаписал не тот файл. Первое действие?",
    options: [
      "⚙️ → Rollback → восстановить состояние из журнала",
      "Паниковать и удалять проект",
      "Перезагрузить Windows",
      "Ничего, файл не вернуть",
    ],
    correct: 0,
    explain: "Журнал существует именно для этого: откат файловых операций и записей БД.",
  },
},
{
  id: "l8", icon: "🌉", title: "Расширение браузера",
  lead: "Мост чат ↔ браузер: куки веб-чатов в один клик, wake-кнопка, суммаризация",
  body: `
  <p>Расширение <b>LLM Agent Bridge</b> (v1.2.0) связывает чат с браузером:</p>
  <ul>
    <li><b>🔑 Куки веб-чатов</b> — кнопки DeepSeek/Qwen в попапе: открывают
        страницу логина, после авторизации куки сами прилетают в систему
        (ручной F12 не нужен).</li>
    <li><b>Alt+Shift+B</b> — разбудить агента на текущей странице.</li>
    <li><b>Суммаризация</b> — пункт в контекстном меню: страница целиком
        уходит агенту, тот возвращает выжимку в чат.</li>
  </ul>

  <h3>Установка</h3>
  ${copyBtn("Chromium: chrome://extensions → Загрузить распакованное → папка extension/chromium\nFirefox: about:debugging → Загрузить временное дополнение → extension/firefox\nПостоянная установка в Firefox: python scripts/sign_firefox.py --api-key ... --api-secret ...")}

  <h3>Куки в системе</h3>
  <p>Ниже — живой список куков, известных системе (значения не показываются,
  только статусы). Куки можно удалить кнопкой в чате (⚙️ → Bridge) или здесь.</p>`,
  checksAfterBody: [
    apiCheck("Куки веб-чатов: /api/bridge/cookies", "/api/bridge/cookies",
      (d) => {
        const arr = Array.isArray(d) ? d : (d.cookies || d.items || []);
        return "записей: " + (arr.length || 0);
      }),
  ],
  quiz: {
    q: "Куда попадают куки DeepSeek после кнопки 🔑 в расширении?",
    options: [
      "В ваш сервер (/api/bridge/cookies), дальше — в .env/реестр веб-куков",
      "На почту разработчика",
      "В облако OpenAI",
      "Никуда, расширение только для вида",
    ],
    correct: 0,
    explain: "Куки остаются на вашей машине и используются вашим сервером для браузерного пути DeepSeek.",
  },
},
{
  id: "l9", icon: "🐘", title: "PostgreSQL, бэкапы, данные",
  lead: "Зеркала, аналитика, AGE, CDC, бэкапы — что даёт PG и как не потерять данные",
  body: `
  <p>Без PostgreSQL всё работает локально (SQLite + LanceDB). С PG появляются:
  долговременные зеркала журнала/сессий/планов, память агентов в pgvector,
  AGE-граф, SQL-аналитика (📊), CDC-события.</p>

  <h3>Включить за две команды</h3>
  ${copyBtn("docker compose up -d postgres\npython scripts/init_db.py          # идемпотентно: init + journal + ops + analytics + age")}

  <h3>Автодетект</h3>
  <p>При старте <code>run.py</code> система сама находит службу PostgreSQL:
  пробует пароль из .env, затем словарь дефолтных паролей, затем спрашивает
  админский (в консоли), создаёт роль <code>llmagent</code> и накатывает
  схему. Не нужен PG — <code>PG_ENABLED=false</code> и <code>PG_REPLICATE=0</code>
  в .env, лог будет тихим.</p>

  <h3>Бэкапы</h3>
  <p>⚙️ → <b>Бэкапы</b>: создать/восстановить/скачать. Авто-бэкапы:
  <code>BACKUP_ENABLED=true</code>, интервал — <code>BACKUP_INTERVAL_HOURS</code>,
  сколько хранить — <code>BACKUP_KEEP_LAST</code>. Перед большими
  экспериментами — бэкап вручную.</p>`,
  checksAfterBody: [
    apiCheck("Автодетект PG: /api/db/autodetect", "/api/db/autodetect",
      (d) => "статус: " + (d.status || "?")),
    apiCheck("Бэкапы: /api/backup/list", "/api/backup/list",
      (d) => {
        const arr = Array.isArray(d) ? d : (d.backups || []);
        return "бэкапов: " + (arr.length || 0);
      }),
  ],
  quiz: {
    q: "PostgreSQL не установлен. Что будет?",
    options: [
      "Система честно напишет об этом один раз и будет работать на SQLite + LanceDB",
      "Сервер упадёт при старте",
      "Появится спам в логе каждую секунду",
      "Чат не откроется",
    ],
    correct: 0,
    explain: "Локальный режим — полноценный; сообщение об отсутствии PG появляется один раз (потом раз в 60–300с при попытках репликации, если не выключить PG_REPLICATE=0).",
  },
},
{
  id: "l10", icon: "🏁", title: "Итоги и самопроверка",
  lead: "Финальный чек-лист: всё важное в одном месте",
  body: `
  <h3>Вы прошли курс. Теперь вы умеете:</h3>
  <ul>
    <li>✅ Ставить и запускать систему (<code>install.py</code> → <code>run.bat</code>)</li>
    <li>✅ Ставить задачи в чате и управлять подтверждениями</li>
    <li>✅ Выбирать модели из 13 провайдеров, понимать фолбэки</li>
    <li>✅ Управлять агентами и MCP-инструментами</li>
    <li>✅ Откатывать действия агента через журнал</li>
    <li>✅ Подключать расширение и куки веб-чатов</li>
    <li>✅ Включать PostgreSQL и бэкапы</li>
  </ul>

  <h3>Комплексная самопроверка</h3>
  <p>Ниже — сводный прогон ключевых API. Всё зелёное — система полностью
  готова к работе.</p>

  <h3>Шпаргалка команд</h3>
  ${copyBtn("python install.py                 # установка\nrun.bat                            # запуск (Windows)\npython first_run.py --reconfigure-ai  # перенастроить AI\npython first_run.py --check-llm    # проверить связь с LLM\npython scripts/init_db.py --check  # проверить схему PG\npython scripts/test_onboarding.py  # самопроверка поставки")}

  <h3>Что читать дальше</h3>
  <ul>
    <li><code>docs/CAPABILITIES.md</code> — все возможности с таблицами</li>
    <li><code>docs/GETTING_STARTED.md</code> — установка и настройка по шагам</li>
    <li><code>docs/FAQ.md</code> — решения типовых проблем</li>
    <li><code>docs/JOURNAL.md</code>, <code>docs/DATABASE.md</code>, <code>docs/providers.md</code> — по темам</li>
  </ul>`,
  checksAfterBody: [
    apiCheck("Сервер", "/api/db/status", (d) => "ok=" + (d.ok !== undefined ? d.ok : "?")),
    apiCheck("Модели", "/api/models", (d) => "провайдеров: " + (d.providers || []).length),
    apiCheck("Агенты", "/api/registry/agents", (d) => Array.isArray(d) ? d.length : "ok"),
    apiCheck("MCP", "/api/registry/mcp", (d) => Array.isArray(d) ? d.length : "ok"),
    apiCheck("Фичи", "/api/features", (d) => Array.isArray(d.features) ? d.features.length : "ok"),
  ],
},
];

/* ─── Прогресс ──────────────────────────────────────────────────────── */
function loadProgress() {
  try {
    const p = JSON.parse(localStorage.getItem(LS_KEY) || "{}");
    return { done: Array.isArray(p.done) ? p.done : [], last: p.last || null };
  } catch (e) { return { done: [], last: null }; }
}
function saveProgress(p) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(p)); } catch (e) { /* приватный режим */ }
}

let state = { idx: 0, done: [] };

/* ─── Рендер ────────────────────────────────────────────────────────── */
function renderSidebar() {
  const box = $("#g-sidebar");
  box.innerHTML = LESSONS.map((L, i) => `
    <div class="g-nav-item ${i === state.idx ? "active" : ""} ${state.done.includes(L.id) ? "done" : ""}"
         data-i="${i}">
      <span class="g-num">${i + 1}</span>
      <span class="g-text">${L.icon} ${esc(L.title)}</span>
      <span class="g-check">✓</span>
    </div>`).join("");
  box.querySelectorAll(".g-nav-item").forEach((el) => {
    el.addEventListener("click", () => goto(parseInt(el.dataset.i, 10)));
  });
}

function renderLesson() {
  const L = LESSONS[state.idx];
  const main = $("#g-main");
  let html = `<div class="g-lesson">
    <h2>${L.icon} ${esc(L.title)}</h2>
    <p class="g-lead">${esc(L.lead)}</p>
    ${L.body}`;

  if (L.checks && L.checks.length) html += renderChecks(L.checks, "checks-main-" + L.id);
  if (L.checksAfterBody && L.checksAfterBody.length)
    html += renderChecks(L.checksAfterBody, "checks-post-" + L.id);
  if (L.body2) html += L.body2;
  if (L.quiz) html += renderQuiz(L);
  html += `</div>`;
  main.innerHTML = html;
  main.scrollTop = 0;

  wireChecks(main);
  if (L.quiz) wireQuiz(main, L);
  wireCopy(main);

  $("#g-prev").disabled = state.idx === 0;
  $("#g-next").textContent = state.idx === LESSONS.length - 1 ? "Завершить ✓" : "Далее →";
  $("#g-step-label").textContent = "Урок " + (state.idx + 1) + " из " + LESSONS.length;
  renderSidebar();
  $("#g-progress-num").textContent = state.done.length;
  $("#g-progress-total").textContent = LESSONS.length;
}

function renderChecks(checks, key) {
  return `<div class="g-checks" data-key="${key}">
    <button class="g-run-btn secondary" type="button">▶ Запустить проверки</button>
    <div class="g-rows"></div>
  </div>`;
}

function wireChecks(root) {
  root.querySelectorAll(".g-checks").forEach((box) => {
    const btn = box.querySelector(".g-run-btn");
    btn.addEventListener("click", async () => {
      btn.disabled = true; btn.textContent = "… выполняю";
      const rows = box.querySelector(".g-rows");
      const lesson = LESSONS[state.idx];
      const key = box.dataset.key;
      const checks = key.startsWith("checks-main-") ? lesson.checks : lesson.checksAfterBody;
      rows.innerHTML = "";
      for (const c of checks) {
        const row = document.createElement("div");
        row.className = "g-check-row run";
        row.innerHTML = `<span class="g-name">${esc(c.name)}</span>
                         <span class="g-state">проверяю…</span>`;
        rows.appendChild(row);
        const res = await c.run();
        row.className = "g-check-row " + (res.ok ? "ok" : "bad");
        row.innerHTML = `<span class="g-name">${esc(c.name)}</span>
                         <span class="g-state">${res.ok ? "✓ доступно" : "✗ недоступно"}</span>
                         <div class="g-detail">${esc(res.detail)}</div>`;
      }
      btn.disabled = false; btn.textContent = "↻ Повторить";
    });
  });
}

function renderQuiz(L) {
  const q = L.quiz;
  return `<div class="g-quiz" data-lesson="${L.id}">
    <div class="g-q">❓ ${esc(q.q)}</div>
    ${q.options.map((o, i) =>
      `<label data-i="${i}"><input type="radio" name="quiz-${L.id}" value="${i}" />
       <span>${esc(o)}</span></label>`).join("")}
    <div class="g-explain" style="display:none"></div>
  </div>`;
}

function wireQuiz(root, L) {
  const quiz = root.querySelector('.g-quiz[data-lesson="' + L.id + '"]');
  const labels = quiz.querySelectorAll("label");
  labels.forEach((lab) => {
    lab.querySelector("input").addEventListener("change", () => {
      const picked = parseInt(lab.dataset.i, 10);
      labels.forEach((x) => x.classList.remove("right", "wrong"));
      const okPick = picked === L.quiz.correct;
      lab.classList.add(okPick ? "right" : "wrong");
      if (!okPick) labels[L.quiz.correct].classList.add("right");
      const ex = quiz.querySelector(".g-explain");
      ex.style.display = "block";
      ex.innerHTML = (okPick ? "✅ <b>Верно!</b> " : "↩️ <b>Почти.</b> ") + esc(L.quiz.explain);
    });
  });
}

function wireCopy(root) {
  root.querySelectorAll(".g-copy").forEach((b) => {
    b.addEventListener("click", () => {
      const code = b.parentElement.querySelector("code");
      if (code && navigator.clipboard) navigator.clipboard.writeText(code.textContent);
      const old = b.textContent;
      b.textContent = "✓ скопировано";
      setTimeout(() => { b.textContent = old; }, 1400);
    });
  });
}

/* ─── Навигация ─────────────────────────────────────────────────────── */
function goto(i) {
  state.idx = Math.max(0, Math.min(LESSONS.length - 1, i));
  renderLesson();
  try { window.scrollTo(0, 0); } catch (e) {}
}

$("#g-prev").addEventListener("click", () => goto(state.idx - 1));
$("#g-next").addEventListener("click", () => {
  const L = LESSONS[state.idx];
  if (!state.done.includes(L.id)) state.done.push(L.id);
  saveProgress({ done: state.done, last: L.id });
  if (state.idx === LESSONS.length - 1) { renderLesson(); return; }
  goto(state.idx + 1);
});
$("#g-reset").addEventListener("click", () => {
  state.done = [];
  saveProgress({ done: [], last: null });
  renderLesson();
});

/* ─── Старт ─────────────────────────────────────────────────────────── */
(function init() {
  const p = loadProgress();
  state.done = p.done;
  const lastIdx = LESSONS.findIndex((L) => L.id === p.last);
  state.idx = lastIdx >= 0 ? lastIdx : 0;
  renderLesson();
})();
