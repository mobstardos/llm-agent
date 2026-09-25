"""Модели метаданных 1С."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class ObjectKind(str, Enum):
    CATALOG = "Catalog"
    DOCUMENT = "Document"
    INFORMATION_REGISTER = "InformationRegister"
    ACCUMULATION_REGISTER = "AccumulationRegister"
    ACCOUNTING_REGISTER = "AccountingRegister"
    CALCULATION_REGISTER = "CalculationRegister"
    COMMON_MODULE = "CommonModule"
    ENUM = "Enum"
    REPORT = "Report"
    DATA_PROCESSOR = "DataProcessor"
    EXCHANGE_PLAN = "ExchangePlan"
    BUSINESS_PROCESS = "BusinessProcess"
    TASK = "Task"
    CONSTANT = "Constant"


KIND_TO_DIR = {
    ObjectKind.CATALOG: "Catalogs",
    ObjectKind.DOCUMENT: "Documents",
    ObjectKind.INFORMATION_REGISTER: "InformationRegisters",
    ObjectKind.ACCUMULATION_REGISTER: "AccumulationRegisters",
    ObjectKind.ACCOUNTING_REGISTER: "AccountingRegisters",
    ObjectKind.CALCULATION_REGISTER: "CalculationRegisters",
    ObjectKind.COMMON_MODULE: "CommonModules",
    ObjectKind.ENUM: "Enums",
    ObjectKind.REPORT: "Reports",
    ObjectKind.DATA_PROCESSOR: "DataProcessors",
    ObjectKind.EXCHANGE_PLAN: "ExchangePlans",
    ObjectKind.BUSINESS_PROCESS: "BusinessProcesses",
    ObjectKind.TASK: "Tasks",
    ObjectKind.CONSTANT: "Constants",
}


@dataclass
class Attribute:
    """Реквизит объекта метаданных."""
    name: str
    type_: str = "String"
    length: int = 100
    precision: int = 0
    required: bool = False
    indexed: bool = False
    default_value: str = ""
    comment: str = ""
    uuid: str = ""


@dataclass
class TabularSection:
    """Табличная часть."""
    name: str
    attributes: list[Attribute] = field(default_factory=list)
    uuid: str = ""


@dataclass
class Form:
    """Управляемая форма."""
    name: str
    kind: str = "Form"        # Form | ListForm | ChoiceForm
    xml_path: str = ""
    module_bsl_path: str = ""


@dataclass
class MetadataObject:
    """Объект метаданных."""
    kind: ObjectKind
    name: str
    synonym_ru: str = ""
    synonym_en: str = ""
    comment: str = ""
    uuid: str = ""
    xml_path: str = ""
    attributes: list[Attribute] = field(default_factory=list)
    tabular_sections: list[TabularSection] = field(default_factory=list)
    forms: list[Form] = field(default_factory=list)
    has_object_module: bool = False
    has_manager_module: bool = False

    @property
    def dir_name(self) -> str:
        return KIND_TO_DIR.get(self.kind, "")


@dataclass
class MetadataDiff:
    """Разница между двумя версиями объекта."""
    added_attributes: list[str] = field(default_factory=list)
    removed_attributes: list[str] = field(default_factory=list)
    modified_attributes: list[str] = field(default_factory=list)
    added_tabular_sections: list[str] = field(default_factory=list)
    removed_tabular_sections: list[str] = field(default_factory=list)
    added_forms: list[str] = field(default_factory=list)
    removed_forms: list[str] = field(default_factory=list)
