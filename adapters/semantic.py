"""Compatibility exports for the historical semantic adapter module.

Concrete, code-generating adapters live in :mod:`adapters.lvgl`.
"""
from core.model import Component, ReactProjectModel
from .registry import AdapterResult, register


class _EvidenceAdapter:
    def __init__(self, name: str, kind: str, widget: str):
        self.name, self.kind, self.widget_type = name, kind, widget
    def matches(self, component: Component) -> bool:
        return component.kind == self.kind
    def convert(self, component: Component, model: ReactProjectModel) -> AdapterResult:
        return AdapterResult("partial", self.widget_type, (
            f"{self.name} 已识别；属性、资源和事件将由生成阶段继续绑定",))


# Kept for compatibility with callers that import these two values.  The
# concrete adapters register first when ``adapters.lvgl`` is imported.
TableAdapter = register(_EvidenceAdapter("TableAdapter", "table", "lv_table"))
DropdownAdapter = register(_EvidenceAdapter("DropdownAdapter", "dropdown", "lv_dropdown"))
