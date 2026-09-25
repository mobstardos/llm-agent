"""Миграция схемы 0.9 → 1.0.

Изменения:
  - depends_on: [list] → {hard: [...], soft: [...]}
  - добавлено поле schema_version
"""


def migrate(data: dict) -> dict:
    # depends_on
    dep = data.get("depends_on")
    if isinstance(dep, list):
        data["depends_on"] = {"hard": list(dep), "soft": []}
    elif dep is None:
        data["depends_on"] = {"hard": [], "soft": []}

    # schema_version
    data.setdefault("schema_version", "1.0.0")

    return data
