"""Создание и правка XML-объектов метаданных 1С."""
from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path

from lxml import etree

from src.onec_metadata.schema import (
    Attribute,
    KIND_TO_DIR,
    MetadataObject,
    ObjectKind,
    TabularSection,
)

logger = logging.getLogger(__name__)

# Пространства имён
NSMAP = {
    None: "http://v8.1c.ru/8.3/MDClasses",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xr": "http://v8.1c.ru/8.3/xcf/readable",
    "xs": "http://www.w3.org/2001/XMLSchema",
}


def new_uuid() -> str:
    return str(uuid_mod.uuid4())


class MetadataWriter:
    """Создаёт и редактирует XML-объекты метаданных."""

    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)

    # ═══════════════════════════════════════════════════════
    # Создание объекта
    # ═══════════════════════════════════════════════════════
    def create_object(
        self,
        kind: ObjectKind,
        name: str,
        synonym_ru: str = "",
        comment: str = "",
        attributes: list[Attribute] | None = None,
    ) -> MetadataObject:
        dir_name = KIND_TO_DIR.get(kind)
        if not dir_name:
            raise ValueError(f"Неизвестный вид объекта: {kind}")

        obj_dir = self.config_dir / dir_name / name
        if obj_dir.exists():
            raise FileExistsError(f"Объект уже существует: {obj_dir}")

        obj_dir.mkdir(parents=True)

        # Базовый XML
        root = etree.Element(
            f"{{{NSMAP[None]}}}{kind.value}",
            nsmap=NSMAP,
        )
        root.set("uuid", new_uuid())

        # Внутренний MetaDataObject
        meta = etree.SubElement(root, f"{{{NSMAP[None]}}}MetaDataObject")
        obj_elem = etree.SubElement(meta, f"{{{NSMAP[None]}}}{kind.value}")
        obj_elem.set("uuid", root.get("uuid"))

        # InternalInfo — пропускаем (генерируется конфигуратором)

        # Properties
        props = etree.SubElement(obj_elem, "Properties")

        # Name
        etree.SubElement(props, "Name").text = name

        # Synonym
        syn = etree.SubElement(props, "Synonym")
        syn.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
        item = etree.SubElement(syn, "item")
        etree.SubElement(item, "lang").text = "ru"
        etree.SubElement(item, "content").text = synonym_ru or name

        # Comment
        etree.SubElement(props, "Comment").text = comment or ""

        # ObjectModule если будет
        object_module_path = obj_dir / "Ext" / "ObjectModule.bsl"
        (obj_dir / "Ext").mkdir(parents=True, exist_ok=True)

        # Добавляем реквизиты в XML через ChildObjects
        child_objects = etree.SubElement(obj_elem, "ChildObjects")

        if attributes:
            for attr in attributes:
                attr_elem = self._make_attribute_element(attr)
                child_objects.append(attr_elem)

        # Сохраняем
        xml_path = obj_dir / f"{name}.xml"
        tree = etree.ElementTree(root)
        tree.write(
            str(xml_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )

        # Пустой модуль объекта
        object_module_path.write_text(
            "// Модуль объекта\n", encoding="utf-8",
        )

        logger.info("Создан объект: %s/%s", dir_name, name)

        return MetadataObject(
            kind=kind,
            name=name,
            synonym_ru=synonym_ru,
            comment=comment,
            uuid=root.get("uuid", ""),
            xml_path=str(xml_path),
            attributes=attributes or [],
            has_object_module=True,
        )

    def _make_attribute_element(self, attr: Attribute) -> etree._Element:
        elem = etree.Element("Attribute")
        elem.set("uuid", attr.uuid or new_uuid())

        props = etree.SubElement(elem, "Properties")
        etree.SubElement(props, "Name").text = attr.name

        # Synonym
        syn = etree.SubElement(props, "Synonym")
        syn.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
        item = etree.SubElement(syn, "item")
        etree.SubElement(item, "lang").text = "ru"
        etree.SubElement(item, "content").text = attr.name

        etree.SubElement(props, "Comment").text = attr.comment or ""

        # Type
        type_elem = etree.SubElement(props, "Type")
        type_set = etree.SubElement(type_elem, "TypeSet")
        type_elem_inner = etree.SubElement(type_set, "Type")
        type_elem_inner.text = f"xs:{attr.type_}"

        # Length
        if attr.type_ in ("String", "Number"):
            etree.SubElement(props, "Length").text = str(attr.length)

        # Precision
        if attr.type_ == "Number":
            etree.SubElement(props, "Precision").text = str(attr.precision)

        # Indexing
        if attr.indexed:
            etree.SubElement(props, "Indexing").text = "Index"

        return elem

    # ═══════════════════════════════════════════════════════
    # Добавление реквизита в существующий объект
    # ═══════════════════════════════════════════════════════
    def add_attribute(
        self,
        kind: ObjectKind,
        name: str,
        attr: Attribute,
    ) -> bool:
        dir_name = KIND_TO_DIR.get(kind)
        if not dir_name:
            return False

        obj_dir = self.config_dir / dir_name / name
        xml_path = obj_dir / f"{name}.xml"
        if not xml_path.exists():
            return False

        try:
            tree = etree.parse(str(xml_path))
        except Exception as e:
            logger.warning("Read %s: %s", xml_path, e)
            return False

        # Ищем ChildObjects
        child = tree.find(f".//{{{NSMAP[None]}}}ChildObjects")
        if child is None:
            logger.warning("ChildObjects не найден в %s", xml_path)
            return False

        # Проверяем, нет ли уже такого реквизита
        for existing in child.findall(f"{{{NSMAP[None]}}}Attribute"):
            name_elem = existing.find(
                f".//{{{NSMAP[None]}}}Name"
            )
            if name_elem is not None and (name_elem.text or "").strip() == attr.name:
                logger.info("Реквизит уже существует: %s", attr.name)
                return False

        child.append(self._make_attribute_element(attr))

        tree.write(
            str(xml_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )
        logger.info("Добавлен реквизит %s в %s", attr.name, name)
        return True

    # ═══════════════════════════════════════════════════════
    # Добавление табличной части
    # ═══════════════════════════════════════════════════════
    def add_tabular_section(
        self,
        kind: ObjectKind,
        name: str,
        ts: TabularSection,
    ) -> bool:
        dir_name = KIND_TO_DIR.get(kind)
        if not dir_name:
            return False

        obj_dir = self.config_dir / dir_name / name
        xml_path = obj_dir / f"{name}.xml"
        if not xml_path.exists():
            return False

        try:
            tree = etree.parse(str(xml_path))
        except Exception as e:
            logger.warning("Read %s: %s", xml_path, e)
            return False

        child = tree.find(f".//{{{NSMAP[None]}}}ChildObjects")
        if child is None:
            return False

        for existing in child.findall(f"{{{NSMAP[None]}}}TabularSection"):
            name_elem = existing.find(f".//{{{NSMAP[None]}}}Name")
            if name_elem is not None and (name_elem.text or "").strip() == ts.name:
                logger.info("Табличная часть уже существует: %s", ts.name)
                return False

        ts_elem = etree.Element("TabularSection")
        ts_elem.set("uuid", ts.uuid or new_uuid())

        props = etree.SubElement(ts_elem, "Properties")
        etree.SubElement(props, "Name").text = ts.name

        syn = etree.SubElement(props, "Synonym")
        syn.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
        item = etree.SubElement(syn, "item")
        etree.SubElement(item, "lang").text = "ru"
        etree.SubElement(item, "content").text = ts.name

        etree.SubElement(props, "Comment").text = ""

        # Атрибуты ТЧ
        if ts.attributes:
            inner_child = etree.SubElement(ts_elem, "ChildObjects")
            for a in ts.attributes:
                inner_child.append(self._make_attribute_element(a))

        child.append(ts_elem)

        tree.write(
            str(xml_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )
        logger.info("Добавлена ТЧ %s в %s", ts.name, name)
        return True

    # ═══════════════════════════════════════════════════════
    # Создание расширения
    # ═══════════════════════════════════════════════════════
    def create_extension(
        self,
        extension_name: str,
        purpose: str = "Customization",
    ) -> Path:
        """Создаёт структуру расширения конфигурации."""
        ext_dir = self.config_dir.parent / f"Extension_{extension_name}"
        ext_dir.mkdir(parents=True, exist_ok=True)

        # Configuration.xml расширения
        xml_path = ext_dir / "Configuration.xml"
        if xml_path.exists():
            raise FileExistsError(f"Расширение уже существует: {ext_dir}")

        root = etree.Element(
            f"{{{NSMAP[None]}}}MetaDataObject",
            nsmap=NSMAP,
        )
        root.set("uuid", new_uuid())
        root.set("version", "2.17")

        # Конфигурация расширения
        conf = etree.SubElement(root, f"{{{NSMAP[None]}}}Configuration")
        conf.set("uuid", new_uuid())

        props = etree.SubElement(conf, "Properties")
        etree.SubElement(props, "Name").text = extension_name

        syn = etree.SubElement(props, "Synonym")
        syn.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
        item = etree.SubElement(syn, "item")
        etree.SubElement(item, "lang").text = "ru"
        etree.SubElement(item, "content").text = extension_name

        # Purpose
        etree.SubElement(props, "ConfigurationExtensionPurpose").text = purpose

        # NamePrefix
        etree.SubElement(props, "NamePrefix").text = ""

        child = etree.SubElement(conf, "ChildObjects")

        tree = etree.ElementTree(root)
        tree.write(
            str(xml_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )

        logger.info("Создано расширение: %s (%s)", extension_name, purpose)
        return ext_dir

    def adopt_object(
        self,
        extension_dir: str | Path,
        kind: ObjectKind,
        object_name: str,
    ) -> Path:
        """Заимствует объект в расширение."""
        ext_dir = Path(extension_dir)
        dir_name = KIND_TO_DIR.get(kind)
        if not dir_name:
            raise ValueError(f"Неизвестный вид: {kind}")

        target_dir = ext_dir / dir_name / object_name
        target_dir.mkdir(parents=True, exist_ok=True)

        # Пустая структура — платформа сама наполнит при загрузке
        xml_path = target_dir / f"{object_name}.xml"
        if xml_path.exists():
            return target_dir

        root = etree.Element(
            f"{{{NSMAP[None]}}}{kind.value}",
            nsmap=NSMAP,
        )
        root.set("uuid", new_uuid())

        meta = etree.SubElement(root, f"{{{NSMAP[None]}}}MetaDataObject")
        obj = etree.SubElement(meta, f"{{{NSMAP[None]}}}{kind.value}")
        obj.set("uuid", root.get("uuid"))
        obj.set("isAdopted", "true")

        props = etree.SubElement(obj, "Properties")
        etree.SubElement(props, "ObjectBelonging").text = "Adopted"
        etree.SubElement(props, "Name").text = object_name

        etree.SubElement(obj, "ChildObjects")

        tree = etree.ElementTree(root)
        tree.write(
            str(xml_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )

        logger.info("Заимствован объект %s/%s", dir_name, object_name)
        return target_dir
