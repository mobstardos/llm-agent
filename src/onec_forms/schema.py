"""Модели управляемой формы."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FormElement:
    name: str
    kind: str              # Field | Group | Table | Button | Label | ...
    title: str = ""
    parent: str = ""
    data_path: str = ""    # привязка к реквизиту
    events: dict[str, str] = field(default_factory=dict)
    children: list["FormElement"] = field(default_factory=list)


@dataclass
class FormCommand:
    name: str
    title: str = ""
    action: str = ""       # имя обработчика
    shortcut: str = ""
    picture: str = ""


@dataclass
class FormAttribute:
    name: str
    type_: str = ""
    saved_data: bool = True
    main_attribute: bool = False


@dataclass
class ManagedForm:
    uuid: str = ""
    name: str = ""
    xml_path: str = ""
    bsl_path: str = ""
    title: str = ""
    kind: str = "Form"     # Form | ListForm | ChoiceForm
    elements: list[FormElement] = field(default_factory=list)
    commands: list[FormCommand] = field(default_factory=list)
    attributes: list[FormAttribute] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
