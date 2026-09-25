# LLM-провайдеры (Task 24-d)

Система работает с **любым OpenAI-совместимым endpoint** без изменения кода.
Реестр — `config/models.yaml`; ключи — в `.env`.

> Браузерный путь (без API-ключей и без Node.js): веб-чаты DeepSeek и
> Qwen работают через куки (`DEEPSEEK_*`, `QWEN_WEB_*`) — их собирает
> расширение Bridge в один клик (см. раздел «Только куки» ниже и
> «Куки веб-чатов» в docs/SECURITY.md). Альтернатива для Qwen —
> qwenproxy (аккаунт в TUI: `qpx` → `[5] Accounts` → `A`).

## Как это работает

1. При старте чата UI спрашивает `GET /api/models` — сервер параллельно
   опрашивает всех провайдеров из `models.yaml` (таймаут ~1.5 с).
2. Провайдер с ключом в `.env` показывает живой каталог моделей
   (`GET {base_url}/models`); без ключа — серым с причиной «нет ключа …».
3. Выбор сохраняется в `runtime.yaml → model_preferences` и переживает
   перезагрузку страницы.
4. Запрос уходит на base_url нужного провайдера (per-call роутинг
   `src/llm_providers.resolve_endpoint`).

## Формат модели в чате: `provider/model`

Квалифицированные ID решают коллизии имён (одна и та же модель есть у
нескольких провайдеров):

```
openrouter/deepseek/deepseek-r1:free   → OpenRouter
deepseek/deepseek-chat                 → DeepSeek API
openrouter/deepseek-chat               → OpenRouter (та же модель, другой провайдер)
ollama_local/qwen2.5:1.5b-instruct     → локальная Ollama
```

Короткое имя без префикса (`qwen3.8-max`) тоже работает — если оно
уникально в `models:` из `models.yaml`.

## Провайдеры из коробки

| Провайдер | Ключ в .env | Доступность из РФ | Примечание |
|---|---|---|---|
| Qwen (qwenproxy) | — (локальный прокси) | да | браузерный режим |
| DeepSeek API | `DEEPSEEK_API_KEY` | да | reasoner = reasoning-модель |
| Ollama (локально) | — | да | без интернета; `ollama pull X` → модель в списке |
| LM Studio (локально) | — | да | запусти сервер в GUI (порт 1234) |
| **OpenRouter** | `OPENROUTER_API_KEY` | да | сотни моделей, есть `:free` |
| **SiliconFlow** | `SILICONFLOW_API_KEY` | да | free Qwen3/DeepSeek, регистрация по email |
| **VseGPT** | `VSEGPT_API_KEY` | да | оплата картами РФ |
| **ProxyAPI** | `PROXYAPI_API_KEY` | да | оплата картами РФ |
| Anthropic Claude | `ANTHROPIC_API_KEY` | прокси | официальный OpenAI-compat endpoint |
| Google Gemini | `GEMINI_API_KEY` | прокси | `GEMINI_URL` можно указать на свой прокси |
| Groq | `GROQ_API_KEY` | прокси | очень быстрый inference |
| Mistral AI | `MISTRAL_API_KEY` | прокси | — |
| OpenAI | `OPENAI_API_KEY` | прокси | блокирует РФ-регион (403) |

Подключение = одна строка ключа в `.env`, перезапуск сервера.

## Только куки: DeepSeek и Qwen без API-ключей и без qwenproxy

Пока вы залогинены в веб-чат, его приватный API можно использовать как
модель. Куки собирает расширение **LLM Agent Bridge** — API-ключи не
нужны вообще, Node.js не нужен, ничего не устанавливается.

Как включить (по одному разу на провайдера):

1. Установите расширение Bridge (⚙️ Настройки → «Мост» → ссылка/инструкция,
   или папка `extension/` → «Загрузить распакованное» в браузере).
2. Откройте popup расширения → **🔑 DeepSeek** (откроется chat.deepseek.com)
   или **🔑 Qwen** (chat.qwen.ai) → войдите в чат как обычно.
3. Расширение само перешлёт куки и токен на `POST /api/bridge/cookies` —
   они попадут в `.env` и в окружение сервера **сразу, без перезапуска**.
4. В селекте моделей появятся:
   * **DeepSeek (веб-чат по кукам)** → `web-chat`;
   * **Qwen (веб-чат по кукам)** → `web-chat`.

Нюансы:

* История диалога приклеивается в один промпт (у веб-чата нет
  messages-массива) — системный промпт и последние ~10 реплик сохраняются.
* Инструменты (tools) веб-чаты не умеют — вызовы функций агентом
  автоматически отключаются, фолбэк работает штатно.
* Модель сайта Qwen меняется через `.env`: `QWEN_WEB_MODEL=qwen3-max-thinking`.
* Куки протухают (обычно через недели): в чате появится подсказка
  «куки/токен отклонены» → нажмите 🔑 в расширении ещё раз. Статус и
  возраст кук — вкладка **«Мост»** или `GET /api/bridge/cookies/status`.
* Значения кук хранятся в `.env` локально и нигде не возвращаются
  (в статусе — только маскированные хвосты), см. docs/SECURITY.md.

## Авто-фолбэк

Если выбранная модель недоступна (связь, ключ, модель удалена, лимиты)
и в чат ещё не начал приходить ответ, сервер автоматически пробует
цепочку из `config/models.yaml → selection.fallbacks` (или env
`LLM_FALLBACKS` через запятую). В UI перед ответом появляется
подсказка «переключаюсь на …». Переполнение контекста фолбэком не
лечится — это честная ошибка.

Дефолтная цепочка: `qwen3.8-max` → `ollama_local/qwen2.5:1.5b-instruct`
(локальная модель — последний рубеж, работает без интернета).

## Reasoning-модели (DeepSeek-R1, QwQ, reasoner)

Ход мыслей приходит отдельным полем `reasoning_content`. Сервер
собирает его и показывает в чате сворачиваемым блоком «🧠 Размышления»;
при первом токене ответа блок сворачивается автоматически.

## Добавить своего провайдера (3 строки в models.yaml)

```yaml
providers:
  myprovider:
    name: Мой провайдер
    base_url: https://api.myprovider.example/v1
    api_key_env: MYPROVIDER_API_KEY
    # local: true          # если работает без интернета
    # docs_url: https://...
```

Локальные движки — то же самое:

```yaml
  llama_cpp:
    name: llama.cpp server
    base_url: ${LLAMACPP_URL:http://127.0.0.1:8080}/v1
    api_key_env: ""
    local: true
```

Модель в `models:` (необязательно — нужна только для living-офлайн
подсказок и флагов):

```yaml
models:
  "myprovider/my-model":
    provider: myprovider
    context_window: 32000
    supports_tools: true
```

### Универсальный шлюз LiteLLM (опционально)

Если нужен провайдер с нативным НЕ-OpenAI API (Bedrock, Vertex AI,
GigaChat) — подними LiteLLM-прокси, и он станет обычным
OpenAI-совместимым провайдером:

```bash
pip install 'litellm[proxy]'
litellm --config config/litellm.example.yaml --port 4000
```

```yaml
# config/models.yaml
providers:
  litellm:
    name: LiteLLM шлюз
    base_url: http://127.0.0.1:4000/v1
    api_key_env: LITELLM_KEY
```

Пример конфига шлюза — `config/litellm.example.yaml`.

## Заметки про локальные модели на 2 ГБ VRAM

- Одна модель на всё: `qwen2.5:1.5b-instruct` (уже настроена для
  enrichment-воркера и микрозадач). Две разные модели одновременно =
  свопы VRAM.
- Модели Ollama ≥3b автоматически считаются умеющими tools; меньше —
  вызываются без инструментов (крошки ломают схему вызовов).
- `OLLAMA_KV_CACHE_TYPE=q8_0` экономит ~40% KV-кэша.
