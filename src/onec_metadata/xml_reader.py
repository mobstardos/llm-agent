"""Чтение XML-выгрузки 1С."""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from src.onec_metadata.schema import (
    Attribute,
    Form,
    KIND_TO_DIR,
    MetadataObject,
    ObjectKind,
    TabularSection,
)

logger = logging.getLogger(__name__)

NS = {
    "md": "http://v8.1c.ru/8.3/MDClasses",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
}


class MetadataReader:
    """Читает XML-выгрузку конфигурации 1С."""

    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)

    # ═══════════════════════════════════════════════════════
    # Список объектов
    # ═══════════════════════════════════════════════════════
    def list_objects(
        self, kind: ObjectKind | None = None,
    ) -> list[dict]:
        result: list[dict] = []
        kinds = [kind] if kind else list(ObjectKind)

        for k in kinds:
            dir_name = KIND_TO_DIR.get(k)
            if not dir_name:
                continue
            d = self.config_dir / dir_name
            if not d.exists():
                continue
            for obj_dir in sorted(d.iterdir()):
                if not obj_dir.is_dir():
                    continue
                xml_file = obj_dir / f"{obj_dir.name}.xml"
                if not xml_file.exists():
                    continue
                result.append({
                    "kind": k.value,
                    "name": obj_dir.name,
                    "path": str(obj_dir),
                })
        return result

    def count_objects(self) -> dict[str, int]:
        result: dict[str, int] = {}
        for k in ObjectKind:
            dir_name = KIND_TO_DIR.get(k)
            if not dir_name:
                continue
            d = self.config_dir / dir_name
            if d.exists():
                count = sum(
                    1 for x in d.iterdir()
                    if x.is_dir() and (x / f"{x.name}.xml").exists()
                )
                if count:
                    result[k.value] = count
        return result

    # ═══════════════════════════════════════════════════════
    # Чтение объекта
    # ═══════════════════════════════════════════════════════
    def read_object(
        self, kind: ObjectKind, name: str,
    ) -> MetadataObject | None:
        dir_name = KIND_TO_DIR.get(kind)
        if not dir_name:
            return None

        obj_dir = self.config_dir / dir_name / name
        xml_file = obj_dir / f"{name}.xml"
        if not xml_file.exists():
            return None

        try:
            tree = etree.parse(str(xml_file))
        except Exception as e:
            logger.warning("Read %s: %s", xml_file, e)
            return None

        obj = MetadataObject(kind=kind, name=name, xml_path=str(xml_file))

        # UUID и свойства
        meta_root = tree.getroot()
        obj.uuid = meta_root.get("uuid", "")

        for elem in meta_root.iter():
            tag = etree.QName(elem).localname
            if tag == "Name" and not obj.name:
                obj.name = (elem.text or "").strip()
            elif tag == "Synonym":
                # Синoнимы: <Synonym><v8:item><v8:lang>ru</v8:lang>...
                for item in elem.iter():
                    itag = etree.QName(item).localname
                    if itag == "lang" and item.text == "ru":
                        sib = item.getnext()
                        if sib is not None:
                            obj.synonym_ru = (sib.text or "").strip()
                    elif itag == "lang" and item.text == "en":
                        sib = item.getnext()
                        if sib is not None:
                            obj.synonym_en = (sib.text or "").strip()
            elif tag == "Comment":
                obj.comment = (elem.text or "").strip()

        # Реквизиты
        obj.attributes = self._read_attributes(meta_root)

        # Табличные части
        obj.tabular_sections = self._read_tabular_sections(meta_root)

        # Модули
        obj.has_object_module = (
            obj_dir / "Ext" / "ObjectModule.bsl"
        ).exists()
        obj.has_manager_module = (
            obj_dir / "Ext" / "ManagerModule.bsl"
        ).exists()

        # Формы
        obj.forms = self._read_forms(obj_dir)

        return obj

    def _read_attributes(self, root) -> list[Attribute]:
        attrs: list[Attribute] = []
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag not in ("Attribute", "Dimension", "Resource"):
                continue

            attr = Attribute(name="", uuid=elem.get("uuid", ""))
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "Name" and child.getparent() is not None \
                        and etree.QName(child.getparent()).localname in (
                            "Attribute", "Dimension", "Resource",
                        ):
                    attr.name = (child.text or "").strip()
                elif ctag == "Type":
                    type_text = self._read_type(child)
                    if type_text:
                        attr.type_ = type_text
                elif ctag == "Length":
                    try:
                        attr.length = int((child.text or "0").strip())
                    except ValueError:
                        pass
                elif ctag == "Precision":
                    try:
                        attr.precision = int((child.text or "0").strip())
                    except ValueError:
                        pass
                elif ctag == "FillChecking" and (child.text or "") == "ShowError":
                    attr.required = True
                elif ctag == "Indexing":
                    attr.indexed = (child.text or "") in ("Index", "IndexWithAdditionalOrder")

            if attr.name:
                attrs.append(attr)
        return attrs

    def _read_type(self, type_elem) -> str:
        """Извлекает человекочитаемый тип."""
        types: list[str] = []
        for child in type_elem.iter():
            ctag = etree.QName(child).localname
            if ctag == "TypeSet":
                for t in child.iter():
                    ttag = etree.QName(t).localname
                    if ttag == "Type":
                        types.append((t.text or "").split(":")[-1])
        if not types:
            return ""
        return " | ".join(types[:3])

    def _read_tabular_sections(self, root) -> list[TabularSection]:
        sections: list[TabularSection] = []
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag != "TabularSection":
                continue

            ts = TabularSection(name="", uuid=elem.get("uuid", ""))
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "Name" and child.getparent() is not None \
                        and etree.QName(child.getparent()).localname == "TabularSection":
                    ts.name = (child.text or "").strip()
                elif ctag in ("Attribute", "Dimension", "Resource"):
                    attr = Attribute(name="")
                    for c2 in child.iter():
                        c2tag = etree.QName(c2).localname
                        if c2tag == "Name" and c2.getparent() is not None \
                                and etree.QName(c2.getparent()).localname in (
                                    "Attribute", "Dimension", "Resource",
                                ):
                            attr.name = (c2.text or "").strip()
                        elif c2tag == "Type":
                            attr.type_ = self._read_type(c2)
                    if attr.name:
                        ts.attributes.append(attr)

            if ts.name:
                sections.append(ts)
        return sections

    def _read_forms(self, obj_dir: Path) -> list[Form]:
        forms: list[Form] = []
        forms_dir = obj_dir / "Ext" / "Forms"
        if not forms_dir.exists():
            return forms

        for form_dir in sorted(forms_dir.iterdir()):
            if not form_dir.is_dir():
                continue
            xml_path = form_dir / f"{form_dir.name}.xml"
            if not xml_path.exists():
                continue
            bsl_path = form_dir / "Ext" / "Form" / "Module.bsl"
            forms.append(Form(
                name=form_dir.name,
                xml_path=str(xml_path),
                module_bsl_path=str(bsl_path) if bsl_path.exists() else "",
            ))
        return forms
