"""Создание СКД."""
from __future__ import annotations

import logging
import uuid as uuid_mod
from pathlib import Path

from lxml import etree

from src.onec_dcs.schema import (
    CalculatedField,
    DataSet,
    DataSetField,
    DataSetType,
    DCS,
    Parameter,
)

logger = logging.getLogger(__name__)

NSMAP = {
    None: "http://v8.1c.ru/8.1/data-composition-system/schema",
    "dcsset": "http://v8.1c.ru/8.1/data-composition-system/settings",
    "v8": "http://v8.1c.ru/8.1/data/core",
    "xsi": "http://www.w3.org/2001/XMLSchema-instance",
}


def new_uuid() -> str:
    return str(uuid_mod.uuid4())


class DCSWriter:
    """Создаёт СКД."""

    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        name: str,
        data_sets: list[DataSet] | None = None,
        parameters: list[Parameter] | None = None,
        calculated_fields: list[CalculatedField] | None = None,
    ) -> Path:
        root = etree.Element(
            "DataCompositionSchema",
            nsmap=NSMAP,
        )
        root.set("uuid", new_uuid())

        # DataSets
        ds_container = etree.SubElement(root, "dataSets")
        for ds in (data_sets or []):
            self._append_dataset(ds_container, ds)

        # CalculatedFields
        if calculated_fields:
            cf_container = etree.SubElement(root, "calculatedFields")
            for cf in calculated_fields:
                self._append_calculated_field(cf_container, cf)

        # Parameters
        if parameters:
            p_container = etree.SubElement(root, "parameters")
            for p in parameters:
                self._append_parameter(p_container, p)

        # Settings
        settings_container = etree.SubElement(root, "settings")

        # Сохраняем
        out_path = self.output_dir / f"{name}.xml"
        tree = etree.ElementTree(root)
        tree.write(
            str(out_path),
            encoding="UTF-8",
            xml_declaration=True,
            pretty_print=True,
        )
        logger.info("СКД создана: %s", out_path)
        return out_path

    def _append_dataset(self, parent, ds: DataSet) -> None:
        tag_map = {
            DataSetType.QUERY: "DataSetQuery",
            DataSetType.OBJECT: "DataSetObject",
            DataSetType.UNION: "DataSetUnion",
            DataSetType.CALCULATION: "DataSetCalculation",
        }
        tag = tag_map.get(ds.type, "DataSetQuery")
        elem = etree.SubElement(parent, tag)
        elem.set("uuid", new_uuid())

        etree.SubElement(elem, "name").text = ds.name

        if ds.type == DataSetType.QUERY and ds.query:
            etree.SubElement(elem, "query").text = ds.query

        if ds.type == DataSetType.OBJECT and ds.data_source:
            etree.SubElement(elem, "dataSource").text = ds.data_source

        if ds.fields:
            fields_elem = etree.SubElement(elem, "fields")
            for f in ds.fields:
                field_elem = etree.SubElement(fields_elem, "field")
                etree.SubElement(field_elem, "dataPath").text = f.field
                etree.SubElement(field_elem, "field").text = f.field
                if f.title:
                    title = etree.SubElement(field_elem, "title")
                    title.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
                    item = etree.SubElement(title, "item")
                    etree.SubElement(item, "lang").text = "ru"
                    etree.SubElement(item, "content").text = f.title
                if f.role:
                    etree.SubElement(field_elem, "role").text = f.role

    def _append_calculated_field(self, parent, cf: CalculatedField) -> None:
        elem = etree.SubElement(parent, "calculatedField")
        etree.SubElement(elem, "dataPath").text = cf.name
        etree.SubElement(elem, "expression").text = cf.expression
        if cf.title:
            title = etree.SubElement(elem, "title")
            title.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
            item = etree.SubElement(title, "item")
            etree.SubElement(item, "lang").text = "ru"
            etree.SubElement(item, "content").text = cf.title

    def _append_parameter(self, parent, p: Parameter) -> None:
        elem = etree.SubElement(parent, "parameter")
        etree.SubElement(elem, "name").text = p.name
        if p.title:
            title = etree.SubElement(elem, "title")
            title.set(f"{{{NSMAP['xsi']}}}type", "v8:LocalStringType")
            item = etree.SubElement(title, "item")
            etree.SubElement(item, "lang").text = "ru"
            etree.SubElement(item, "content").text = p.title
        if p.type_:
            type_elem = etree.SubElement(elem, "type")
            type_elem.text = p.type_
        if p.value:
            etree.SubElement(elem, "value").text = p.value
        if p.hidden:
            etree.SubElement(elem, "hidden").text = "true"
