"""Сравнение версий объектов метаданных."""
from __future__ import annotations

import logging
from pathlib import Path

from src.onec_metadata.schema import MetadataDiff, MetadataObject
from src.onec_metadata.xml_reader import MetadataReader

logger = logging.getLogger(__name__)


def diff_objects(
    before: MetadataObject,
    after: MetadataObject,
) -> MetadataDiff:
    """Сравнивает два состояния объекта."""
    diff = MetadataDiff()

    before_attrs = {a.name for a in before.attributes}
    after_attrs = {a.name for a in after.attributes}
    diff.added_attributes = sorted(after_attrs - before_attrs)
    diff.removed_attributes = sorted(before_attrs - after_attrs)
    for a in before.attributes:
        for b in after.attributes:
            if a.name == b.name:
                if a.type_ != b.type_ or a.length != b.length \
                        or a.required != b.required:
                    diff.modified_attributes.append(a.name)
                break

    before_ts = {ts.name for ts in before.tabular_sections}
    after_ts = {ts.name for ts in after.tabular_sections}
    diff.added_tabular_sections = sorted(after_ts - before_ts)
    diff.removed_tabular_sections = sorted(before_ts - after_ts)

    before_forms = {f.name for f in before.forms}
    after_forms = {f.name for f in after.forms}
    diff.added_forms = sorted(after_forms - before_forms)
    diff.removed_forms = sorted(before_forms - after_forms)

    return diff


def snapshot_dir(config_dir: str | Path) -> dict[str, str]:
    """Снимок всех XML-объектов конфигурации (path → mtime)."""
    import hashlib
    root = Path(config_dir)
    result: dict[str, str] = {}
    if not root.exists():
        return result

    for xml in root.rglob("*.xml"):
        try:
            h = hashlib.sha256(xml.read_bytes()).hexdigest()[:16]
            rel = str(xml.relative_to(root)).replace("\\", "/")
            result[rel] = h
        except Exception:
            continue
    return result


def diff_dirs(
    before: dict[str, str], after: dict[str, str],
) -> MetadataDiff:
    """Простое сравнение директорий по хэшам."""
    diff = MetadataDiff()
    b_keys, a_keys = set(before), set(after)
    diff.added_attributes = sorted(a_keys - b_keys)[:200]
    diff.removed_attributes = sorted(b_keys - a_keys)[:200]
    for k in sorted(b_keys & a_keys):
        if before[k] != after[k]:
            diff.modified_attributes.append(k)
    return diff
