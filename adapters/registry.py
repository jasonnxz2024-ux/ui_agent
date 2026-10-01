"""LVGL adapter registry.

Adapters are selected from semantic evidence in the React intermediate model;
they are never selected by project name or screenshot appearance alone.
"""
from dataclasses import dataclass
from typing import Protocol

from core.model import Component, ReactProjectModel


@dataclass(frozen=True)
class AdapterResult:
    status: str  # native | partial | fallback
    widget_type: str | None
    warnings: tuple[str, ...] = ()


class Adapter(Protocol):
    name: str

    def matches(self, component: Component) -> bool: ...
    def convert(self, component: Component, model: ReactProjectModel) -> AdapterResult: ...


_adapters: list[Adapter] = []


def register(adapter: Adapter | type[Adapter]) -> Adapter | type[Adapter]:
    """Register an adapter instance or an adapter class with a nullary ctor.

    Existing instance-based adapters continue to work; class registration makes
    the concrete adapters concise and avoids global mutable configuration.
    """
    instance = adapter() if isinstance(adapter, type) else adapter
    _adapters.append(instance)
    return adapter


def resolve(component: Component) -> Adapter | None:
    for adapter in _adapters:
        if adapter.matches(component):
            return adapter
    return None


def resolve_all(component: Component) -> list[Adapter]:
    """Return every applicable adapter, preserving registration order.

    A React component may intentionally contain several semantic widgets (for
    example a data table plus an export menu).  Selecting only the first match
    silently lost one of them, so compilation uses this plural form.
    """
    return [adapter for adapter in _adapters if adapter.matches(component)]


def registered_names() -> list[str]:
    return [adapter.name for adapter in _adapters]
