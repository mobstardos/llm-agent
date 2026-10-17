"""Модели СКД."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class DataSetType(str, Enum):
    QUERY = "DataSetQuery"
    OBJECT = "DataSetObject"
    UNION = "DataSetUnion"
    CALCULATION = "DataSetCalculation"


@dataclass
class DataSetField:
    field: str              # путь к полю
    title: str = ""         # заголовок
    role: str = ""          # Measurement | Dimension | Resource ...
    order: int = 0


@dataclass
class DataSet:
    name: str
    type: DataSetType = DataSetType.QUERY
    query: str = ""
    fields: list[DataSetField] = field(default_factory=list)
    data_source: str = ""    # для объекта


@dataclass
class CalculatedField:
    name: str
    expression: str = ""
    title: str = ""
    order: int = 0


@dataclass
class TotalField:
    name: str
    expression: str = ""


@dataclass
class Parameter:
    name: str
    title: str = ""
    type_: str = ""
    value: str = ""
    hidden: bool = False
    available_values: str = ""


@dataclass
class VariantSettings:
    name: str
    title: str = ""
    structure: str = ""      # XML структуры
    filter: str = ""
    order: str = ""
    parameters: str = ""


@dataclass
class DCS:
    uuid: str = ""
    name: str = ""
    xml_path: str = ""
    data_sets: list[DataSet] = field(default_factory=list)
    calculated_fields: list[CalculatedField] = field(default_factory=list)
    total_fields: list[TotalField] = field(default_factory=list)
    parameters: list[Parameter] = field(default_factory=list)
    variants: list[VariantSettings] = field(default_factory=list)
    templates: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
