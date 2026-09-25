"""Чтение СКД из XML."""
from __future__ import annotations

import logging
from pathlib import Path

from lxml import etree

from src.onec_dcs.schema import (
    CalculatedField,
    DataSet,
    DataSetField,
    DataSetType,
    DCS,
    Parameter,
    TotalField,
    VariantSettings,
)

logger = logging.getLogger(__name__)

# Пространства имён СКД
DCS_NS = {
    None: "http://v8.1c.ru/8.1/data-composition-system/schema",
    "dcsset": "http://v8.1c.ru/8.1/data-composition-system/settings",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


class DCSReader:
    """Читает схему компоновки данных."""

    def __init__(self, schema_path: str | Path):
        self.schema_path = Path(schema_path)
        self._tree = None

    def read(self) -> DCS:
        dcs = DCS(xml_path=str(self.schema_path))

        if not self.schema_path.exists():
            dcs.errors.append(f"Файл не найден: {self.schema_path}")
            return dcs

        try:
            self._tree = etree.parse(str(self.schema_path))
        except Exception as e:
            dcs.errors.append(f"Ошибка парсинга XML: {e}")
            return dcs

        root = self._tree.getroot()
        # Root может быть Schema или DataCompositionSchema
        dcs.uuid = root.get("uuid", "")
        dcs.name = (root.get("name") or "").strip()

        # DataSets
        dcs.data_sets = self._read_data_sets(root)

        # CalculatedFields
        dcs.calculated_fields = self._read_calculated_fields(root)

        # TotalFields
        dcs.total_fields = self._read_total_fields(root)

        # Parameters
        dcs.parameters = self._read_parameters(root)

        # Variants
        dcs.variants = self._read_variants(root)

        # Templates
        dcs.templates = self._read_templates(root)

        return dcs

    def _read_data_sets(self, root) -> list[DataSet]:
        result: list[DataSet] = []

        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag not in (
                "DataSetQuery", "DataSetObject",
                "DataSetUnion", "DataSetCalculation",
            ):
                continue

            type_map = {
                "DataSetQuery": DataSetType.QUERY,
                "DataSetObject": DataSetType.OBJECT,
                "DataSetUnion": DataSetType.UNION,
                "DataSetCalculation": DataSetType.CALCULATION,
            }
            ds = DataSet(
                name="",
                type=type_map.get(tag, DataSetType.QUERY),
            )

            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "name" and child.getparent() is not None:
                    ptag = etree.QName(child.getparent()).localname
                    if ptag == tag:
                        ds.name = (child.text or "").strip()
                elif ctag == "query" and not ds.query:
                    ds.query = (child.text or "").strip()
                elif ctag == "dataSource" and not ds.data_source:
                    ds.data_source = (child.text or "").strip()
                elif ctag == "field" and etree.QName(child.getparent()).localname in (
                    tag, "fields",
                ):
                    f = self._read_field(child)
                    if f:
                        ds.fields.append(f)

            if ds.name:
                result.append(ds)
        return result

    def _read_field(self, elem) -> DataSetField | None:
        f = DataSetField(field="")
        for child in elem.iter():
            tag = etree.QName(child).localname
            if tag == "dataPath" and not f.field:
                f.field = (child.text or "").strip()
            elif tag == "field" and not f.field:
                f.field = (child.text or "").strip()
            elif tag == "title" and not f.title:
                for item in child.iter():
                    itag = etree.QName(item).localname
                    if itag == "content" and not f.title:
                        f.title = (item.text or "").strip()
            elif tag == "role":
                f.role = (child.text or "").strip()
            elif tag == "order":
                try:
                    f.order = int((child.text or "0").strip())
                except ValueError:
                    pass
        return f if f.field else None

    def _read_calculated_fields(self, root) -> list[CalculatedField]:
        result: list[CalculatedField] = []
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag != "calculatedField":
                continue
            cf = CalculatedField(name="", expression="")
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "dataPath" and not cf.name:
                    cf.name = (child.text or "").strip()
                elif ctag == "expression" and not cf.expression:
                    cf.expression = (child.text or "").strip()
                elif ctag == "title" and not cf.title:
                    for item in child.iter():
                        itag = etree.QName(item).localname
                        if itag == "content" and not cf.title:
                            cf.title = (item.text or "").strip()
            if cf.name:
                result.append(cf)
        return result

    def _read_total_fields(self, root) -> list[TotalField]:
        result: list[TotalField] = []
        for elem in root.iter():
            if etree.QName(elem).localname != "totalField":
                continue
            tf = TotalField(name="", expression="")
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "dataPath" and not tf.name:
                    tf.name = (child.text or "").strip()
                elif ctag == "expression" and not tf.expression:
                    tf.expression = (child.text or "").strip()
            if tf.name:
                result.append(tf)
        return result

    def _read_parameters(self, root) -> list[Parameter]:
        result: list[Parameter] = []
        for elem in root.iter():
            if etree.QName(elem).localname != "parameter":
                continue
            p = Parameter(name="")
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "name" and not p.name:
                    p.name = (child.text or "").strip()
                elif ctag == "title" and not p.title:
                    for item in child.iter():
                        itag = etree.QName(item).localname
                        if itag == "content" and not p.title:
                            p.title = (item.text or "").strip()
                elif ctag == "type" and not p.type_:
                    p.type_ = (child.text or "").strip()
                elif ctag == "value" and not p.value:
                    p.value = (child.text or "").strip()
                elif ctag == "hidden":
                    p.hidden = (child.text or "").lower() in ("true", "истина")
            if p.name:
                result.append(p)
        return result

    def _read_variants(self, root) -> list[VariantSettings]:
        result: list[VariantSettings] = []
        for elem in root.iter():
            tag = etree.QName(elem).localname
            if tag not in ("settingsVariant", "variant"):
                continue
            v = VariantSettings(name="")
            for child in elem.iter():
                ctag = etree.QName(child).localname
                if ctag == "name" and not v.name:
                    v.name = (child.text or "").strip()
                elif ctag == "title" and not v.title:
                    for item in child.iter():
                        itag = etree.QName(item).localname
                        if itag == "content" and not v.title:
                            v.title = (item.text or "").strip()
            if v.name:
                result.append(v)
        return result

    def _read_templates(self, root) -> list[str]:
        result: list[str] = []
        for elem in root.iter():
            if etree.QName(elem).localname == "template":
                for child in elem.iter():
                    if etree.QName(child).localname == "name":
                        name = (child.text or "").strip()
                        if name:
                            result.append(name)
                        break
        return result

    def find_schemas_in_report(self, report_dir: str | Path) -> list[Path]:
        """Ищет все СКД в директории отчёта."""
        d = Path(report_dir)
        return list(d.rglob("*КомпоновкиДанных.xml"))
