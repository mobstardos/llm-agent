# Feature: notes

Эталонный пример Feature SDK (Этап 4, ARCHITECTURE-V2 §2.2): подсистема =
директория `features/<id>/` с манифестом, **ноль правок** `main.py` /
`index.html`.

## Структура

```
features/notes/
├── feature.yaml   # манифест (pydantic-валидация при старте)
├── api.py         # create_router() -> APIRouter (монтируется автоматически)
├── ui.js          # самодостаточная вкладка «Заметки» в настройках
└── README.md
```

## Что происходит автоматически

1. `FeatureLoader` читает манифест и монтирует `api.py:create_router`
   → эндпоинты `GET/POST/DELETE /api/notes[...]`.
2. `GET /api/features` отдаёт карточку вкладки (`js_url`), `app.js`
   подгружает `ui.js` при старте — вкладка появляется в настройках.
3. Изменения публикуются в шину событий (`notes.changed`) и уходят
   WS-мостом подключённым клиентам: `{"type": "event",
   "kind": "notes.changed", "payload": {...}}`.

## Хранилище

`data/notes.json` (stdlib, без БД). Не для production-нагрузок —
образец для скелетонера `python scripts/new_feature.py`.
