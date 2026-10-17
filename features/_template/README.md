# Feature Skeleton — эталонный шаблон

> **Рекомендация 13** из технического анализа (Sprint 4-5, P2).
> Копируйте этот каталог в `features/<your-id>/` для новой фичи.

## Структура

```
features/_template/
├── feature.yaml     ← манифест: id, requires, api_router, ui_tab
├── api.py           ← FastAPI-роутер (create_router() -> APIRouter)
├── ui.js            ← клиентская вкладка (IIFE, идемпотентная)
├── README.md        ← этот файл
└── (опц.) tests.py ← тесты фичи
```

## Контракт FeatureLoader

`src/core/features.py:FeatureLoader` при старте:

1. Сканирует `features/*/feature.yaml`.
2. Парсит YAML в `FeatureManifest` (pydantic).
3. Если `enabled: true` — вызывает `api_router` (формат `module:function`).
   В нашем случае: `import features.<id>.api as m; router = m.create_router()`.
4. `app.include_router(router)` — без правок `src/main.py`.
5. Регистрирует `ui_tab` в `GET /api/features` (отдаёт `js_url`).
6. `app.js` на старте подгружает `js_url` каждого enabled-фичи и
   вызывает `window.__llm<Name>Feature.mounted()`.

## Как создать новую фичу

```bash
# 1. Скопировать шаблон
cp -r features/_template features/my-feature

# 2. Заменить __template__ на my-feature
cd features/my-feature
sed -i 's/__template__/my-feature/g' feature.yaml api.py ui.js README.md
mv README.md README.md.tmp  # переименовать — README уникальный
sed -i 's/Шаблон фичи/My Feature/g' feature.yaml
mv README.md.tmp README.md

# 3. Реализовать create_router() в api.py (минимум — /health эндпоинт)

# 4. Реализовать UI в ui.js (минимум — mount в settings-модалку)

# 5. Включить фичу
sed -i 's/enabled: false/enabled: true/' feature.yaml

# 6. Перезапустить сервер
python run.py

# 7. Проверить, что фича подгрузилась
curl http://127.0.0.1:8000/api/features | jq '.[] | select(.id=="my-feature")'
```

## Минимальные требования к новой фиче

### `feature.yaml`

```yaml
id: my-feature         # уникальный идентификатор (kebab-case)
version: "1.0.0"
title: My Feature       # человекочитаемое название
description: >
  Что делает фича.
enabled: true           # ← true для активации

requires:
  python_packages: []   # если нужны пакеты — список с level: hard|soft
  env_vars: []

api_router: api.py:create_router   # ← точка входа

ui_tab:
  file: ui.js
  title: My Feature
  icon: "🚀"
  category: dev

ws_events: []            # если стримит события через WS
```

### `api.py`

```python
from fastapi import APIRouter

def create_router() -> APIRouter:
    router = APIRouter(prefix="/api/my-feature", tags=["my-feature"])

    @router.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # ... остальные эндпоинты

    return router
```

### `ui.js`

```javascript
(function () {
  "use strict";
  if (window.__llmMyFeatureLoaded) return;
  window.__llmMyFeatureLoaded = true;

  window.__llmMyFeature = {
    mounted: function () {
      // ... добавить вкладку в settings-модалку
    },
    unmount: function () {
      // ... cleanup
    },
  };

  if (document.readyState !== "loading") {
    window.__llmMyFeature.mounted();
  } else {
    document.addEventListener("DOMContentLoaded", function () {
      window.__llmMyFeature.mounted();
    });
  }
})();
```

## Существующие фичи для референса

| Фича | Сложность | Чему учиться |
|---|---|---|
| `features/notes/` | 🟢 низкая | Минимум — простые заметки в SQLite |
| `features/bridge/` | 🟡 средняя | WS-события + lifecycle (подключается к браузерному расширению) |
| `features/journal/` | 🔴 высокая | Полноценный API (18 эндпоинтов), сложный UI, интеграция с MCP-перехватом |
| `features/ops/` | 🟡 средняя | Бэкапы, ретенция, dry-run с подтверждением |
| `features/cluster/` | 🟡 средняя | Мульти-инстанс аналитика, агрегация из Redis |
| `features/memory/` | 🟡 средняя | Семантическая память, поиск по смыслу |
| `features/history/` | 🟡 средняя | История диалогов, FTS, экспорт в Markdown |

## Антипаттерны

❌ **Не делайте:**

- `@router.get("/api/foo")` на уровне модуля — порядок импортов хрупкий.
  Все эндпоинты внутри `create_router()`.
- Импортируйте тяжёлые пакеты на верхнем уровне модуля. Используйте
  lazy imports внутри `create_router()`.
- Используйте global state — храните state в closure внутри
  `create_router()` или в `src/state.py` (после декомпозиции main.py).
- Делайте `print()` в API-эндпоинтах — используйте `logger.info()`.
- Возвращайте None или пустой dict — всегда возвращайте structured
  JSON с понятными ключами (`{"status": "ok", "data": ...}`).

## Тестирование фичи

```python
# tests/test_my_feature.py
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from features.my_feature.api import create_router


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(create_router())
    return a


def test_health(app):
    client = TestClient(app)
    r = client.get("/api/my-feature/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
```

## Связанные документы

- `docs/ARCHITECTURE-V2.md` — §2.2, §3.10 (контракт FeatureLoader).
- `src/core/features.py` — реализация FeatureLoader.
- `features/journal/README.md` — референс полной фичи (18 эндпоинтов).
