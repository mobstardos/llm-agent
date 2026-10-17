"""Чтение управляемых форм."""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from src.onec_forms.schema import (
    FormAttribute,
    FormCommand,
    FormElement,
    ManagedForm,
)

logger = logging.getLogger(__name__)

FORM_NS = {
    None: "http://v8.1c.ru/8.3/xcf/logform",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
}


class FormReader:
    def __init__(self, xml_path: str | Path, bsl_path: str | Path | None = None):
        self.xml_path = Path(xml_path)
        self.bsl_path = Path(bsl_path) if bsl_path else None

    def read(self) -> ManagedForm:
        form = ManagedForm(xml_path=str(self.xml_path))
        if self.bsl_path:
            form.bsl_path = str(self.bsl_path)

        if not self.xml_path.exists():
            form.errors.append(f"Файл не найден: {self.xml_path}")
            return form

        try:
            tree = etree.parse(str(self.xml_path))
        except Exception as e:
            form.errors.append(f"Ошибка парсинга: {e}")
            return form

        root = tree.getroot()
        form.uuid = root.get("uuid", "")
        form.name = self.xml_path.parent.name

        # Properties
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag == "Title":
                for item in elem.iter():
                    itag = etree.QName(item).localname
                    if itag == "content" and not form.title:
                        form.title = (item.text or "").strip()
            elif tag == "FormType":
                kind = (elem.text or "").strip()
                if kind:
                    form.kind = kind

        # Attributes
        form.attributes = self._read_attributes(root)

        # ChildItems
        form.elements = self._read_child_items(root, parent="")

        # Commands
        form.commands = self._read_commands(root)

        return form

    def _read_attributes(self, root) -> list[FormAttribute]:
        result: list[FormAttribute] = []
        for elem in root.iter():
            if etree.QName(elem).localname != "Attribute":
                continue
            attr = FormAttribute(name="")
            for child in elem.iter():
                tag = etree.QName(child).localname
                if tag == "name" and not attr.name:
                    attr.name = (child.text or "").strip()
                elif tag == "type" and not attr.type_:
                    attr.type_ = (child.text or "").strip()
                elif tag == "savedData":
                    attr.saved_data = (child.text or "").lower() == "true"
                elif tag == "mainAttribute":
                    attr.main_attribute = (child.text or "").lower() == "true"
            if attr.name:
                result.append(attr)
        return result

    def _read_child_items(self, root, parent: str) -> list[FormElement]:
        result: list[FormElement] = []

        # Ищем контейнеры типов
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag in ("InputField", "LabelField", "CheckBoxField",
                       "Group", "Table", "Button", "Decoration",
                       "UsualGroup", "ColumnGroup", "Pages", "Page"):
                name = ""
                title = ""
                data_path = ""

                for child in elem.iter():
                    ctag = etree.QName(child).localname
                    if ctag == "name" and not name:
                        name = (child.text or "").strip()
                    elif ctag == "title" and not title:
                        for item in child.iter():
                            itag = etree.QName(item).localname
                            if itag == "content" and not title:
                                title = (item.text or "").strip()
                    elif ctag == "dataPath" and not data_path:
                        data_path = (child.text or "").strip()

                if name:
                    result.append(FormElement(
                        name=name, kind=tag, title=title,
                        parent=parent, data_path=data_path,
                    ))
        return result

    def _read_commands(self, root) -> list[FormCommand]:
        result: list[FormCommand] = []
        for elem in root.iter():
            if etree.QName(elem).localname != "Command":
                continue
            cmd = FormCommand(name="")
            for child in elem.iter():
                tag = etree.QName(child).localname
                if tag == "name" and not cmd.name:
                    cmd.name = (child.text or "").strip()
                elif tag == "title" and not cmd.title:
                    for item in child.iter():
                        itag = etree.QName(item).localname
                        if itag == "content" and not cmd.title:
                            cmd.title = (item.text or "").strip()
                elif tag == "action" and not cmd.action:
                    cmd.action = (child.text or "").strip()
                elif tag == "shortcut" and not cmd.shortcut:
                    cmd.shortcut = (child.text or "").strip()
                elif tag == "picture" and not cmd.picture:
                    cmd.picture = (child.text or "").strip()
            if cmd.name:
                result.append(cmd)
        return result


def find_forms_for_object(obj_dir: str | Path) -> list[dict]:
    """Ищет все формы объекта метаданных."""
    d = Path(obj_dir)
    forms_dir = d / "Ext" / "Forms"
    if not forms_dir.exists():
        return []

    result = []
    for form_dir in sorted(forms_dir.iterdir()):
        if not form_dir.is_dir():
            continue
        xml = form_dir / f"{form_dir.name}.xml"
        bsl = form_dir / "Ext" / "Form" / "Module.bsl"
        if xml.exists():
            result.append({
                "name": form_dir.name,
                "xml": str(xml),
                "bsl": str(bsl) if bsl.exists() else "",
            })
    return result
