"""Создание управляемых форм."""
from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path

from lxml import etree

from src.onec_forms.schema import (
    FormAttribute,
    FormCommand,
    FormElement,
    ManagedForm,
)

logger = logging.getLogger(__name__)

NSMAP = {
    None: "http://v8.1c.ru/8.3/xcf/logform",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "cfg": "http://v8.1c.ru/8.1/data/enterprise/current-config",
}


def new_uuid() -> str:
    return str(uuid_mod.uuid4())


class FormWriter:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def create_simple_form(
        self,
        name: str,
        title: str = "",
        attributes: list[FormAttribute] | None = None,
        elements: list[FormElement] | None = None,
        commands: list[FormCommand] | None = None,
        kind: str = "Form",
    ) -> tuple[Path, Path]:
        """Создаёт форму + модуль."""
        root = etree.Element("Form", nsmap=NSMAP)
        root.set("uuid", new_uuid())
        root.set("version", "2.17")

        # Title
        if title:
            title_elem = etree.SubElement(root, "Title")
            title_elem.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
            item = etree.SubElement(title_elem, "item")
            etree.SubElement(item, "lang").text = "ru"
            etree.SubElement(item, "content").text = title

        # Attributes
        if attributes:
            attrs = etree.SubElement(root, "Attributes")
            for a in attributes:
                attr_elem = etree.SubElement(attrs, "Attribute")
                attr_elem.set("uuid", new_uuid())
                etree.SubElement(attr_elem, "name").text = a.name
                if a.type_:
                    etree.SubElement(attr_elem, "type").text = a.type_
                if not a.saved_data:
                    etree.SubElement(attr_elem, "savedData").text = "false"
                if a.main_attribute:
                    etree.SubElement(attr_elem, "mainAttribute").text = "true"

        # Commands
        if commands:
            cmds = etree.SubElement(root, "Commands")
            for c in commands:
                cmd_elem = etree.SubElement(cmds, "Command")
                cmd_elem.set("uuid", new_uuid())
                etree.SubElement(cmd_elem, "name").text = c.name
                if c.title:
                    t = etree.SubElement(cmd_elem, "title")
                    t.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
                    i = etree.SubElement(t, "item")
                    etree.SubElement(i, "lang").text = "ru"
                    etree.SubElement(i, "content").text = c.title
                if c.action:
                    etree.SubElement(cmd_elem, "action").text = c.action

        # ChildItems
        if elements:
            child = etree.SubElement(root, "ChildItems")
            for e in elements:
                self._append_element(child, e)

        # Сохраняем XML
        xml_path = self.output_dir / f"{name}.xml"
        tree = etree.ElementTree(root)
        tree.write(
            str(xml_path), encoding="UTF-8",
            xml_declaration=True, pretty_print=True,
        )

        # Создаём модуль
        module_dir = self.output_dir / "Ext" / "Form"
        module_dir.mkdir(parents=True, exist_ok=True)
        bsl_path = module_dir / "Module.bsl"
        if not bsl_path.exists():
            bsl_path.write_text(
                "// Модуль формы\n\n",
                encoding="utf-8",
            )

        logger.info("Форма создана: %s + %s", xml_path, bsl_path)
        return xml_path, bsl_path

    def _append_element(self, parent, e: FormElement) -> None:
        tag_map = {
            "Field": "InputField",
            "InputField": "InputField",
            "Label": "LabelField",
            "LabelField": "LabelField",
            "CheckBox": "CheckBoxField",
            "Group": "UsualGroup",
            "UsualGroup": "UsualGroup",
            "Table": "Table",
            "Button": "Button",
        }
        tag = tag_map.get(e.kind, "InputField")

        elem = etree.SubElement(parent, tag)
        elem.set("uuid", new_uuid())

        etree.SubElement(elem, "name").text = e.name
        if e.title:
            t = etree.SubElement(elem, "title")
            t.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
            i = etree.SubElement(t, "item")
            etree.SubElement(i, "lang").text = "ru"
            etree.SubElement(i, "content").text = e.title
        if e.data_path:
            etree.SubElement(elem, "dataPath").text = e.data_path

        for c in e.children:
            self._append_element(elem, c)


def add_form_to_object(
    obj_dir: str | Path,
    form: ManagedForm,
) -> Path:
    """Сохраняет форму в Ext/Forms объекта."""
    obj_path = Path(obj_dir)
    forms_dir = obj_path / "Ext" / "Forms" / form.name
    forms_dir.mkdir(parents=True, exist_ok=True)

    writer = FormWriter(forms_dir)
    xml_path, bsl_path = writer.create_simple_form(
        name=form.name,
        title=form.title,
        attributes=form.attributes,
        elements=form.elements,
        commands=form.commands,
        kind=form.kind,
    )
    return forms_dir
