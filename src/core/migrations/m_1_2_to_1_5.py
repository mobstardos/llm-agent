"""Миграция схемы 1.2 → 1.5.

Изменения:
  - добавлено routing_hints (keywords, negative_keywords, description_for_router)
  - добавлено ui (icon, color, category)
"""


def migrate(data: dict) -> dict:
    # routing_hints
    if "routing_hints" not in data:
        data["routing_hints"] = {
            "keywords": data.pop("keywords", []) or [],
            "negative_keywords": data.pop("negative_keywords", []) or [],
            "description_for_router": (
                data.get("description") or data.get("title", "")
            ),
        }

    # ui
    if "ui" not in data:
        data["ui"] = {
            "icon": "🤖",
            "color": "#3b82f6",
            "category": "general",
        }

    data["schema_version"] = "1.5.0"
    return data
